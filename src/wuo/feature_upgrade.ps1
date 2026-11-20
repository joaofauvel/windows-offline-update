[CmdletBinding(DefaultParameterSetName = 'Preview')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Prepare')][switch]$Prepare,
    [Parameter(Mandatory, ParameterSetName = 'Finalize')][Guid]$FinalizeJob
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Assert-SourceBuild {
    $version = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    if ("$($version.CurrentBuildNumber).$($version.UBR)" -ne $env:WUO_FEATURE_SOURCE) {
        throw 'The running Windows build changed during the feature check.'
    }
}

function Assert-WindowsReady {
    Assert-SourceBuild
    $setup = Get-ItemProperty 'HKLM:\SYSTEM\Setup'
    $progress = Get-ItemProperty 'HKLM:\SYSTEM\Setup\MoSetup\Volatile' -Name SetupProgress -ErrorAction SilentlyContinue
    $systemInfo = New-Object -ComObject Microsoft.Update.SystemInfo
    if ($setup.SystemSetupInProgress -or $null -ne $progress -or $systemInfo.RebootRequired -or
        (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')) {
        throw 'Finish the pending Windows installation or restart before checking a feature upgrade.'
    }
}

function Assert-PlainPath {
    param([string]$Path, [switch]$Directory)
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -ne $Directory.IsPresent -or
        $item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Unexpected filesystem entry in the feature upgrade job: $Path"
    }
}

function Assert-JobMarker {
    param([string]$Path, [string]$Expected)
    Assert-PlainPath $Path
    if ([IO.File]::ReadAllText($Path, [Text.Encoding]::ASCII) -cne "$Expected`r`n") {
        throw "Invalid feature upgrade marker: $Path"
    }
}

function Assert-SourceMachine {
    $guid = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid).MachineGuid
    if ($guid -ne $request.machine) { throw 'This feature upgrade belongs to a different Windows installation.' }
    Assert-SourceBuild
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

    $sourceBuild = [int]($env:WUO_FEATURE_SOURCE.Split('.')[0])
    $expectedBuild = $env:WUO_FEATURE_BUILD
    $matching = @(foreach ($entry in Get-WindowsImage -ImagePath $images[0]) {
        $image = Get-WindowsImage -ImagePath $images[0] -Index $entry.ImageIndex
        if ($image.Languages.Count -le $image.DefaultLanguageIndex) { continue }
        if ($image.MajorVersion -eq 10 -and $image.MinorVersion -eq 0 -and
            $image.Build -gt $sourceBuild -and
            (!$expectedBuild -or $image.Build -eq [int]$expectedBuild) -and
            $image.InstallationType -eq 'Client' -and
            $image.Architecture -eq $architecture -and $image.EditionId -eq $editions[0].Edition -and
            $image.Languages[[int]$image.DefaultLanguageIndex] -eq $sourceLanguage) {
            $image
        }
    })
    if ($matching.Count -ne 1) {
        $buildDescription = if ($expectedBuild) { "with build $expectedBuild" } else { "newer than build $sourceBuild" }
        throw ("The ISO must contain one Windows image {0} for {1}, {2}, {3}." -f
            $buildDescription, $editions[0].Edition, $env:WUO_FEATURE_ARCH, $sourceLanguage)
    }
    return $matching[0]
}

$finalizing = $PSCmdlet.ParameterSetName -eq 'Finalize'
$stagedSystem = Join-Path "$env:SystemDrive\" '$WINDOWS.~BT\NewOS\Windows\System32\Config\SYSTEM'
if ($finalizing) {
    if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne 'S-1-5-18') {
        throw 'A feature upgrade can only resume from the LocalSystem startup task.'
    }
    $jobId = $FinalizeJob.ToString('D')
    $job = Join-Path $env:SystemRoot "System32\wuo\features\$jobId"
    $authorized = Join-Path $job 'authorized.id'
    $claimed = Join-Path $job 'claimed.id'
    if (!(Test-Path -LiteralPath $authorized)) { return }
    foreach ($relative in 'System32\wuo', 'System32\wuo\features') {
        Assert-PlainPath (Join-Path $env:SystemRoot $relative) -Directory
    }
    Assert-PlainPath $job -Directory
    Assert-JobMarker (Join-Path $job 'feature.ready') $jobId
    Assert-JobMarker $authorized $jobId
    if (Test-Path -LiteralPath $claimed) { throw 'This feature upgrade has already been claimed.' }
    $manifest = Join-Path $job 'feature.json'
    Assert-PlainPath $manifest
    $request = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
    if ($request.id -cne $jobId -or $request.schema -ne 1 -or
        $request.machine -notmatch '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' -or
        $request.source -notmatch '^[0-9]+\.[0-9]+$' -or
        $request.iso -notmatch '^[A-Z]:\\[^\r\n]+\.iso$' -or
        $request.sha256 -notmatch '^[0-9a-f]{64}$' -or
        $request.callback_sha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'Invalid feature upgrade request.'
    }
    $env:WUO_FEATURE_ISO = $request.iso
    $env:WUO_FEATURE_SHA256 = $request.sha256
    $env:WUO_FEATURE_SOURCE = $request.source
    $env:WUO_FEATURE_ARCH = $request.image.architecture
    $env:WUO_FEATURE_BUILD = $request.image.build
    $env:WUO_FEATURE_REVISION = $request.image.revision
    $env:WUO_FEATURE_INDEX = $request.image.index
    $env:WUO_FEATURE_EDITION = $request.image.edition
    $env:WUO_FEATURE_LANGUAGE = $request.image.language
    Assert-SourceMachine
    Assert-PlainPath $stagedSystem
} else {
    Assert-WindowsReady
}

$iso = $env:WUO_FEATURE_ISO
$drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($iso))
if ($drive.DriveType -eq [IO.DriveType]::Network) {
    throw 'Copy the Windows ISO to a local drive.'
}
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
    $language = $image.Languages[[int]$image.DefaultLanguageIndex]
    $operation = @('/Compat', 'ScanOnly')
    if (!$finalizing) { Assert-WindowsReady }
    if ($Prepare -or $finalizing) {
        if ([string]$image.ImageIndex -ne $env:WUO_FEATURE_INDEX -or
            [string]$image.Build -ne $env:WUO_FEATURE_BUILD -or
            [string]$image.SPBuild -ne $env:WUO_FEATURE_REVISION -or
            $image.EditionId -ne $env:WUO_FEATURE_EDITION -or
            $language -ne $env:WUO_FEATURE_LANGUAGE) {
            throw 'The selected image differs from the verified compatibility scan.'
        }
    }
    if ($Prepare) {
        if (Test-Path -LiteralPath $stagedSystem) { throw 'Windows Setup already has a staged installation.' }
        $bcd = "$env:SystemRoot\System32\bcdedit.exe"
        $bootBefore = & $bcd /enum '{bootmgr}' /v
        if ($LASTEXITCODE -ne 0 -or !$bootBefore) { throw 'Could not inspect Windows Boot Manager.' }
        $operation = @('/SkipFinalize')
    }
    if ($finalizing) { $operation = @('/Finalize') }
    $arguments = @('/Auto', 'Upgrade', '/Quiet') + $operation + @('/DynamicUpdate', 'Disable', '/EULA', 'Accept')
    if (!$finalizing) { $arguments += @('/ImageIndex', [string]$image.ImageIndex) }
    if ($finalizing) {
        $callback = Join-Path $env:SystemRoot "System32\update\runonce\$jobId\precommit.cmd"
        Assert-PlainPath (Split-Path -Parent $callback) -Directory
        Assert-PlainPath $callback
        if ((Get-FileHash -LiteralPath $callback -Algorithm SHA256).Hash -ne $request.callback_sha256) {
            throw 'The verified Windows Setup precommit hook is missing or changed.'
        }
        if (Get-Process -Name setup,setuphost,setupprep -ErrorAction SilentlyContinue) {
            throw 'Windows Setup is already running.'
        }
        Assert-SourceMachine
        Assert-PlainPath $stagedSystem
        Assert-JobMarker $authorized $jobId
        [IO.File]::Move($authorized, $claimed)
    }
    $process = Start-Process -FilePath $setup -WorkingDirectory $root -Wait -PassThru -ArgumentList $arguments
    if ($finalizing) {
        $result = '0x{0:X8}' -f $process.ExitCode
        [IO.File]::WriteAllText((Join-Path $job 'finalize.result'), "$result`r`n", [Text.Encoding]::ASCII)
        if ($process.ExitCode -ne 0) { throw "Windows Setup did not continue ($result). Check the Panther and precommit logs." }
    }
    if ($Prepare) {
        if ($process.ExitCode -ne 0) {
            throw ('Windows Setup preparation failed (0x{0:X8}). Check the Panther logs.' -f $process.ExitCode)
        }
        $bootAfter = & $bcd /enum '{bootmgr}' /v
        if ($LASTEXITCODE -ne 0 -or ($bootBefore -join "`n") -cne ($bootAfter -join "`n")) {
            throw 'Windows Boot Manager changed during deferred preparation.'
        }
        $version = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
        if ("$($version.CurrentBuildNumber).$($version.UBR)" -ne $env:WUO_FEATURE_SOURCE -or
            !(Test-Path -LiteralPath $stagedSystem -PathType Leaf)) {
            throw 'Windows Setup did not reach the expected deferred state.'
        }
    }
    @{
        setup_code = $process.ExitCode
        index = $image.ImageIndex
        edition = $image.EditionId
        architecture = $env:WUO_FEATURE_ARCH
        language = $language
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
