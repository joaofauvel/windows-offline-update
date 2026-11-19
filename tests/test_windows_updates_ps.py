"""Exercise the PowerShell orchestration with AST-loaded functions and fake WUA.

The production script's entrypoint is never executed and every native helper is
replaced before orchestration, so these tests cannot install host updates.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "src/win_offline_update/windows_updates.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
TARGET = {
    "machine_guid": "test-machine",
    "build": 26200,
    "revision": 9974,
    "architecture": "x64",
}
IDS = [
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222",
    "33333333-3333-3333-3333-333333333333",
]


def offer(index: int, kb: str, version: str) -> dict:
    return {
        "id": IDS[index],
        "revision": 1,
        "kb": kb,
        "title": f"Security update ({version})",
        "category": "defender",
    }


HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:WUO_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($fn in $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $false)) {
    . ([scriptblock]::Create($fn.Extent.Text))
}
$script:Calls = [Collections.Generic.List[string]]::new()
$script:Fixtures = Get-Content -LiteralPath $env:WUO_FIXTURES -Raw | ConvertFrom-Json
$script:Installed = @{}
$script:Offers = @{}
foreach ($record in $script:Fixtures.updates) {
    if ($script:Fixtures.missing_id -eq $record.id) { continue }
    $script:Offers[$record.id] = [pscustomobject]@{
        Identity = [pscustomobject]@{UpdateID=$record.id;RevisionNumber=$record.revision}
        KBArticleIDs = @($record.kb.Substring(2)); Title=$record.title; Type=1
        IsDownloaded=$false; EulaAccepted=($script:Fixtures.unaccepted_id -ne $record.id)
        RebootRequired=($script:Fixtures.update_reboot_id -eq $record.id)
    }
}
foreach ($id in $script:Fixtures.installed) { $script:Installed[$id] = $true }
function Assert-WindowsNativeAdministrator {}
function Get-WindowsTargetIdentity { [pscustomobject]@{machine_guid='test-machine';build=$(if($script:Fixtures.build){$script:Fixtures.build}else{26200});revision=9974;architecture='x64'} }
function Get-WindowsBootId { 'boot-now' }
function Get-WuaRebootRequired {
    return [bool]($script:Fixtures.global_reboot -or ($script:Fixtures.post_global_reboot -and @($script:Calls | Where-Object { $_.StartsWith('install:') }).Count))
}
function Get-SecurityObserved { [ordered]@{AntivirusSignatureVersion=$script:Fixtures.signature;AMProductVersion='4.18.1.0';AMEngineVersion='1.1.1.0';SecurityAppVersion='1000.1.0.0'} }
function New-WuaUpdateCollection {
    $collection = [pscustomobject]@{Items=[Collections.Generic.List[object]]::new()}
    $collection | Add-Member ScriptMethod Add {param($update) $this.Items.Add($update); return 0}
    return $collection
}
function New-WuaSession {
    $session = [pscustomobject]@{ClientApplicationID=''}
    $session | Add-Member ScriptMethod CreateUpdateSearcher {
        $searcher = [pscustomobject]@{Online=$false}
        $searcher | Add-Member ScriptMethod Search {
            param($criteria)
            $script:Calls.Add('search:' + $criteria)
            $id = [regex]::Match($criteria, "UpdateID='([^']+)'").Groups[1].Value
            $installedQuery = $criteria.Contains('IsInstalled=1')
            $isInstalled = $script:Installed.ContainsKey($id)
            $items = @()
            if ($script:Offers.ContainsKey($id) -and $installedQuery -eq $isInstalled) { $items = @($script:Offers[$id]) }
            $collection = [pscustomobject]@{Count=$items.Count;Items=$items}
            $collection | Add-Member ScriptMethod Item {param($index) return $this.Items[$index]}
            return [pscustomobject]@{ResultCode=2;Updates=$collection}
        }
        return $searcher
    }
    $session | Add-Member ScriptMethod CreateUpdateDownloader {
        $downloader = [pscustomobject]@{Updates=$null}
        $downloader | Add-Member ScriptMethod Download {
            $update = $this.Updates.Items[0]
            $script:Calls.Add('download:' + $update.Identity.UpdateID)
            $prior = Get-Content -LiteralPath $env:WUO_JOURNAL -Raw | ConvertFrom-Json
            if (($prior.updates | Where-Object id -eq $update.Identity.UpdateID).state -ne 'downloading') { throw 'No durable downloading journal' }
            $update.IsDownloaded=$true
            $result = [pscustomobject]@{ResultCode=2}
            $result | Add-Member ScriptMethod GetUpdateResult {param($index) [pscustomobject]@{ResultCode=2;HResult=0}}
            return $result
        }
        return $downloader
    }
    $session | Add-Member ScriptMethod CreateUpdateInstaller {
        $script:Calls.Add('installer')
        $installer = [pscustomobject]@{Updates=$null;IsBusy=$false;RebootRequiredBeforeInstallation=[bool]$script:Fixtures.pre_reboot;IsForced=$false;ForceQuiet=$false;AllowSourcePrompts=$true}
        $installer | Add-Member ScriptMethod Install {
            if ($this.IsForced -or $this.AllowSourcePrompts -or !$this.ForceQuiet) { throw 'Unsafe installer interaction settings' }
            $update = $this.Updates.Items[0]
            $id=$update.Identity.UpdateID
            $script:Calls.Add('install:' + $id)
            $prior = Get-Content -LiteralPath $env:WUO_JOURNAL -Raw | ConvertFrom-Json
            if (($prior.updates | Where-Object id -eq $id).state -ne 'installing') { throw 'No durable installing journal' }
            $failed = $script:Fixtures.fail_id -eq $id
            if (!$failed) { $script:Installed[$id]=$true }
            if ($script:Fixtures.post_update_reboot) { $update.RebootRequired=$true }
            $result = [pscustomobject]@{ResultCode=$(if($failed){4}else{2});HResult=$(if($failed){-1}else{0});RebootRequired=[bool]$script:Fixtures.post_reboot}
            $result | Add-Member ScriptMethod GetUpdateResult {param($index) return $this}
            return $result
        }
        return $installer
    }
    return $session
}
try {
    Invoke-WindowsSecurityUpdates -RequestPath $env:WUO_REQUEST -JournalPath $env:WUO_JOURNAL | Out-Null
    $failure = $null
} catch { $failure = $_.Exception.Message }
$journal = if (Test-Path -LiteralPath $env:WUO_JOURNAL) { Get-Content -LiteralPath $env:WUO_JOURNAL -Raw | ConvertFrom-Json } else { $null }
@{calls=@($script:Calls);journal=$journal;failure=$failure} | ConvertTo-Json -Depth 20 -Compress
"""


def run_script(
    tmp_path: Path, updates: list[dict], *, mode="install", previous=None, **fixtures
) -> dict:
    assert SCRIPT.is_file(), "The native Windows security installer is missing"
    if not POWERSHELL:
        pytest.skip("PowerShell is required for AST-isolated native script tests")
    request = {
        "schema_version": 1,
        "request_id": "44444444-4444-4444-4444-444444444444",
        "mode": mode,
        "target": TARGET,
        "updates": updates,
    }
    if previous:
        request.update(previous)
    request_path = tmp_path / "request.json"
    fixture_path = tmp_path / "fixtures.json"
    journal_path = tmp_path / "journal.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    fixture_path.write_text(
        json.dumps(
            {"updates": updates, "installed": [], "signature": None, **fixtures}
        ),
        encoding="utf-8",
    )
    harness_path = tmp_path / "harness.ps1"
    harness_path.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(harness_path)],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "WUO_SCRIPT": str(SCRIPT),
            "WUO_REQUEST": str(request_path),
            "WUO_FIXTURES": str(fixture_path),
            "WUO_JOURNAL": str(journal_path),
        },
    )
    return json.loads(result.stdout)


def test_installs_only_exact_security_offers_in_platform_app_intelligence_order(
    tmp_path,
):
    updates = [
        offer(2, "KB2267602", "1.463.360.0"),
        offer(1, "KB5007651", "10.0.29680.1000"),
        offer(0, "KB4052623", "4.18.26100.4"),
    ]
    result = run_script(tmp_path, updates)
    assert result["failure"] is None
    assert result["journal"]["request_id"] == "44444444-4444-4444-4444-444444444444"
    assert [call for call in result["calls"] if call.startswith("install:")] == [
        f"install:{id}" for id in IDS
    ]
    assert all(
        item["state"] == "installed" and item["verified"]
        for item in result["journal"]["updates"]
    )
    assert all(
        "RevisionNumber=1" in call
        and "Type='Software'" in call
        and "IsHidden=0" in call
        for call in result["calls"]
        if call.startswith("search:")
    )


def test_verify_never_constructs_installer_or_downloads_and_keeps_same_boot_restart(
    tmp_path,
):
    update = offer(2, "KB2267602", "1.463.360.0")
    result = run_script(
        tmp_path,
        [update],
        mode="verify",
        previous={"previous_boot_id": "boot-now", "previous_reboot_required": True},
        installed=[update["id"]],
    )
    assert result["failure"] is None
    assert not any(
        call == "installer" or call.startswith(("install:", "download:"))
        for call in result["calls"]
    )
    assert result["journal"]["reboot_required"] is True
    assert result["journal"]["updates"][0]["verified"] is False


def test_initial_restart_barrier_preserves_all_pending_work(tmp_path):
    result = run_script(
        tmp_path, [offer(1, "KB5007651", "10.0.29680.1000")], pre_reboot=True
    )
    assert result["journal"]["reboot_required"] is True
    assert result["journal"]["updates"][0]["state"] == "pending"
    assert not any(
        call.startswith(("install:", "download:")) for call in result["calls"]
    )


@pytest.mark.parametrize("restart_source", ["global", "installed", "pending"])
def test_install_blocks_current_global_or_exact_update_restart(
    tmp_path, restart_source
):
    update = offer(1, "KB5007651", "10.0.29680.1000")
    fixtures = (
        {"global_reboot": True}
        if restart_source == "global"
        else {"update_reboot_id": IDS[1]}
    )
    if restart_source == "installed":
        fixtures["installed"] = [IDS[1]]
    result = run_script(tmp_path, [update], **fixtures)
    assert result["journal"]["reboot_required"] is True
    assert result["journal"]["updates"][0]["verified"] is False
    assert not any(
        call.startswith(("install:", "download:")) for call in result["calls"]
    )


@pytest.mark.parametrize("restart_source", ["global", "update"])
def test_install_rechecks_live_restart_after_native_result(tmp_path, restart_source):
    fixtures = (
        {"post_global_reboot": True}
        if restart_source == "global"
        else {"post_update_reboot": True}
    )
    result = run_script(
        tmp_path, [offer(1, "KB5007651", "10.0.29680.1000")], **fixtures
    )
    row = result["journal"]["updates"][0]
    assert row["result_code"] == 2 and row["verified"] is False
    assert (
        row["state"] == "reboot-required"
        and result["journal"]["reboot_required"] is True
    )


def test_failure_is_persisted_and_stops_later_work(tmp_path):
    updates = [
        offer(1, "KB5007651", "10.0.29680.1000"),
        offer(2, "KB2267602", "1.463.360.0"),
    ]
    result = run_script(tmp_path, updates, fail_id=IDS[1])
    assert result["journal"]["updates"][0]["state"] == "failed"
    assert result["journal"]["updates"][0]["result_code"] == 4
    assert result["journal"]["updates"][1]["state"] == "pending"
    assert f"install:{IDS[2]}" not in result["calls"]


def test_restart_after_install_stops_later_work_until_another_boot(tmp_path):
    updates = [
        offer(1, "KB5007651", "10.0.29680.1000"),
        offer(2, "KB2267602", "1.463.360.0"),
    ]
    result = run_script(tmp_path, updates, post_reboot=True)
    first, second = result["journal"]["updates"]
    assert first["state"] == "reboot-required" and first["result_code"] == 2
    assert first["verified"] is False and result["journal"]["reboot_required"] is True
    assert second["state"] == "pending"
    assert f"install:{IDS[2]}" not in result["calls"]


def test_verify_after_new_boot_preserves_result_and_allows_newer_windows(tmp_path):
    update = {
        **offer(1, "KB5007651", "10.0.29680.1000"),
        "state": "reboot-required",
        "result_code": 2,
        "hresult": 0,
        "reboot_required": True,
    }
    result = run_script(
        tmp_path,
        [update],
        mode="verify",
        previous={"previous_boot_id": "older-boot", "previous_reboot_required": True},
        installed=[IDS[1]],
        build=26201,
    )
    row = result["journal"]["updates"][0]
    assert row["verified"] is True and row["result_code"] == 2 and row["hresult"] == 0
    assert result["journal"]["reboot_required"] is False
    assert not any(
        call == "installer" or call.startswith(("install:", "download:"))
        for call in result["calls"]
    )


def test_verify_reconciles_installed_update_and_preserves_failed_install_result(
    tmp_path,
):
    update = {
        **offer(1, "KB5007651", "10.0.29680.1000"),
        "state": "failed",
        "result_code": 4,
        "hresult": -1,
        "error": "The earlier installation failed.",
    }
    result = run_script(tmp_path, [update], mode="verify", installed=[IDS[1]])
    row = result["journal"]["updates"][0]
    assert row["verified"] is True and row["installed_proof"] is True
    assert row["result_code"] == 4 and row["hresult"] == -1


@pytest.mark.parametrize("restart_source", ["global", "update"])
def test_verify_preserves_current_restart_requirement_after_another_boot(
    tmp_path, restart_source
):
    fixtures = (
        {"global_reboot": True}
        if restart_source == "global"
        else {"update_reboot_id": IDS[1]}
    )
    result = run_script(
        tmp_path,
        [offer(1, "KB5007651", "10.0.29680.1000")],
        mode="verify",
        previous={
            "previous_boot_id": "older-boot",
            "previous_reboot_required": True,
        },
        installed=[IDS[1]],
        **fixtures,
    )
    row = result["journal"]["updates"][0]
    assert result["journal"]["reboot_required"] is True
    assert row["state"] == "reboot-required" and row["verified"] is False
    assert row["installed_proof"] is True
    assert not any(
        call == "installer" or call.startswith(("install:", "download:"))
        for call in result["calls"]
    )


def test_verify_does_not_trust_copied_installation_proof(tmp_path):
    update = {
        **offer(1, "KB5007651", "10.0.29680.1000"),
        "state": "installed",
        "verified": True,
        "installed_proof": True,
    }
    result = run_script(tmp_path, [update], mode="verify")
    row = result["journal"]["updates"][0]
    assert row["verified"] is False and row["installed_proof"] is False


@pytest.mark.parametrize("mode", ["install", "verify"])
def test_native_journal_echoes_hook_transaction_identity(tmp_path, mode):
    hook_id = "55555555-5555-5555-5555-555555555555"
    result = run_script(
        tmp_path,
        [offer(1, "KB5007651", "10.0.29680.1000")],
        mode=mode,
        previous={"hooks_job_id": hook_id},
    )
    assert result["failure"] is None
    assert result["journal"]["hooks_job_id"] == hook_id


def test_native_request_rejects_invalid_hook_transaction_identity(tmp_path):
    result = run_script(
        tmp_path,
        [offer(1, "KB5007651", "10.0.29680.1000")],
        previous={"hooks_job_id": "bad-job-id"},
    )
    assert result["failure"] and result["calls"] == []


def test_install_rejects_changed_windows_baseline_before_wua(tmp_path):
    result = run_script(
        tmp_path, [offer(1, "KB5007651", "10.0.29680.1000")], build=26201
    )
    assert result["failure"] and result["calls"] == []


@pytest.mark.parametrize("mode", ["install", "verify"])
def test_missing_exact_offer_is_unavailable_without_substitution(tmp_path, mode):
    result = run_script(
        tmp_path,
        [offer(1, "KB5007651", "10.0.29680.1000")],
        mode=mode,
        missing_id=IDS[1],
    )
    assert result["journal"]["updates"][0]["state"] == "unavailable"
    assert result["journal"]["updates"][0]["error"]
    assert not any(
        call.startswith(("install:", "download:")) for call in result["calls"]
    )


def test_unaccepted_license_is_not_accepted_or_installed(tmp_path):
    result = run_script(
        tmp_path, [offer(1, "KB5007651", "10.0.29680.1000")], unaccepted_id=IDS[1]
    )
    row = result["journal"]["updates"][0]
    assert row["state"] == "failed" and "unaccepted" in row["error"]
    assert not any(
        call.startswith(("install:", "download:")) for call in result["calls"]
    )


def test_verify_accepts_newer_intelligence_without_installing_a_substitute(tmp_path):
    result = run_script(
        tmp_path,
        [offer(2, "KB2267602", "1.463.360.0")],
        mode="verify",
        signature="1.463.400.0",
    )
    assert result["journal"]["updates"][0]["verified"] is True
    assert result["journal"]["observed"]["AntivirusSignatureVersion"] == "1.463.400.0"
    assert not any(call.startswith("install:") for call in result["calls"])


@pytest.mark.parametrize(
    "changed",
    [{"kb": "KB1234567"}, {"id": "not-a-guid"}, {"revision": "1"}, {"title": 5}],
)
def test_rejects_invalid_requests_before_creating_wua(tmp_path, changed):
    update = {**offer(2, "KB2267602", "1.463.360.0"), **changed}
    result = run_script(tmp_path, [update])
    assert result["failure"]
    assert result["calls"] == []
