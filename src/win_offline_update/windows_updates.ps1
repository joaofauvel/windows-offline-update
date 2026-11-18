param(
    [Parameter(Mandatory = $true)][string]$Request,
    [Parameter(Mandatory = $true)][string]$Journal
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Assert-RequestInteger {
    param($Value, [string]$Name, [long]$Minimum = 0)
    if (($Value -isnot [int] -and $Value -isnot [long]) -or $Value -lt $Minimum -or $Value -gt [int]::MaxValue) {
        throw "Invalid integer in security update request: $Name."
    }
}

function Assert-RequestString {
    param($Value, [string]$Name)
    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value) -or $Value -match '[\x00-\x1f]') {
        throw "Invalid string in security update request: $Name."
    }
}

function Read-SecurityUpdateRequest {
    param([string]$Path)
    $value = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-RequestInteger $value.schema_version 'schema_version' 1
    if ($value.schema_version -ne 1 -or $value.mode -isnot [string] -or $value.mode -notin @('install', 'verify')) {
        throw 'Unsupported security update request schema or mode.'
    }
    $requestGuid = [guid]::Empty
    if ($value.request_id -isnot [string] -or ![guid]::TryParseExact($value.request_id, 'D', [ref]$requestGuid) -or $requestGuid -eq [guid]::Empty) {
        throw 'Invalid security update request ID.'
    }
    Assert-RequestString $value.target.machine_guid 'target.machine_guid'
    Assert-RequestInteger $value.target.build 'target.build' 1
    Assert-RequestInteger $value.target.revision 'target.revision'
    if ($value.target.architecture -isnot [string] -or $value.target.architecture -notin @('x86', 'x64', 'arm64')) {
        throw 'Invalid target architecture in security update request.'
    }
    if ($null -eq $value.updates -or $value.updates -isnot [array] -or !$value.updates.Count) {
        throw 'The security update request must contain an array of updates.'
    }
    $seen = @{}
    foreach ($update in $value.updates) {
        foreach ($name in @('id', 'kb', 'title', 'category')) { Assert-RequestString $update.$name $name }
        $parsed = [guid]::Empty
        if (![guid]::TryParseExact($update.id, 'D', [ref]$parsed) -or $parsed -eq [guid]::Empty) {
            throw 'Invalid security update ID.'
        }
        Assert-RequestInteger $update.revision 'update.revision' 1
        if ($update.kb -cnotin @('KB4052623', 'KB5007651', 'KB2267602')) {
            throw 'This update is outside the Windows security installer allowlist.'
        }
        $key = $parsed.ToString('D')
        if ($seen.ContainsKey($key)) { throw 'Duplicate security update ID in request.' }
        $seen[$key] = $true
    }
    foreach ($name in @('previous_reboot_required')) {
        if ($value.PSObject.Properties[$name] -and $value.$name -isnot [bool]) { throw "Invalid $name." }
    }
    if ($value.PSObject.Properties['previous_boot_id']) { Assert-RequestString $value.previous_boot_id 'previous_boot_id' }
    if ($value.mode -eq 'verify' -and $value.previous_reboot_required -eq $true -and !$value.PSObject.Properties['previous_boot_id']) {
        throw 'A previous restart requirement must include its boot identity.'
    }
    if ($value.PSObject.Properties['hooks_job_id']) {
        $hooksGuid = [guid]::Empty
        if ($value.hooks_job_id -isnot [string] -or ![guid]::TryParseExact($value.hooks_job_id, 'D', [ref]$hooksGuid) -or $hooksGuid -eq [guid]::Empty) {
            throw 'Invalid registry hook transaction ID.'
        }
    }
    return $value
}

function Assert-WindowsNativeAdministrator {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Security updates require Windows.' }
    if ([Environment]::Is64BitOperatingSystem -and ![Environment]::Is64BitProcess) {
        throw 'Use native 64-bit PowerShell for Windows security updates.'
    }
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Use an elevated native Windows terminal for Windows security updates.'
    }
}

function Get-WindowsTargetIdentity {
    $windows = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $processor = Get-CimInstance Win32_Processor | Select-Object -First 1
    $architecture = @{0 = 'x86'; 9 = 'x64'; 12 = 'arm64'}[[int]$processor.Architecture]
    $processArchitecture = @{x86 = 'x86'; AMD64 = 'x64'; ARM64 = 'arm64'}[$env:PROCESSOR_ARCHITECTURE]
    if (!$architecture -or $architecture -ne $processArchitecture) { throw 'Use native PowerShell for the Windows architecture.' }
    return [pscustomobject]@{
        machine_guid = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid
        build = [int]$windows.CurrentBuildNumber
        revision = [int]$windows.UBR
        architecture = $architecture
    }
}

function Get-WindowsBootId {
    $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
    return $boot.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
}

function Get-SecurityObserved {
    $observed = [ordered]@{AntivirusSignatureVersion = $null; AMProductVersion = $null; AMEngineVersion = $null; SecurityAppVersion = $null}
    try {
        $status = Get-MpComputerStatus -ErrorAction Stop
        foreach ($name in @('AntivirusSignatureVersion', 'AMProductVersion', 'AMEngineVersion')) {
            if ($status.$name) { $observed[$name] = [string]$status.$name }
        }
    } catch { } # Defender can be unavailable while Windows Security itself is updateable.
    try {
        $app = Get-AppxPackage -Name Microsoft.SecHealthUI -ErrorAction Stop | Sort-Object Version -Descending | Select-Object -First 1
        if ($app) { $observed.SecurityAppVersion = [string]$app.Version }
    } catch { }
    return $observed
}

function New-WuaSession { return New-Object -ComObject Microsoft.Update.Session }
function New-WuaUpdateCollection { return ,(New-Object -ComObject Microsoft.Update.UpdateColl) }
function Get-WuaRebootRequired {
    $systemInfo = New-Object -ComObject Microsoft.Update.SystemInfo
    return [bool]$systemInfo.RebootRequired
}

function Write-SecurityJournal {
    param($Value, [string]$Path)
    $destination = [IO.Path]::GetFullPath($Path)
    $temporary = $destination + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 20))
    try {
        $stream = [IO.FileStream]::new($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        if ([IO.File]::Exists($destination)) { [IO.File]::Replace($temporary, $destination, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $destination) }
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Find-ExactSecurityUpdate {
    param($Searcher, $Row, [bool]$Installed)
    $installedValue = [int]$Installed
    $result = $Searcher.Search("UpdateID='$($Row.id)' and RevisionNumber=$($Row.revision) and Type='Software' and IsInstalled=$installedValue and IsHidden=0")
    if ($result.ResultCode -ne 2 -or $result.Updates.Count -gt 1) { throw 'Windows Update could not confirm the exact security update revision.' }
    if (!$result.Updates.Count) { return $null }
    $update = $result.Updates.Item(0)
    if ([guid]$update.Identity.UpdateID -ne [guid]$Row.id -or $update.Identity.RevisionNumber -ne $Row.revision -or
        $update.Type -ne 1 -or @($update.KBArticleIDs) -notcontains $Row.kb.Substring(2) -or $update.Title -cne $Row.title) {
        throw 'The exact security update identity or metadata changed. Run a fresh scan.'
    }
    return $update
}

function Test-IntelligenceVersion {
    param($Row, $Observed)
    if ($Row.kb -ne 'KB2267602' -or !$Observed.AntivirusSignatureVersion) { return $false }
    $match = [regex]::Match($Row.title, '\b\d+(?:\.\d+){3}\b')
    if (!$match.Success) { return $false }
    try { return [version]$Observed.AntivirusSignatureVersion -ge [version]$match.Value } catch { return $false }
}

function Invoke-WindowsSecurityUpdates {
    param([string]$RequestPath, [string]$JournalPath)
    $inputRequest = Read-SecurityUpdateRequest $RequestPath
    Assert-WindowsNativeAdministrator
    $actual = Get-WindowsTargetIdentity
    if ($actual.machine_guid -ne $inputRequest.target.machine_guid -or $actual.architecture -ne $inputRequest.target.architecture) {
        throw 'The security update request belongs to a different Windows installation.'
    }
    $baseline = [version]::new($inputRequest.target.build, $inputRequest.target.revision)
    $running = [version]::new($actual.build, $actual.revision)
    if (($inputRequest.mode -eq 'install' -and $running -ne $baseline) -or ($inputRequest.mode -eq 'verify' -and $running -lt $baseline)) {
        throw 'The Windows build changed. Run a fresh update scan.'
    }
    $order = @{'KB4052623' = 0; 'KB5007651' = 1; 'KB2267602' = 2}
    $rows = @(foreach ($update in ($inputRequest.updates | Sort-Object @{Expression = {$order[$_.kb]}}, id)) {
        $row = [ordered]@{
            id = $update.id; revision = $update.revision; kb = $update.kb; title = $update.title; category = $update.category
            state = 'pending'; verified = $false; installed_proof = $false
            result_code = $null; hresult = $null; reboot_required = $false; error = $null
        }
        if ($inputRequest.mode -eq 'verify') {
            if ($update.state -in @('pending', 'downloading', 'installing', 'installed', 'failed', 'unavailable', 'reboot-required')) { $row.state = $update.state }
            foreach ($name in @('result_code', 'hresult')) {
                if ($update.$name -is [int] -or $update.$name -is [long]) { $row[$name] = $update.$name }
            }
            if ($update.error -is [string]) { $row.error = $update.error }
        }
        $row
    })
    $bootId = Get-WindowsBootId
    $sameBootRestart = $inputRequest.mode -eq 'verify' -and $inputRequest.previous_reboot_required -eq $true -and $inputRequest.previous_boot_id -eq $bootId
    $journalValue = [ordered]@{
        schema_version = 1; request_id = $inputRequest.request_id; hooks_job_id = $inputRequest.hooks_job_id
        target = $inputRequest.target; boot_id = $bootId
        reboot_required = [bool]$sameBootRestart; updates = $rows; observed = Get-SecurityObserved
    }
    Write-SecurityJournal $journalValue $JournalPath
    $session = New-WuaSession
    $session.ClientApplicationID = 'Windows Offline Update security'
    $searcher = $session.CreateUpdateSearcher()
    $searcher.Online = $inputRequest.mode -eq 'install'

    # Verification cannot create a downloader or installer, including on error paths.
    if ($inputRequest.mode -eq 'verify') {
        $journalValue.reboot_required = [bool]($sameBootRestart -or (Get-WuaRebootRequired))
        Write-SecurityJournal $journalValue $JournalPath
        foreach ($row in $rows) {
            try {
                $installed = Find-ExactSecurityUpdate $searcher $row $true
                $pending = if (!$installed) { Find-ExactSecurityUpdate $searcher $row $false } else { $null }
                $row.installed_proof = [bool]($installed -or (Test-IntelligenceVersion $row $journalValue.observed))
                $row.reboot_required = [bool]($installed.RebootRequired -or $pending.RebootRequired)
                if ($row.reboot_required) { $journalValue.reboot_required = $true }
                if ($row.installed_proof) {
                    $row.state = 'installed'
                    $row.verified = !$journalValue.reboot_required
                    $row.error = $null
                    if (!$row.verified) { $row.state = 'reboot-required'; $row.reboot_required = $true }
                } elseif (!$pending) {
                    $row.state = 'unavailable'
                    $row.error = 'The exact recorded revision is neither installed nor available. Run a fresh scan.'
                } elseif ($row.reboot_required) {
                    $row.state = 'reboot-required'
                } elseif ($row.state -notin @('failed', 'unavailable')) {
                    $row.state = 'pending'
                }
            } catch { $row.state = 'failed'; $row.error = $_.Exception.Message; $row.hresult = [int]$_.Exception.HResult }
            Write-SecurityJournal $journalValue $JournalPath
        }
        return $journalValue
    }

    if (Get-WuaRebootRequired) {
        $journalValue.reboot_required = $true
        Write-SecurityJournal $journalValue $JournalPath
        return $journalValue
    }
    $installer = $session.CreateUpdateInstaller()
    if ($installer.RebootRequiredBeforeInstallation) {
        $journalValue.reboot_required = $true
        Write-SecurityJournal $journalValue $JournalPath
        return $journalValue
    }
    $installer.IsForced = $false
    $installer.AllowSourcePrompts = $false
    $installer.ForceQuiet = $true
    foreach ($row in $rows) {
        try {
            if ($installer.IsBusy) { throw 'Windows Update is already installing another update.' }
            if ($installer.RebootRequiredBeforeInstallation -or (Get-WuaRebootRequired)) {
                $journalValue.reboot_required = $true
                Write-SecurityJournal $journalValue $JournalPath
                break
            }
            $installed = Find-ExactSecurityUpdate $searcher $row $true
            $update = if (!$installed) { Find-ExactSecurityUpdate $searcher $row $false } else { $null }
            if ($installed.RebootRequired -or $update.RebootRequired -or (Get-WuaRebootRequired)) {
                $row.state = 'reboot-required'; $row.reboot_required = $true
                $row.installed_proof = [bool]($installed -or (Test-IntelligenceVersion $row $journalValue.observed))
                $journalValue.reboot_required = $true
                Write-SecurityJournal $journalValue $JournalPath
                break
            }
            if ($installed -or (Test-IntelligenceVersion $row $journalValue.observed)) {
                $row.state = 'installed'; $row.verified = $true; $row.installed_proof = $true
                Write-SecurityJournal $journalValue $JournalPath
                continue
            }
            if (!$update) {
                $row.state = 'unavailable'
                $row.error = 'The exact requested revision is neither installed nor available. Run a fresh scan.'
                Write-SecurityJournal $journalValue $JournalPath
                break
            }
            if (!$update.EulaAccepted) { throw 'Windows Update reports unaccepted license terms for this security update.' }
            $collection = New-WuaUpdateCollection
            [void]$collection.Add($update)
            if (!$update.IsDownloaded) {
                $row.state = 'downloading'
                Write-SecurityJournal $journalValue $JournalPath
                $downloader = $session.CreateUpdateDownloader()
                $downloader.Updates = $collection
                $download = $downloader.Download()
                $item = $download.GetUpdateResult(0)
                $row.result_code = [int]$item.ResultCode; $row.hresult = [int]$item.HResult
                if ($download.ResultCode -ne 2 -or $item.ResultCode -ne 2 -or !$update.IsDownloaded) {
                    throw 'Windows Update did not completely download the exact security update.'
                }
                Write-SecurityJournal $journalValue $JournalPath
            }
            if ($installer.IsBusy) { throw 'Windows Update is already installing another update.' }
            if ($installer.RebootRequiredBeforeInstallation -or $update.RebootRequired -or (Get-WuaRebootRequired)) {
                $journalValue.reboot_required = $true
                Write-SecurityJournal $journalValue $JournalPath
                break
            }
            $installer.Updates = $collection
            $row.state = 'installing'; $row.result_code = $null; $row.hresult = $null
            Write-SecurityJournal $journalValue $JournalPath
            $result = $installer.Install()
            $item = $result.GetUpdateResult(0)
            $row.result_code = [int]$item.ResultCode; $row.hresult = [int]$item.HResult
            $row.reboot_required = [bool]($result.RebootRequired -or $item.RebootRequired -or $update.RebootRequired -or (Get-WuaRebootRequired))
            $journalValue.reboot_required = $row.reboot_required
            # Persist the install result before attempting any independent verification.
            $row.state = if ($row.reboot_required) {'reboot-required'} else {'installed'}
            if ($result.ResultCode -ne 2 -or $item.ResultCode -ne 2) {
                $row.state = 'failed'; $row.error = 'Windows Update did not report complete installation success.'
            }
            Write-SecurityJournal $journalValue $JournalPath
            $journalValue.observed = Get-SecurityObserved
            if ($row.state -ne 'failed' -and !$row.reboot_required) {
                $row.installed_proof = [bool]((Find-ExactSecurityUpdate $searcher $row $true) -or (Test-IntelligenceVersion $row $journalValue.observed))
                $row.verified = $row.installed_proof
                if (!$row.verified) { $row.state = 'failed'; $row.error = 'Installation returned success but the exact installed revision could not be verified.' }
            }
            Write-SecurityJournal $journalValue $JournalPath
        } catch {
            $row.state = 'failed'; $row.verified = $false; $row.error = $_.Exception.Message
            if ($null -eq $row.hresult) { $row.hresult = [int]$_.Exception.HResult }
            Write-SecurityJournal $journalValue $JournalPath
        }
        if ($row.state -in @('failed', 'unavailable') -or $journalValue.reboot_required) { break }
    }
    if (Get-WuaRebootRequired) {
        $journalValue.reboot_required = $true
        foreach ($row in $rows) {
            if ($row.state -eq 'installed') { $row.state = 'reboot-required'; $row.reboot_required = $true; $row.verified = $false }
        }
        Write-SecurityJournal $journalValue $JournalPath
    }
    return $journalValue
}

try {
    $result = Invoke-WindowsSecurityUpdates -RequestPath $Request -JournalPath $Journal
    $result | ConvertTo-Json -Depth 20 -Compress
    if (@($result.updates | Where-Object {$_.state -in @('failed', 'unavailable')}).Count) { exit 1 }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
