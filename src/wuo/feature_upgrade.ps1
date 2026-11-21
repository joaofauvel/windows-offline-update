[CmdletBinding(DefaultParameterSetName = 'Preview')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Prepare')][switch]$Prepare,
    [Parameter(ParameterSetName = 'Prepare')][Guid]$RetryPrepareJob,
    [Parameter(Mandatory, ParameterSetName = 'Finalize')][Guid]$FinalizeJob,
    [Parameter(Mandatory, ParameterSetName = 'CheckRetry')][Guid]$CheckRetryJob,
    [Parameter(ParameterSetName = 'CheckRetry')][switch]$StagedRetry,
    [Parameter(Mandatory, ParameterSetName = 'Complete')][Guid]$CompleteJob,
    [Parameter(Mandatory, ParameterSetName = 'Complete')][ValidateSet('success', 'failure')][string]$Outcome
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

function Assert-ProtectedJobDirectory([string]$Path) {
    Assert-PlainPath $Path -Directory
    $acl = Get-Acl -LiteralPath $Path
    $trusted = 'S-1-5-18', 'S-1-5-32-544'
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if ($owner -notin $trusted -or !$rules.Count -or @($rules | Where-Object {
        $_.AccessControlType -ne 'Allow' -or $_.IdentityReference.Value -notin $trusted
    }).Count) {
        throw "The feature job must only grant access to SYSTEM and Administrators: $Path"
    }
}

function Assert-FeatureBackupAvailable {
    param([string]$Job, [string]$JobId, $Request, [switch]$VerifyArtifacts)
    $receiptPath = Join-Path $Job 'rollback.json'
    Assert-PlainPath $receiptPath
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    if ($receipt.schema -ne 1 -or $receipt.id -cne $JobId -or
        $receipt.machine -ne $Request.machine -or $receipt.source -ne $Request.source -or
        $receipt.computer -ne $env:COMPUTERNAME -or
        $receipt.catalog -notmatch '^[0-9]{2}/[0-9]{2}/[0-9]{4}-[0-9]{2}:[0-9]{2}$' -or
        $receipt.volume -notmatch '^\\\\\?\\Volume\{[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\}\\$') {
        throw 'The rollback backup receipt does not match the staged feature upgrade.'
    }
    $partitions = @(Get-Partition | Where-Object { $_.AccessPaths -contains $receipt.volume })
    if ($partitions.Count -ne 1 -or $partitions[0].DriveLetter -notmatch '^[A-Z]$') {
        throw 'The selected rollback backup drive is unavailable.'
    }
    $drive = "$($partitions[0].DriveLetter):"
    $root = Join-Path "$drive\" 'WUO-Rollback'
    $snapshot = Join-Path $root $JobId
    $portable = Join-Path $snapshot 'recovery'
    foreach ($path in $root, $snapshot, $portable) { Assert-PlainPath $path -Directory }
    foreach ($name in 'restore.cmd', 'certutil.exe', 'findstr.exe') {
        Assert-PlainPath (Join-Path $portable $name)
    }
    if ($VerifyArtifacts) {
        $runner = Join-Path $Job 'restore.cmd'
        $digest = Join-Path $Job 'restore.sha256'
        $portableRunner = Join-Path $portable 'restore.cmd'
        $portableDigest = Join-Path $portable 'restore.sha256'
        foreach ($path in $runner, $digest, $portableDigest) { Assert-PlainPath $path }
        $expected = Get-Content -LiteralPath $digest -Raw -Encoding ASCII
        if ($expected -cnotmatch '^[0-9a-f]{64}\r\n\z' -or
            (Get-Content -LiteralPath $portableDigest -Raw -Encoding ASCII) -cne $expected -or
            (Get-FileHash -LiteralPath $runner -Algorithm SHA256).Hash -ne $expected.Trim() -or
            (Get-FileHash -LiteralPath $portableRunner -Algorithm SHA256).Hash -ne $expected.Trim()) {
            throw 'The rollback launcher or its checksum changed.'
        }
    }
    $null = & "$env:SystemRoot\System32\wbadmin.exe" get items "-version:$($receipt.catalog)" "-backupTarget:$drive" "-machine:$($receipt.computer)"
    if ($LASTEXITCODE -ne 0) { throw 'Windows Backup could not read the selected rollback backup.' }
}

function Assert-SourceMachine {
    $guid = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid).MachineGuid
    if ($guid -ne $request.machine) { throw 'This feature upgrade belongs to a different Windows installation.' }
    Assert-SourceBuild
}

function Assert-BootPreservationAssets([string]$Job, $Request) {
    $assets = $Request.boot_preservation
    if (!$assets) { return }
    if (!$assets.'feature_boot.ps1' -or !$assets.'boot-order.json') {
        throw 'The feature upgrade is missing a required boot preservation asset.'
    }
    foreach ($asset in $assets.PSObject.Properties) {
        if ($asset.Name -cnotin 'feature_boot.ps1', 'boot-order.json', 'boot-fallback.efi' -or
            $asset.Value -cnotmatch '^[0-9a-f]{64}$') {
            throw 'The feature upgrade declares an invalid boot preservation asset.'
        }
        $path = Join-Path $Job $asset.Name
        Assert-PlainPath $path
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $asset.Value) {
            throw "The boot preservation asset $($asset.Name) changed after capture."
        }
    }
}

function Assert-FeatureRetry {
    param([string]$Job, [string]$JobId, $Request, [string]$StagedSystem, [switch]$Staged)
    if ($Request.rollback -ne $true -or !$Request.boot_preservation) {
        throw 'Retrying a feature upgrade requires its verified rollback backup and boot preservation assets.'
    }
    if (Test-Path -LiteralPath (Join-Path $Job 'authorized.id')) {
        throw 'The feature upgrade is already authorized.'
    }
    if (Test-Path -LiteralPath (Join-Path $Job 'boot-rollback.id')) {
        throw 'The feature upgrade has a pending rollback.'
    }
    $env:WUO_FEATURE_SOURCE = $Request.source
    Assert-SourceMachine
    if (Get-Process -Name setup,setuphost,setupprep -ErrorAction SilentlyContinue) {
        throw 'Windows Setup is still running or staged.'
    }
    if ($Staged) {
        $attempt = Join-Path $Job 'retry-active'
        Assert-ProtectedJobDirectory $attempt
        Assert-JobMarker (Join-Path $attempt 'prepared.id') $JobId
        Assert-PlainPath $StagedSystem
        if ((Get-ItemProperty 'HKLM:\SYSTEM\Setup').SystemSetupInProgress) {
            throw 'Windows Setup is still running or staged.'
        }
    } else {
        Assert-WindowsReady
        if (Test-Path -LiteralPath $StagedSystem) { throw 'Windows Setup is still running or staged.' }
    }
    $runner = Join-Path $Job 'feature_upgrade.ps1'
    Assert-PlainPath $runner
    $fileArgument = if ($runner -match '[ \t]') { '"' + $runner + '"' } else { $runner }
    $arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File $fileArgument -FinalizeJob $JobId"
    $task = Get-ScheduledTask -TaskName "wuo-feature-$JobId" -TaskPath '\'
    if ($task.State -ne 'Ready' -or $task.Principal.UserId -notin 'SYSTEM', 'NT AUTHORITY\SYSTEM', 'S-1-5-18' -or
        @($task.Triggers).Count -ne 1 -or $task.Triggers[0].CimClass.CimClassName -ne 'MSFT_TaskBootTrigger' -or
        !$task.Triggers[0].Enabled -or @($task.Actions).Count -ne 1 -or
        $task.Actions[0].Execute -ne "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -or
        $task.Actions[0].Arguments -cne $arguments) {
        throw 'The expected LocalSystem startup task is missing, changed, or not ready.'
    }
    Assert-BootPreservationAssets $Job $Request
    Assert-JobMarker (Join-Path $Job 'rollback.required') $JobId
    Assert-FeatureBackupAvailable $Job $JobId $Request -VerifyArtifacts
}

function Complete-FeatureBoot([string]$Job, [string]$JobId, $Request, [string]$Completion) {
    if (!$Request.boot_preservation) { throw 'Boot preservation was not requested for this upgrade.' }
    $result = Join-Path $Job 'boot-order-result.json'
    if (Test-Path -LiteralPath $result) {
        Assert-PlainPath $result
        $previous = [IO.File]::ReadAllText($result) | ConvertFrom-Json
        if ($previous.order -in 'restored', 'unchanged' -and
            $previous.fallback -in 'restored', 'unchanged', 'absent') { return }
    }
    try {
        $machine = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid).MachineGuid
        $version = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
        if ($machine -ne $Request.machine) { throw 'Boot preservation belongs to a different installation.' }
        if ($Completion -eq 'success') {
            Assert-JobMarker (Join-Path $Job 'precommit.done') $JobId
            if ($version.CurrentBuildNumber -ne $Request.image.build -or
                $version.UBR -lt $Request.image.revision -or $version.EditionID -ne $Request.image.edition) {
                throw 'The upgraded Windows installation could not be verified for boot preservation.'
            }
        } elseif ("$($version.CurrentBuildNumber).$($version.UBR)" -ne $Request.source) {
            throw 'The original Windows installation could not be verified for boot preservation.'
        }
        $phase = Join-Path $Job 'boot-completion.phase'
        if (Test-Path -LiteralPath $phase) { Assert-PlainPath $phase }
        [IO.File]::WriteAllText($phase, "$Completion`r`n", [Text.Encoding]::ASCII)
        Assert-BootPreservationAssets $Job $Request
        $null = & (Join-Path $Job 'feature_boot.ps1') -Mode Restore -JobPath $Job
        Assert-PlainPath $result
    } catch {
        $message = $_.Exception.Message
        $report = @{schema=1; order='failed'; order_detail=$message; fallback='failed'; fallback_detail=$message}
        $temporary = Join-Path $Job "boot-order-result-$([Guid]::NewGuid().ToString('N')).tmp"
        [IO.File]::WriteAllText($temporary, ($report | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
        if (Test-Path -LiteralPath $result) {
            $previousPath = "$temporary.old"
            [IO.File]::Replace($temporary, $result, $previousPath)
            [IO.File]::Delete($previousPath)
        } else { [IO.File]::Move($temporary, $result) }
    }
    $menu = Join-Path (Split-Path -Parent (Split-Path -Parent $Job)) 'menu'
    Assert-ProtectedJobDirectory $menu
    [IO.File]::WriteAllText((Join-Path $menu 'boot-order-last.id'), "$JobId`r`n", [Text.Encoding]::ASCII)
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
$completing = $PSCmdlet.ParameterSetName -eq 'Complete'
$checkingRetry = $PSCmdlet.ParameterSetName -eq 'CheckRetry'
$retryPreparing = $Prepare -and $PSBoundParameters.ContainsKey('RetryPrepareJob')
$stagedSystem = Join-Path "$env:SystemDrive\" '$WINDOWS.~BT\NewOS\Windows\System32\Config\SYSTEM'
if ($finalizing -or $completing -or $checkingRetry -or $retryPreparing) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ($checkingRetry -or $retryPreparing) {
        $principal = [Security.Principal.WindowsPrincipal]::new($identity)
        if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw 'Check the feature upgrade retry from an elevated Windows terminal.'
        }
    } elseif ($identity.User.Value -ne 'S-1-5-18') {
        throw 'A feature upgrade can only resume from the LocalSystem startup task.'
    }
    $jobId = if ($checkingRetry) { $CheckRetryJob.ToString('D') } elseif ($retryPreparing) {
        $RetryPrepareJob.ToString('D')
    } elseif ($finalizing) {
        $FinalizeJob.ToString('D')
    } else { $CompleteJob.ToString('D') }
    $programData = Join-Path "$env:SystemDrive\" 'ProgramData'
    $root = Join-Path $programData 'WinOfflineUpdate'
    $jobs = Join-Path $root 'jobs'
    $job = Join-Path $jobs $jobId
    $authorized = Join-Path $job 'authorized.id'
    $claimed = Join-Path $job 'claimed.id'
    $rollbackMarker = Join-Path $job 'boot-rollback.id'
    $bootPhase = Join-Path $job 'boot-completion.phase'
    $rolledBack = $finalizing -and (Test-Path -LiteralPath $rollbackMarker)
    $retryBoot = $finalizing -and (Test-Path -LiteralPath $bootPhase)
    if ($finalizing -and !$rolledBack -and !$retryBoot -and !(Test-Path -LiteralPath $authorized)) { return }
    Assert-PlainPath $programData -Directory
    foreach ($path in $root, $jobs, $job) {
        Assert-ProtectedJobDirectory $path
    }
    if ($rolledBack) {
        Assert-JobMarker $rollbackMarker $jobId
    } else {
        Assert-JobMarker (Join-Path $job 'feature.ready') $jobId
        if ($completing -or $checkingRetry -or $retryPreparing -or $retryBoot) { Assert-JobMarker $claimed $jobId }
        else {
            Assert-JobMarker $authorized $jobId
            if (Test-Path -LiteralPath $claimed) { throw 'This feature upgrade has already been claimed.' }
        }
    }
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
    if ($checkingRetry) {
        Assert-FeatureRetry $job $jobId $request $stagedSystem -Staged:$StagedRetry
        return
    }
    if ($retryPreparing) {
        $retryAttempt = Join-Path $job 'retry-active'
        Assert-ProtectedJobDirectory $retryAttempt
        $preparedMarker = Join-Path $retryAttempt 'prepared.id'
        if (Test-Path -LiteralPath $preparedMarker) { throw 'The feature retry is already prepared.' }
        Assert-FeatureRetry $job $jobId $request $stagedSystem
    }
    if ($completing -or $rolledBack -or $retryBoot) {
        if ($rolledBack -and $request.rollback -ne $true) { throw 'The rollback request is missing.' }
        $completion = if ($rolledBack) { 'rollback' } elseif ($completing) { $Outcome } else {
            Assert-PlainPath $bootPhase
            $phaseText = [IO.File]::ReadAllText($bootPhase, [Text.Encoding]::ASCII)
            if ($phaseText -cnotin "success`r`n", "failure`r`n") { throw 'Invalid boot preservation completion phase.' }
            $phaseText.TrimEnd("`r", "`n")
        }
        Complete-FeatureBoot $job $jobId $request $completion
        return
    }
    Assert-BootPreservationAssets $job $request
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
    if (!$retryPreparing) { Assert-PlainPath $stagedSystem }
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
        if ($request.rollback -eq $true) {
            $backupLog = Join-Path $job 'backup-check.log'
            try {
                Assert-JobMarker (Join-Path $job 'rollback.required') $jobId
                Assert-FeatureBackupAvailable $job $jobId $request
                [IO.File]::Delete($backupLog)
            } catch {
                [IO.File]::WriteAllText($backupLog, $_.Exception.Message)
                throw
            }
        }
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
        if ($retryPreparing) {
            Assert-PlainPath $stagedSystem
            Assert-ProtectedJobDirectory $retryAttempt
            $temporaryMarker = Join-Path $retryAttempt "prepared-$([Guid]::NewGuid().ToString('N')).tmp"
            try {
                [IO.File]::WriteAllText($temporaryMarker, "$jobId`r`n", [Text.Encoding]::ASCII)
                [IO.File]::Move($temporaryMarker, $preparedMarker)
            } finally { [IO.File]::Delete($temporaryMarker) }
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
