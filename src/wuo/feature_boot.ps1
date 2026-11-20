[CmdletBinding()]
param(
    [ValidateSet('Preview', 'Capture', 'Restore')][string]$Mode,
    [string]$JobPath
)

$ErrorActionPreference = 'Stop'

function Initialize-WuoBootNative {
    if ('WuoBootNative' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class WuoBootNative
{
    private const string EfiGlobal = "{8be4df61-93ca-11d2-aa0d-00e098032b8c}";

    [StructLayout(LayoutKind.Sequential)]
    private struct Luid
    {
        public uint LowPart;
        public int HighPart;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct TokenPrivileges
    {
        public uint Count;
        public Luid Luid;
        public uint Attributes;
    }

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentProcess();

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
    private static extern bool LookupPrivilegeValueW(string system, string name, out Luid luid);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool AdjustTokenPrivileges(IntPtr token, bool disableAll,
        ref TokenPrivileges requested, uint size, out TokenPrivileges previous, out uint returned);

    [DllImport("advapi32.dll", EntryPoint = "AdjustTokenPrivileges", SetLastError = true)]
    private static extern bool RestoreTokenPrivileges(IntPtr token, bool disableAll,
        ref TokenPrivileges previous, uint size, IntPtr unused, IntPtr returned);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
    private static extern uint GetFirmwareEnvironmentVariableExW(string name, string guid,
        [Out] byte[] data, uint length, out uint attributes);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
    private static extern bool SetFirmwareEnvironmentVariableExW(string name, string guid,
        byte[] data, uint length, uint attributes);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
    private static extern bool MoveFileExW(string from, string to, uint flags);

    private sealed class FirmwarePrivilege : IDisposable
    {
        private IntPtr token;
        private TokenPrivileges previous;

        public FirmwarePrivilege()
        {
            if (!OpenProcessToken(GetCurrentProcess(), 0x28, out token))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            try
            {
                Luid luid;
                if (!LookupPrivilegeValueW(null, "SeSystemEnvironmentPrivilege", out luid))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                TokenPrivileges requested = new TokenPrivileges { Count = 1, Luid = luid, Attributes = 2 };
                uint returned;
                bool adjusted = AdjustTokenPrivileges(token, false, ref requested,
                    (uint)Marshal.SizeOf(typeof(TokenPrivileges)), out previous, out returned);
                int error = Marshal.GetLastWin32Error();
                if (!adjusted || error != 0)
                    throw new Win32Exception(error);
            }
            catch
            {
                CloseHandle(token);
                throw;
            }
        }

        public void Dispose()
        {
            try
            {
                if (previous.Count != 0 && !RestoreTokenPrivileges(token, false, ref previous,
                    0, IntPtr.Zero, IntPtr.Zero))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            finally
            {
                CloseHandle(token);
            }
        }
    }

    public static byte[] Read(string name, out uint attributes)
    {
        using (new FirmwarePrivilege())
        {
            byte[] data = new byte[65536];
            uint length = GetFirmwareEnvironmentVariableExW(name, EfiGlobal, data,
                (uint)data.Length, out attributes);
            if (length == 0)
                throw new Win32Exception(Marshal.GetLastWin32Error());
            Array.Resize(ref data, (int)length);
            return data;
        }
    }

    public static void WriteBootOrder(byte[] data, uint attributes)
    {
        if (data.Length < 2 || data.Length % 2 != 0 || attributes != 7)
            throw new ArgumentException("Invalid BootOrder data or variable attributes.");
        using (new FirmwarePrivilege())
        {
            if (!SetFirmwareEnvironmentVariableExW("BootOrder", EfiGlobal, data,
                (uint)data.Length, attributes))
                throw new Win32Exception(Marshal.GetLastWin32Error());
        }
    }

    public static void ReplaceFile(string from, string to)
    {
        // Replace the file on the same volume and flush the completed move.
        if (!MoveFileExW(from, to, 0x1 | 0x8))
            throw new Win32Exception(Marshal.GetLastWin32Error());
    }
}
'@
}

function Get-WuoFirmwareVariable([string]$Name) {
    Initialize-WuoBootNative
    $attributes = [uint32]0
    try { $bytes = [WuoBootNative]::Read($Name, [ref]$attributes) }
    catch { throw "Could not read UEFI $Name. $($_.Exception.Message)" }
    return @{ bytes = $bytes; attributes = $attributes }
}

function Set-WuoBootOrder([byte[]]$Bytes, [uint32]$Attributes) {
    Initialize-WuoBootNative
    [WuoBootNative]::WriteBootOrder($Bytes, $Attributes)
}

function Move-WuoFile([string]$Source, [string]$Destination) {
    Initialize-WuoBootNative
    [WuoBootNative]::ReplaceFile($Source, $Destination)
}

function Get-WuoPathKind([string]$Path) {
    try { $attributes = [IO.File]::GetAttributes($Path) }
    catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] { return 'absent' }
    if ($attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Unexpected reparse point: $Path" }
    if ($attributes -band [IO.FileAttributes]::Directory) { return 'directory' }
    return 'file'
}

function Assert-WuoProtectedJob([string]$Path) {
    if ($Path -notmatch '^[A-Za-z]:\\' -or [IO.Path]::GetFullPath($Path) -ine $Path) {
        throw 'The boot preservation job requires an absolute local directory.'
    }
    for ($directory = [IO.DirectoryInfo]::new($Path); $null -ne $directory; $directory = $directory.Parent) {
        if ((Get-WuoPathKind $directory.FullName) -ne 'directory') {
            throw 'Could not verify the boot preservation job path.'
        }
    }
    $acl = Get-Acl -LiteralPath $Path
    $trusted = 'S-1-5-18', 'S-1-5-32-544'
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted -or !$rules.Count -or
        @($rules | Where-Object {
            $_.AccessControlType -ne 'Allow' -or $_.IdentityReference.Value -notin $trusted
        }).Count) {
        throw 'The boot preservation job must only grant access to SYSTEM and Administrators.'
    }
}

function Get-WuoEsp {
    $partitions = @(Get-Partition)
    $system = @($partitions | Where-Object {
        $_.IsSystem -and $_.GptType -eq '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
    })
    if ($system.Count -ne 1 -or !$system[0].Guid) { throw 'Could not identify the GPT system EFI partition.' }
    $id = [Guid]$system[0].Guid
    $paths = @($system[0].AccessPaths | Where-Object {
        $_ -match '^\\\\\?\\Volume\{[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\}\\$'
    })
    if ($id -eq [Guid]::Empty -or $paths.Count -ne 1 -or
        @($partitions | Where-Object { $_.Guid -eq $system[0].Guid }).Count -ne 1) {
        throw 'Could not verify a unique system EFI volume.'
    }
    return @{ id = $id.ToString('D'); root = $paths[0] }
}

function Get-WuoFallbackPath {
    switch ($env:PROCESSOR_ARCHITECTURE) {
        'AMD64' { return 'EFI\Boot\bootx64.efi' }
        'ARM64' { return 'EFI\Boot\bootaa64.efi' }
        default { throw 'Boot preservation requires native x64 or ARM64 PowerShell.' }
    }
}

function Get-WuoEspFile($Esp, [string]$Relative) {
    $components = $Relative.Split('\')
    $path = $Esp.root
    for ($index = 0; $index -lt $components.Length - 1; $index++) {
        $path += $components[$index] + '\'
        $kind = Get-WuoPathKind $path.TrimEnd('\')
        if ($kind -eq 'absent') { return $null }
        if ($kind -ne 'directory') { throw "Unexpected entry on the system EFI partition: $path" }
    }
    $path += $components[-1]
    $kind = Get-WuoPathKind $path
    if ($kind -eq 'absent') { return $null }
    if ($kind -ne 'file') { throw "Expected an EFI file: $path" }
    return $path
}

function Get-WuoHash([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-WuoFileHash([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $file = [IO.File]::OpenRead($Path)
        try { return [BitConverter]::ToString($sha.ComputeHash($file)).Replace('-', '').ToLowerInvariant() }
        finally { $file.Dispose() }
    } finally { $sha.Dispose() }
}

function Test-WuoWindowsBootMetadata([string]$Path) {
    $metadata = [Diagnostics.FileVersionInfo]::GetVersionInfo($Path)
    return ($metadata.CompanyName -ieq 'Microsoft Corporation' -and
        $metadata.OriginalFilename -imatch '\A(bootmgr\.exe|bootmgr\.efi|bootmgfw\.efi)(\.mui)?\z')
}

function Test-WuoEfiImage([byte[]]$Bytes, [string]$RelativePath) {
    if ($Bytes.Length -lt 160 -or [BitConverter]::ToUInt16($Bytes, 0) -ne 0x5A4D) { return $false }
    $pe = [BitConverter]::ToInt32($Bytes, 60)
    if ($pe -lt 64 -or $pe -gt $Bytes.Length - 94 -or
        [BitConverter]::ToUInt32($Bytes, $pe) -ne 0x00004550) { return $false }
    $machine = if ($RelativePath.EndsWith('bootaa64.efi')) { 0xAA64 } else { 0x8664 }
    $optionalSize = [BitConverter]::ToUInt16($Bytes, $pe + 20)
    return ([BitConverter]::ToUInt16($Bytes, $pe + 4) -eq $machine -and
        $optionalSize -ge 70 -and $optionalSize -le ($Bytes.Length - $pe - 24) -and
        [BitConverter]::ToUInt16($Bytes, $pe + 24) -eq 0x20B -and
        [BitConverter]::ToUInt16($Bytes, $pe + 92) -eq 10)
}

function Get-WuoWindowsBootHash($Esp) {
    $efi = Get-WuoEspFile $Esp 'EFI\Microsoft\Boot\bootmgfw.efi'
    if (!$efi -or !(Test-WuoWindowsBootMetadata $efi)) {
        throw 'Could not identify Windows Boot Manager on the system EFI partition.'
    }
    $hash = Get-WuoFileHash $efi
    $signature = Get-AuthenticodeSignature -LiteralPath $efi
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or
        $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -ne 'Microsoft Windows') {
        throw 'Could not verify the Microsoft signature on Windows Boot Manager.'
    }
    if ((Get-WuoFileHash $efi) -cne $hash) { throw 'Windows Boot Manager changed while verifying its signature.' }
    return $hash
}

function Get-WuoBootEntry([string]$Id) {
    $data = (Get-WuoFirmwareVariable $Id).bytes
    if ($data.Length -lt 8) { throw "Malformed UEFI entry $Id." }
    $descriptionEnd = 6
    while ($descriptionEnd + 1 -lt $data.Length -and
        [BitConverter]::ToUInt16($data, $descriptionEnd) -ne 0) { $descriptionEnd += 2 }
    $deviceStart = $descriptionEnd + 2
    if ($deviceStart -gt $data.Length -or
        $deviceStart + [BitConverter]::ToUInt16($data, 4) -gt $data.Length) { throw "Malformed UEFI entry $Id." }
    $label = [Text.Encoding]::Unicode.GetString($data, 6, [Math]::Min($descriptionEnd - 6, 512))
    $label = ($label -replace '\p{C}', ' ').Trim()
    # Fingerprint the load attributes, device path and optional data. Labels can change independently.
    $identity = [byte[]]::new($data.Length - $deviceStart + 6)
    [Array]::Copy($data, 0, $identity, 0, 6)
    [Array]::Copy($data, $deviceStart, $identity, 6, $data.Length - $deviceStart)
    return @{ id = $Id; identity = (Get-WuoHash $identity); label = $label }
}

function Get-WuoBootState {
    $order = Get-WuoFirmwareVariable 'BootOrder'
    if ($order.attributes -ne 7 -or $order.bytes.Length -lt 2 -or
        $order.bytes.Length % 2 -ne 0 -or $order.bytes.Length -gt 8192) {
        throw 'The UEFI BootOrder variable has an unsupported format or attributes.'
    }
    $unique = [Collections.Generic.HashSet[string]]::new()
    $entries = for ($offset = 0; $offset -lt $order.bytes.Length; $offset += 2) {
        $id = 'Boot{0:X4}' -f [BitConverter]::ToUInt16($order.bytes, $offset)
        if (!$unique.Add($id)) { throw 'The UEFI boot order contains duplicate entries.' }
        Get-WuoBootEntry $id
    }
    return @{ attributes = $order.attributes; entries = @($entries) }
}

function Get-WuoOrderPlan($Saved, $Current) {
    $byId = @{}
    foreach ($entry in $Current) { $byId[$entry.id] = $entry }
    $seen = [Collections.Generic.HashSet[string]]::new()
    foreach ($entry in $Saved) {
        $match = $byId[$entry.id]
        if ($null -ne $match -and $match.identity -cne $entry.identity) {
            throw "UEFI entry $($entry.id) points to a different loader."
        }
        if ($null -eq $match) {
            $matches = @($Current | Where-Object { $_.identity -ceq $entry.identity })
            if ($matches.Count -ne 1) { throw "UEFI entry $($entry.id) is missing or ambiguous." }
            $match = $matches[0]
        }
        if (!$seen.Add($match.id)) { throw "UEFI entry $($entry.id) is ambiguous." }
        $match.id
    }
    foreach ($entry in $Current) {
        if ($seen.Add($entry.id)) { $entry.id }
    }
}

function Write-WuoNewFile([string]$Path, [byte[]]$Bytes) {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($Bytes, 0, $Bytes.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }
}

function Write-WuoResult([string]$Path, $Report) {
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $json = $Report | ConvertTo-Json -Depth 6 -Compress
        Write-WuoNewFile $temporary ([Text.Encoding]::UTF8.GetBytes($json))
        Move-WuoFile $temporary $Path
    } finally { [IO.File]::Delete($temporary) }
}

function Restore-WuoOrder($Snapshot, $Esp) {
    $current = Get-WuoBootState
    try { $desired = @(Get-WuoOrderPlan $Snapshot.entries $current.entries) }
    catch { return @{ status = 'blocked'; detail = $_.Exception.Message } }
    if (($current.entries.id -join ',') -ceq ($desired -join ',')) {
        return @{ status = 'unchanged'; detail = 'The original UEFI boot order is already in place.' }
    }
    $fresh = Get-WuoBootState
    if ((Get-WuoEsp).id -cne $Esp.id -or $fresh.attributes -ne $current.attributes -or
        ($fresh.entries.id -join ',') -cne ($current.entries.id -join ',') -or
        ($fresh.entries.identity -join ',') -cne ($current.entries.identity -join ',')) {
        return @{ status = 'blocked'; detail = 'The EFI partition or boot entries changed while checking the boot order.' }
    }
    $bytes = [Collections.Generic.List[byte]]::new()
    foreach ($id in $desired) { $bytes.AddRange([BitConverter]::GetBytes([Convert]::ToUInt16($id.Substring(4), 16))) }
    Set-WuoBootOrder $bytes.ToArray() $fresh.attributes
    if (((Get-WuoBootState).entries.id -join ',') -cne ($desired -join ',')) {
        throw 'Firmware readback differs from the requested boot order.'
    }
    return @{ status = 'restored'; detail = 'Restored and verified the original UEFI boot order.' }
}

function Restore-WuoFallback($Snapshot, $Esp, [string]$SavedFile) {
    if ($Snapshot.fallback_state -eq 'absent') {
        return @{ status = 'absent'; detail = 'The original EFI partition did not have a fallback loader.' }
    }
    if ($Snapshot.fallback_state -eq 'windows') {
        return @{ status = 'unchanged'; detail = 'The original EFI fallback was a Windows loader.' }
    }
    if ((Get-WuoPathKind $SavedFile) -ne 'file' -or (Get-WuoFileHash $SavedFile) -cne $Snapshot.fallback_sha256) {
        throw 'Could not verify the saved EFI fallback loader.'
    }
    $destination = Get-WuoEspFile $Esp $Snapshot.fallback_path
    if (!$destination) { return @{ status = 'skipped'; detail = 'The current EFI fallback is missing.' } }
    $currentHash = Get-WuoFileHash $destination
    if ($currentHash -ceq $Snapshot.fallback_sha256) {
        return @{ status = 'unchanged'; detail = 'The saved EFI fallback loader is already in place.' }
    }
    try { $windowsHash = Get-WuoWindowsBootHash $Esp }
    catch { return @{ status = 'skipped'; detail = "Could not verify the replacement EFI fallback. $($_.Exception.Message)" } }
    if ($currentHash -cne $windowsHash) {
        return @{ status = 'skipped'; detail = 'The replacement EFI fallback was not identified as Windows Boot Manager.' }
    }
    $temporary = "$destination.wuo-$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $saved = [IO.File]::ReadAllBytes($SavedFile)
        if ((Get-WuoHash $saved) -cne $Snapshot.fallback_sha256) { throw 'The saved EFI fallback changed while reading it.' }
        Write-WuoNewFile $temporary $saved
        $verified = Get-WuoEsp
        if ($verified.id -cne $Esp.id -or $verified.root -ine $Esp.root -or
            (Get-WuoEspFile $verified $Snapshot.fallback_path) -cne $destination -or
            (Get-WuoFileHash $destination) -cne $windowsHash) {
            return @{ status = 'blocked'; detail = 'The EFI partition or fallback changed before replacement.' }
        }
        Move-WuoFile $temporary $destination
        if ((Get-WuoFileHash $destination) -cne $Snapshot.fallback_sha256) {
            throw 'The restored EFI fallback does not match the saved image.'
        }
    } finally { [IO.File]::Delete($temporary) }
    return @{ status = 'restored'; detail = 'Restored and verified the original EFI fallback loader.' }
}

function Invoke-WuoBootPreservation {
    param(
        [Parameter(Mandatory)][ValidateSet('Preview', 'Capture', 'Restore')][string]$Mode,
        [string]$JobPath
    )
    $esp = Get-WuoEsp
    $relative = Get-WuoFallbackPath
    if ($Mode -ne 'Preview') {
        Assert-WuoProtectedJob $JobPath
        $receipt = Join-Path $JobPath 'boot-order.json'
        $savedFile = Join-Path $JobPath 'boot-fallback.efi'
    }
    if ($Mode -eq 'Restore') {
        $resultPath = Join-Path $JobPath 'boot-order-result.json'
        if ((Get-WuoPathKind $receipt) -ne 'file' -or (Get-WuoPathKind $resultPath) -eq 'directory') {
            throw 'The boot preservation receipt or result path is invalid.'
        }
        $snapshot = [IO.File]::ReadAllText($receipt) | ConvertFrom-Json
        $seen = [Collections.Generic.HashSet[string]]::new()
        if ($snapshot.schema -ne 1 -or $snapshot.esp -cne $esp.id -or $snapshot.fallback_path -cne $relative -or
            !$snapshot.entries.Count -or $snapshot.entries.Count -gt 4096 -or
            $snapshot.fallback_state -cnotin @('absent', 'windows', 'eligible') -or
            ($snapshot.fallback_state -ceq 'eligible' -and $snapshot.fallback_sha256 -cnotmatch '^[0-9a-f]{64}$')) {
            throw 'The boot preservation receipt does not match this EFI partition and architecture.'
        }
        foreach ($entry in $snapshot.entries) {
            if ($entry.id -cnotmatch '^Boot[0-9A-F]{4}$' -or $entry.identity -cnotmatch '^[0-9a-f]{64}$' -or
                !$seen.Add($entry.id)) { throw 'The boot preservation receipt has invalid firmware entries.' }
        }
        try { $fallback = Restore-WuoFallback $snapshot $esp $savedFile }
        catch { $fallback = @{ status = 'failed'; detail = $_.Exception.Message } }
        if ($snapshot.fallback_state -eq 'eligible' -and $fallback.status -notin 'restored', 'unchanged') {
            $order = @{ status = 'blocked'; detail = 'Verify the original EFI fallback before restoring the firmware order.' }
        } else {
            try { $order = Restore-WuoOrder $snapshot $esp }
            catch { $order = @{ status = 'failed'; detail = $_.Exception.Message } }
        }
        $report = @{
            schema = 1; order = $order.status; order_detail = $order.detail
            fallback = $fallback.status; fallback_detail = $fallback.detail
        }
        Write-WuoResult $resultPath $report
        return $report
    }

    $order = Get-WuoBootState
    $fallback = Get-WuoEspFile $esp $relative
    $state = 'absent'
    $fallbackHash = $null
    $bytes = $null
    if ($fallback) {
        $size = [IO.FileInfo]::new($fallback).Length
        if ($size -lt 1 -or $size -gt 16MB) {
            throw 'The EFI fallback loader has an unsupported size.'
        }
        $bytes = [IO.File]::ReadAllBytes($fallback)
        $fallbackHash = Get-WuoHash $bytes
        if (Test-WuoWindowsBootMetadata $fallback) {
            $state = 'windows'
            $fallbackHash = $null
        } else {
            if (!(Test-WuoEfiImage $bytes $relative)) {
                throw 'The original EFI fallback is not a native EFI application.'
            }
            $state = 'eligible'
        }
    }
    $snapshot = @{
        schema = 1; esp = $esp.id; entries = $order.entries
        fallback_path = $relative; fallback_state = $state; fallback_sha256 = $fallbackHash
    }
    if ($Mode -eq 'Capture') {
        if ((Get-WuoPathKind $receipt) -ne 'absent' -or (Get-WuoPathKind $savedFile) -ne 'absent') {
            throw 'The job already contains a boot preservation snapshot.'
        }
        if ($state -eq 'eligible') { Write-WuoNewFile $savedFile $bytes }
        $json = $snapshot | ConvertTo-Json -Depth 6 -Compress
        Write-WuoNewFile $receipt ([Text.Encoding]::UTF8.GetBytes($json))
    }
    return $snapshot
}

if ($MyInvocation.InvocationName -ne '.') {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    Invoke-WuoBootPreservation -Mode $Mode -JobPath $JobPath | ConvertTo-Json -Depth 6 -Compress
}
