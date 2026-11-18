$ErrorActionPreference = 'Stop'

function Find-Partition([guid]$Id) {
    $matches = @(Get-Partition | Where-Object { $_.Guid -and ([guid]$_.Guid -eq $Id) })
    if ($matches.Count -ne 1) {
        throw "Expected one GPT partition with ID $Id, found $($matches.Count)."
    }
    $matches[0]
}

function Get-PartitionLetter($Partition) {
    $letter = [string]$Partition.DriveLetter
    if ($letter -match '^[A-WY-Z]$') { return "${letter}:" }

    $used = @(Get-PSDrive -PSProvider FileSystem | Select-Object -ExpandProperty Name)
    $used += @(Get-Volume | Where-Object DriveLetter | Select-Object -ExpandProperty DriveLetter)
    $letter = @('S','T','U','V','W','Y','Z','R','Q','P','O','N','M','L','K','J','I','H','G','F','E','D','C') |
        Where-Object { $_ -notin $used } | Select-Object -First 1
    if (!$letter) { throw 'No spare drive letter is available.' }
    $script = Join-Path $env:SystemRoot 'Temp\wuo-assign.txt'
    @("select disk $([int]$Partition.DiskNumber)", "select partition $([int]$Partition.PartitionNumber)",
      "assign letter=$letter", 'exit') | Set-Content -LiteralPath $script -Encoding ASCII
    try {
        & (Join-Path $env:SystemRoot 'System32\diskpart.exe') /s $script | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "DiskPart could not assign ${letter}: to the selected partition." }
        $volumePath = (& (Join-Path $env:SystemRoot 'System32\mountvol.exe') "${letter}:\" /L | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or !$volumePath -or
            $volumePath -notin (Find-Partition ([guid]$Partition.Guid)).AccessPaths) {
            throw "The new drive letter ${letter}: did not resolve to the expected partition."
        }
        "${letter}:"
    } finally {
        Remove-Item -LiteralPath $script -Force
    }
}

function Get-PlainItem($Root, $Relative, [switch]$Directory) {
    $path = $Root
    foreach ($segment in ($Relative -split '[\\/]')) {
        if ($segment -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._+~ -]{0,199}$' -or $segment -match '[. ]$' -or
            $segment -match '^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])([.]|$)' -or $segment -eq 'manifest.json') {
            throw "Unsafe job path: $Relative"
        }
        $path = Join-Path $path $segment
        $item = Get-Item -LiteralPath $path -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Reparse point in job path: $path"
        }
    }
    if ($item.PSIsContainer -ne $Directory.IsPresent) { throw "Unexpected job path type: $path" }
    $item.FullName
}

function Get-Sha256($Path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try {
            [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '')
        } finally {
            $stream.Dispose()
        }
    } finally {
        $algorithm.Dispose()
    }
}

try {
    if ($env:SystemRoot -ne 'X:\Windows' -or !(Test-Path 'HKLM:\SYSTEM\CurrentControlSet\Control\MiniNT')) {
        throw 'Boot this job inside WinPE.'
    }
    $job = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'wuo-job.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($job.mode -notin @('check', 'apply') -or !$job.files.'recovery.cmd' -or !$job.files.'plan.json') {
        throw 'The WinPE job record is incomplete.'
    }
    $id = ([guid]$job.job).ToString()
    $windows = Find-Partition ([guid]$job.partitions.windows)
    $efi = Find-Partition ([guid]$job.partitions.efi)
    if ($windows.Guid -eq $efi.Guid -or [guid]$efi.GptType -ne [guid]'c12a7328-f81f-11d2-ba4b-00a0c93ec93b') {
        throw 'The planned EFI partition does not match the boot record.'
    }
    $targetLetter = Get-PartitionLetter $windows
    $efiLetter = Get-PartitionLetter $efi
    if ($targetLetter -eq $efiLetter) { throw 'Windows and EFI cannot share a drive letter.' }
    $targetRoot = "$targetLetter\"
    $marker = Get-PlainItem $targetRoot "ProgramData\WinOfflineUpdate\jobs\$id\target.id"
    if ((Get-Content -LiteralPath $marker -Raw -Encoding ASCII).Trim() -ne $id) {
        throw 'The Windows target marker does not match this job.'
    }
    $bundle = Get-PlainItem $targetRoot $job.bundle -Directory
    foreach ($record in $job.files.PSObject.Properties) {
        $file = Get-PlainItem $bundle $record.Name
        if ((Get-Sha256 $file) -ne $record.Value) {
            throw "Bundle digest mismatch: $($record.Name)"
        }
    }
    $runner = Get-PlainItem $bundle 'recovery.cmd'
    & $runner check $targetLetter $efiLetter
    if ($LASTEXITCODE -ne 0) { throw "Recovery target check returned $LASTEXITCODE." }
    if ($job.mode -eq 'check') { exit 0 }
    & $runner apply $targetLetter $efiLetter
    if ($LASTEXITCODE -ne 0) { throw "Recovery apply returned $LASTEXITCODE. Inspect the recovery log." }
    & (Join-Path $env:SystemRoot 'System32\wpeutil.exe') reboot
    if ($LASTEXITCODE -ne 0) { throw 'Servicing succeeded. Restart into Windows manually.' }
    exit 0
} catch {
    Write-Host "WinPE job stopped: $($_.Exception.Message)"
    exit 1
}
