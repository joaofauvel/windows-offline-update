import argparse
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient

from wuo import cli
from wuo import feature_upgrade as feature
from wuo.target import TargetIdentity
from wuo.update import Offer
from wuo.winre_menu import Menu

TARGET = TargetIdentity(
    "25H2", 26200, 9457, "x64", Path("C:/Windows"), "machine", "", "Client"
)
OFFER = Offer(str(UUID(int=1)), "Windows 11, version 26H2", "features", revision=4)
POWERSHELL = shutil.which("powershell.exe")
SCRIPT = Path(__file__).parents[1] / "src/wuo/feature_upgrade.ps1"


def test_only_one_new_full_feature_offer_is_eligible() -> None:
    irrelevant = [
        replace(OFFER, title="Windows 11, version 25H2"),
        replace(OFFER, title="Windows 11, version 26H2 (KB1234567)"),
        replace(OFFER, category="monthly"),
        replace(OFFER, revision=None),
        replace(OFFER, title="Windows 11, version 26H2 or 27H2"),
    ]
    assert feature.select_offer([*irrelevant, OFFER], TARGET) == (OFFER, "26H2")
    with pytest.raises(ValueError, match="No eligible"):
        feature.select_offer(irrelevant, TARGET)
    with pytest.raises(ValueError, match="Multiple"):
        feature.select_offer([OFFER, replace(OFFER, id=str(UUID(int=2)))], TARGET)


def test_published_build_must_be_unique_and_match_explicit_offer_build() -> None:
    history = "<p><strong>Version 26H2 (OS build 26300)</strong></p>"
    assert feature.release_build(history * 2, OFFER, "26H2", TARGET) == 26300
    with pytest.raises(ValueError, match="confirm"):
        feature.release_build(
            history + history.replace("26300", "26301"), OFFER, "26H2", TARGET
        )
    with pytest.raises(ValueError, match="different"):
        feature.release_build(
            history, replace(OFFER, title=OFFER.title + " (26350.1000)"), "26H2", TARGET
        )


@pytest.mark.parametrize(
    ("code", "message"),
    [(0, "0x00000000"), (0xC1900204, "keep apps and data"), (0xC190020E, "space")],
)
def test_only_setup_scan_success_is_accepted(code: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        feature.require_scan_success(code)
    feature.require_scan_success(0xC1900210 - 2**32)


@pytest.mark.parametrize("flag", [(), ("--no-rollback",)])
def test_cli_plans_rollback_by_default(
    flag: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run(args: argparse.Namespace) -> int:
        assert args.dry_run
        assert args.feature_rollback is (not flag)
        return 0

    monkeypatch.setattr(cli, "_run", run)
    assert (
        cli.main(
            [
                "update",
                "--check-feature",
                "windows.iso",
                "--iso-sha256",
                "a" * 64,
                "--accept-setup-eula",
                *flag,
            ]
        )
        == 0
    )
    assert cli.main(["update", "--no-rollback"]) == 2
    assert cli.main(["update", "--check-feature", "windows.iso"]) == 2


def test_preview_isolated_from_staging_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = replace(
        TARGET, windows=tmp_path / "Windows", machine_guid=str(UUID(int=5))
    )
    calls = []

    def refuse(*args: object, **kwargs: object) -> None:
        pytest.fail("Feature preview attempted to stage or restart Windows")

    async def check(
        client: AsyncClient,
        installed: TargetIdentity,
        iso: Path,
        digest: str,
        *,
        rollback: bool,
    ) -> int:
        assert installed == identity
        calls.append((iso, digest, rollback))
        return 0

    monkeypatch.setattr("wuo.target.read_target", lambda *, read_loader: identity)
    monkeypatch.setattr(cli, "_stage_update", refuse)
    monkeypatch.setattr(Menu, "resume", refuse)
    monkeypatch.setattr(feature, "check_feature", check)
    arguments = [
        "update",
        "--check-feature",
        "windows.iso",
        "--iso-sha256",
        "a" * 64,
        "--accept-setup-eula",
        "--no-rollback",
    ]
    assert cli.main(arguments) == 0
    assert calls == [(Path("windows.iso"), "a" * 64, False)]

    menu = Menu(identity)
    menu.root.mkdir(parents=True)
    (menu.root / "installed.id").write_text(identity.machine_guid, encoding="ascii")
    (menu.root / "pending.id").write_text(str(UUID(int=6)), encoding="ascii")
    assert cli.main(arguments) == 1
    assert len(calls) == 1
    (menu.root / "pending.id").unlink()
    (menu.root / "windows-request.json").write_text("{}", encoding="ascii")
    assert cli.main(arguments) == 1
    assert len(calls) == 1


HARNESS = r"""
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Utility
$letter = 90..68 | ForEach-Object { [string][char]$_ } |
    Where-Object { !(Get-PSDrive -Name $_ -ErrorAction SilentlyContinue) } |
    Select-Object -First 1
New-PSDrive -Name $letter -PSProvider FileSystem -Root $env:WUO_FIXTURE | Out-Null
function Get-ItemProperty {
    param($Path, $Name, $ErrorAction)
    if ($Path -like '*CurrentVersion') { return @{CurrentBuildNumber=26200; UBR=9457} }
    if ($Name -eq 'SetupProgress') {
        if ($env:WUO_TEST_SETUP_BUSY -eq '1') { return @{SetupProgress=0} }
        return
    }
    return @{SystemSetupInProgress=0}
}
function New-Object { param($ComObject) @{RebootRequired=$false} }
function Test-Path {
    param($Path, $LiteralPath, $PathType)
    if ($Path -like 'HKLM:*') { return $false }
    Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath -PathType $PathType
}
function Get-DiskImage { param($ImagePath, $StorageType) @{Attached=$false} }
function Mount-DiskImage {
    param($ImagePath, $StorageType, $Access, [switch]$PassThru)
    if ($StorageType -ne 'ISO' -or $Access -ne 'ReadOnly') { throw 'Wrong access' }
    Add-Content $env:WUO_CALLS mount
    @{Attached=$true}
}
function Dismount-DiskImage {
    param($ImagePath, $StorageType)
    Add-Content $env:WUO_CALLS dismount
    [pscustomobject]@{ImagePath=$ImagePath; Attached=$false}
}
function Get-Volume { [pscustomobject]@{DriveLetter=$letter} }
function Get-AuthenticodeSignature {
    param($LiteralPath)
    $name = [Security.Cryptography.X509Certificates.X500DistinguishedName]::new('CN=Microsoft Windows, O=Microsoft Corporation')
    $signer = [pscustomobject]@{ SubjectName=$name }
    @{Status='Valid'; SignerCertificate=$signer}
}
function Get-WindowsEdition { param([switch]$Online) @{Edition='Professional'} }
function Mock-Dism {
    $global:LASTEXITCODE = 0
    'Default system UI language : en-US'
}
Set-Alias "$env:SystemRoot\System32\dism.exe" Mock-Dism
function Get-WindowsImage {
    param($ImagePath, $Index)
    if ($Index -eq 0) { return @{ImageIndex=6} }
    return @{
        ImageIndex=6; MajorVersion=10; MinorVersion=0; Build=26300; SPBuild=9457
        InstallationType='Client'; Architecture=9; EditionId='Professional'
        DefaultLanguageIndex=0; Languages=@('en-US')
    }
}
function Start-Process {
    param($FilePath, $WorkingDirectory, [switch]$Wait, [switch]$PassThru, $ArgumentList)
    if ($FilePath -ne "${letter}:\setup.exe" -or !$Wait -or !$PassThru) { throw 'Wrong Setup invocation' }
    Add-Content $env:WUO_CALLS ($ArgumentList -join ' ')
    return @{ExitCode=-1047526896}
}
& $env:WUO_SCRIPT
"""


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required")
@pytest.mark.parametrize(
    ("arch", "valid_hash", "setup_busy", "expected_calls"),
    [
        ("x64", True, False, ["mount", "scan", "dismount"]),
        ("arm64", True, False, ["mount", "dismount"]),
        ("x64", False, False, []),
        ("x64", True, True, []),
    ],
)
def test_native_check_validates_media_and_only_invokes_scan(
    tmp_path: Path,
    arch: str,
    valid_hash: bool,
    setup_busy: bool,
    expected_calls: list[str],
) -> None:
    assert POWERSHELL is not None
    fixture = tmp_path / "media"
    (fixture / "sources").mkdir(parents=True)
    (fixture / "sources/install.wim").touch()
    iso = tmp_path / "fixture.iso"
    iso.write_bytes(b"fixture")
    calls = tmp_path / "calls.txt"
    harness = tmp_path / "harness.ps1"
    harness.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            harness,
        ],
        env={
            **os.environ,
            "WUO_SCRIPT": str(SCRIPT),
            "WUO_FIXTURE": str(fixture),
            "WUO_CALLS": str(calls),
            "WUO_FEATURE_ISO": str(iso),
            "WUO_FEATURE_SHA256": hashlib.sha256(b"fixture").hexdigest()
            if valid_hash
            else "0" * 64,
            "WUO_FEATURE_SOURCE": "26200.9457",
            "WUO_FEATURE_BUILD": "26300",
            "WUO_FEATURE_ARCH": arch,
            "WUO_TEST_SETUP_BUSY": str(int(setup_busy)),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    setup = "/Auto Upgrade /Quiet /Compat ScanOnly /DynamicUpdate Disable /EULA Accept /ImageIndex 6"
    expected = [setup if call == "scan" else call for call in expected_calls]
    assert (calls.read_text().splitlines() if calls.exists() else []) == expected, (
        result.stderr
    )
    if "scan" in expected_calls:
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        feature.require_scan_success(report["setup_code"])
        assert report["build"] == 26300
    else:
        assert result.returncode != 0
        expected_error = (
            "pending Windows"
            if setup_busy
            else ("one Windows build" if valid_hash else "differs from")
        )
        assert expected_error in result.stderr
