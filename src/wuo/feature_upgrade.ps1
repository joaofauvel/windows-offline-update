$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Assert-WindowsReady {
    $version = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    if ("$($version.CurrentBuildNumber).$($version.UBR)" -ne $env:WUO_FEATURE_SOURCE) {
        throw 'The running Windows build changed during the feature check.'
    }
    $setup = Get-ItemProperty 'HKLM:\SYSTEM\Setup'
    $progress = Get-ItemProperty 'HKLM:\SYSTEM\Setup\MoSetup\Volatile' -Name SetupProgress -ErrorAction SilentlyContinue
    $systemInfo = New-Object -ComObject Microsoft.Update.SystemInfo
    if ($setup.SystemSetupInProgress -or $null -ne $progress -or $systemInfo.RebootRequired -or
        (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')) {
        throw 'Finish the pending Windows installation or restart before checking a feature upgrade.'
    }
}

function Get-MatchingImage {
    param([string]$Root)

    $images = @(foreach ($name in @('install.wim', 'install.esd')) {
        $path = Join-Path $Root "sources\$name"
        if (Test-Path -LiteralPath $path -PathType Leaf) { $path }
    })
    if ($images.Count -ne 1) {
        throw 'The ISO must contain one Windows installation WIM or ESD.'
    }
    $editions = @(Get-WindowsEdition -Online)
    if ($editions.Count -ne 1 -or !$editions[0].Edition) {
        throw 'Could not identify the installed Windows edition.'
    }
    $intl = & "$env:SystemRoot\System32\dism.exe" /English /Online /Get-Intl
    $language = [regex]::Match(($intl -join "`n"), '(?im)^Default system UI language\s*:\s*([a-z0-9-]+)\s*$')
    if ($LASTEXITCODE -ne 0 -or !$language.Success) {
        throw 'Could not identify the installed Windows default system UI language.'
    }
    $sourceLanguage = $language.Groups[1].Value
    $architecture = @{ x86 = 0; x64 = 9; arm64 = 12 }[$env:WUO_FEATURE_ARCH]
    if ($null -eq $architecture) {
        throw 'The running Windows architecture is unsupported.'
    }

    $matching = @(foreach ($entry in Get-WindowsImage -ImagePath $images[0]) {
        $image = Get-WindowsImage -ImagePath $images[0] -Index $entry.ImageIndex
        if ($image.Languages.Count -le $image.DefaultLanguageIndex) { continue }
        if ($image.MajorVersion -eq 10 -and $image.MinorVersion -eq 0 -and
            $image.Build -eq [int]$env:WUO_FEATURE_BUILD -and $image.InstallationType -eq 'Client' -and
            $image.Architecture -eq $architecture -and $image.EditionId -eq $editions[0].Edition -and
            $image.Languages[[int]$image.DefaultLanguageIndex] -eq $sourceLanguage) {
            $image
        }
    })
    if ($matching.Count -ne 1) {
        throw ("The ISO must contain one Windows build {0} image for {1}, {2}, {3}." -f
            $env:WUO_FEATURE_BUILD, $editions[0].Edition, $env:WUO_FEATURE_ARCH, $sourceLanguage)
    }
    return $matching[0]
}

$iso = $env:WUO_FEATURE_ISO
$drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($iso))
if ($drive.DriveType -eq [IO.DriveType]::Network) {
    throw 'Copy the Windows ISO to a local drive.'
}
Assert-WindowsReady

$imageFile = [IO.File]::Open($iso, 'Open', 'Read', 'Read')
$mountedHere = $false
try {
    $hash = (Get-FileHash -InputStream $imageFile -Algorithm SHA256).Hash
    if ($hash -ne $env:WUO_FEATURE_SHA256) {
        throw "The ISO SHA-256 is $hash and differs from the supplied digest."
    }
    $disk = Get-DiskImage -ImagePath $iso -StorageType ISO
    if (!$disk.Attached) {
        $disk = Mount-DiskImage -ImagePath $iso -StorageType ISO -Access ReadOnly -PassThru
        $mountedHere = $true
    }
    $volumes = @($disk | Get-Volume | Where-Object DriveLetter)
    if ($volumes.Count -ne 1 -or $volumes[0].DriveLetter -notmatch '^[A-Z]$') {
        throw 'Could not find a single drive letter assigned to the verified ISO.'
    }
    $root = "$($volumes[0].DriveLetter):\"
    $setup = Join-Path $root 'setup.exe'
    $signature = Get-AuthenticodeSignature -LiteralPath $setup
    if ($signature.Status -ne 'Valid' -or
        $signature.SignerCertificate.SubjectName.Decode('UseNewLines') -notmatch '(?im)^O=Microsoft Corporation\s*$') {
        throw 'Windows Setup does not have a valid Microsoft Corporation signature.'
    }
    $image = Get-MatchingImage $root
    Assert-WindowsReady

    # The only Setup operation exposed by the preview is the compatibility scan.
    $process = Start-Process -FilePath $setup -WorkingDirectory $root -Wait -PassThru -ArgumentList @(
        '/Auto', 'Upgrade', '/Quiet', '/Compat', 'ScanOnly', '/DynamicUpdate', 'Disable',
        '/EULA', 'Accept', '/ImageIndex', [string]$image.ImageIndex
    )
    @{
        setup_code = $process.ExitCode
        index = $image.ImageIndex
        edition = $image.EditionId
        architecture = $env:WUO_FEATURE_ARCH
        language = $image.Languages[[int]$image.DefaultLanguageIndex]
        build = $image.Build
        revision = $image.SPBuild
    } | ConvertTo-Json -Compress
} finally {
    try {
        if ($mountedHere) { Dismount-DiskImage -ImagePath $iso -StorageType ISO | Out-Null }
    } finally {
        $imageFile.Dispose()
    }
}
