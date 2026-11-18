$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Use an elevated Windows terminal to download and verify a driver from the Windows Update cache.'
}
$id = [guid]$request.update_id
$revision = [int]$request.update_revision
$session = New-Object -ComObject Microsoft.Update.Session
$session.ClientApplicationID = 'Windows Offline Update'
$searcher = $session.CreateUpdateSearcher()
$searcher.Online = $true
$result = $searcher.Search("UpdateID='$id' and RevisionNumber=$revision and Type='Driver' and IsInstalled=0 and IsHidden=0")
if ($result.ResultCode -ne 2 -or $result.Updates.Count -ne 1) {
    throw 'The exact pending driver revision could not be confirmed. Run a fresh scan.'
}
$update = $result.Updates.Item(0)
if ([guid]$update.Identity.UpdateID -ne $id -or $update.Identity.RevisionNumber -ne $revision -or
    $update.Type -ne 2 -or $update.DriverHardwareID -ne $request.hardware_id) {
    throw 'The driver offer changed during discovery.'
}
$hardware = @(Get-CimInstance Win32_PnPEntity -Filter 'Present=TRUE' | ForEach-Object {
    $_.HardwareID
    $_.CompatibleID
})
if ($hardware -notcontains $request.hardware_id) {
    throw 'The driver hardware ID no longer matches a present device.'
}
if (!$update.EulaAccepted) { throw 'Windows Update reports unaccepted license terms for this driver.' }

if (!$update.IsDownloaded) {
    $collection = New-Object -ComObject Microsoft.Update.UpdateColl
    [void]$collection.Add($update)
    $downloader = $session.CreateUpdateDownloader()
    $downloader.Updates = $collection
    $download = $downloader.Download()
    $item = $download.GetUpdateResult(0)
    if ($download.ResultCode -ne 2 -or $item.ResultCode -ne 2 -or !$update.IsDownloaded) {
        throw ('Windows Update Agent did not finish downloading the driver (HRESULT 0x{0:X8}).' -f [int]$item.HResult)
    }
}

$destination = [string]$request.destination
if ($update.BundledUpdates.Count) {
    for ($i = 0; $i -lt $update.BundledUpdates.Count; $i++) {
        $component = Join-Path $destination "component-$i"
        New-Item -ItemType Directory -Path $component | Out-Null
        $update.BundledUpdates.Item($i).CopyFromCache($component, $false)
    }
} else {
    $update.CopyFromCache($destination, $false)
}

$architecture = @{x86=0; x64=9; arm64=12}[$request.architecture]
$version = [regex]::Match([string]$update.Title, '\b\d+(?:\.\d+){3}\b').Value
$matching = @(
    foreach ($inf in Get-ChildItem -LiteralPath $destination -Filter '*.inf' -File -Recurse) {
        $drivers = Get-WindowsDriver -Online -Driver $inf.FullName
        $match = $drivers | Where-Object {
            $_.HardwareId -eq $request.hardware_id -and $_.Architecture -eq $architecture -and
            (!$version -or [string]$_.Version -eq $version)
        } | Select-Object -First 1
        if ($match) { $inf.FullName.Substring($destination.Length + 1).Replace('\', '/') }
    }
)
if (!$matching.Count) {
    throw 'The cached files do not include an INF matching the offered hardware, architecture, and version.'
}
@{ infs = $matching } | ConvertTo-Json -Compress
