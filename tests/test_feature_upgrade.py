import argparse
import asyncio
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

from wuo import cli, update
from wuo import feature_upgrade as feature
from wuo.models import Update, UpdateFile
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


def test_plain_feature_title_can_refer_to_an_enablement_kb() -> None:
    raw = json.dumps(
        {
            "status": 2,
            "updates": [
                {
                    "id": OFFER.id,
                    "title": OFFER.title,
                    "type": 1,
                    "revision": 4,
                    "kb_articles": ["5121794", "KB5121794", ""],
                }
            ],
        }
    )
    offers, issue = update.parse_offers(raw)
    assert issue is None
    assert offers[0].kb_articles == ("KB5121794",)
    assert feature.offered_release(offers[0], TARGET) is None


def test_iso_preview_preserves_a_feature_offer_with_a_standalone_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    identity = replace(TARGET, windows=tmp_path / "Windows")
    previews = []
    monkeypatch.setattr(update, "scan_offers", lambda _: ([OFFER], None))

    async def package(
        _: AsyncClient, offer: Offer, installed: TargetIdentity
    ) -> Update:
        assert offer == OFFER
        assert installed == identity
        return Update(
            offer.id,
            offer.title,
            architecture="x64",
            files=(
                UpdateFile(
                    "enablement.cab",
                    "https://download.windowsupdate.com/enablement.cab",
                    sha256="a" * 64,
                ),
            ),
        )

    async def check(
        _: AsyncClient,
        installed: TargetIdentity,
        offers: list[Offer],
        media: feature.FeatureMedia,
    ) -> None:
        assert installed == identity
        # A separate ISO preview is allowed, but the CBS offer is not a Setup candidate.
        assert not offers
        previews.append(media.iso)

    monkeypatch.setattr(update, "_package", package)
    monkeypatch.setattr(feature, "check_feature", check)

    async def run() -> int:
        async with AsyncClient() as client:
            return await update.run_update(
                client,
                identity,
                tmp_path,
                dry_run=True,
                feature_media=feature.FeatureMedia(Path("windows.iso"), "a" * 64),
            )

    assert asyncio.run(run()) == 0
    assert "Would download enablement.cab" in capsys.readouterr().out
    assert previews == [Path("windows.iso")]


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
                "--dry-run",
                "--feature-iso",
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
    assert cli.main(["update", "--dry-run", "--feature-iso", "windows.iso"]) == 2


@pytest.mark.parametrize("scan_error", [False, True])
def test_explicit_iso_preview_without_a_wua_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scan_error: bool
) -> None:
    identity = replace(
        TARGET, windows=tmp_path / "Windows", machine_guid=str(UUID(int=5))
    )
    calls = []

    def scan(installed: TargetIdentity) -> tuple[list[Offer], None]:
        assert installed == identity
        if scan_error:
            raise OSError("WUA unavailable")
        return [], None

    def check_native(
        installed: TargetIdentity, iso: Path, digest: str, build: int | None
    ) -> None:
        assert installed == identity
        calls.append((iso, digest, build))

    def refuse(*args: object, **kwargs: object) -> None:
        pytest.fail("Feature preview attempted to stage or restart Windows")

    monkeypatch.setattr("wuo.target.read_target", lambda *, read_loader: identity)
    monkeypatch.setattr(feature, "read_target", lambda *, read_loader: identity)
    monkeypatch.setattr("wuo.update.scan_offers", scan)
    monkeypatch.setattr(feature, "_scan_media", check_native)
    monkeypatch.setattr(Menu, "resume", refuse)
    monkeypatch.setattr(Menu, "publish", refuse)
    arguments = [
        "update",
        "--dry-run",
        "--only",
        "features",
        "--feature-iso",
        "windows.iso",
        "--iso-sha256",
        "a" * 64,
        "--accept-setup-eula",
    ]
    assert cli.main(arguments) == 0
    assert calls == [(Path("windows.iso"), "a" * 64, None)]

    menu = Menu(identity)
    menu.root.mkdir(parents=True)
    (menu.root / "installed.id").write_text(identity.machine_guid, encoding="ascii")
    (menu.root / "pending.id").write_text(str(UUID(int=6)), encoding="ascii")
    assert cli.main(arguments) == 1
    assert len(calls) == 1


def test_preview_isolated_from_staging_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = replace(
        TARGET, windows=tmp_path / "Windows", machine_guid=str(UUID(int=5))
    )
    calls = []
    processed = []
    scans = []
    driver = Offer(str(UUID(int=7)), "Driver offer", "drivers")
    enablement = replace(OFFER, id=str(UUID(int=8)), title=OFFER.title + " (KB1234567)")

    def refuse(*args: object, **kwargs: object) -> None:
        pytest.fail("Feature preview attempted to stage or restart Windows")

    async def check(
        client: AsyncClient,
        installed: TargetIdentity,
        offers: list[Offer],
        media: feature.FeatureMedia,
    ) -> None:
        assert installed == identity
        assert offers == [OFFER]
        calls.append((media.iso, media.sha256, media.rollback))

    def scan(installed: TargetIdentity) -> tuple[list[Offer], None]:
        assert installed == identity
        scans.append(installed)
        return [OFFER, driver, enablement], None

    async def process(
        _client: object,
        _installed: object,
        _output: object,
        offer: Offer,
        **options: object,
    ) -> bool:
        assert options["dry_run"] is True
        candidates = options["setup_candidates"]
        if offer == OFFER and isinstance(candidates, list):
            candidates.append(offer)
            return True
        processed.append(offer)
        return True

    monkeypatch.setattr("wuo.target.read_target", lambda *, read_loader: identity)
    monkeypatch.setattr("wuo.update.scan_offers", scan)
    monkeypatch.setattr("wuo.update._process_offer", process)
    monkeypatch.setattr(Menu, "resume", refuse)
    monkeypatch.setattr(Menu, "publish", refuse)
    monkeypatch.setattr(feature, "check_feature", check)
    assert cli.main(["update"]) == 1
    assert processed == [OFFER, driver, enablement]
    assert scans == [identity]
    assert not calls
    processed.clear()
    scans.clear()
    arguments = [
        "update",
        "--dry-run",
        "--feature-iso",
        "windows.iso",
        "--iso-sha256",
        "a" * 64,
        "--accept-setup-eula",
        "--no-rollback",
    ]
    assert cli.main(arguments) == 0
    assert calls == [(Path("windows.iso"), "a" * 64, False)]
    assert scans == [identity]
    assert processed == [driver, enablement]

    assert cli.main([*arguments, "--only", "drivers"]) == 0
    assert processed == [driver, enablement, driver]
    assert len(calls) == 1
    assert cli.main([*arguments, "--only", "features"]) == 0
    assert processed == [driver, enablement, driver, enablement]
    assert len(calls) == 2

    menu = Menu(identity)
    menu.root.mkdir(parents=True)
    (menu.root / "installed.id").write_text(identity.machine_guid, encoding="ascii")
    (menu.root / "pending.id").write_text(str(UUID(int=6)), encoding="ascii")
    assert cli.main(arguments) == 1
    assert len(calls) == 2
    (menu.root / "pending.id").unlink()
    (menu.root / "windows-request.json").write_text("{}", encoding="ascii")
    assert cli.main(arguments) == 1
    assert len(calls) == 2


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required")
def test_scan_media_uses_native_modules_with_inherited_module_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_modules = tmp_path / "PowerShell" / "Modules"
    custom_modules.mkdir(parents=True)
    monkeypatch.setenv("PSMODULEPATH", str(custom_modules))
    monkeypatch.setattr(feature, "files", lambda _: tmp_path)
    (tmp_path / "feature_upgrade.ps1").write_text(
        """
$ErrorActionPreference = 'Stop'
$hash = Get-FileHash -LiteralPath $env:WUO_FEATURE_ISO -Algorithm SHA256
if ($hash.Hash -ne $env:WUO_FEATURE_SHA256) { throw 'Expected SHA-256 mismatch' }
Get-Command Get-AuthenticodeSignature, Get-DiskImage, Get-WindowsImage | Out-Null
@{
    setup_code = -1047526896
    index = 1
    edition = 'Core'
    architecture = $env:WUO_FEATURE_ARCH
    language = 'en-US'
    build = 26300
    revision = 9457
} | ConvertTo-Json -Compress
""",
        encoding="ascii",
    )
    iso = tmp_path / "fixture.iso"
    iso.write_bytes(b"fixture")
    identity = replace(TARGET, windows=Path(os.environ["SYSTEMROOT"]))
    image = feature._scan_media(
        identity, iso, hashlib.sha256(b"fixture").hexdigest(), None
    )
    assert image == feature.FeatureImage(
        index=1,
        build=26300,
        revision=9457,
        edition="Core",
        architecture="x64",
        language="en-US",
    )
    assert os.environ["PSMODULEPATH"] == str(custom_modules)


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
        ImageIndex=6; MajorVersion=10; MinorVersion=0; Build=[int]$env:WUO_TEST_IMAGE_BUILD; SPBuild=9457
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
    (
        "arch",
        "valid_hash",
        "setup_busy",
        "expected_build",
        "image_build",
        "expected_calls",
    ),
    [
        ("x64", True, False, "26300", 26300, ["mount", "scan", "dismount"]),
        ("x64", True, False, "", 26300, ["mount", "scan", "dismount"]),
        ("x64", True, False, "", 26200, ["mount", "dismount"]),
        ("x64", True, False, "", 26100, ["mount", "dismount"]),
        ("x64", True, False, "26300", 26400, ["mount", "dismount"]),
        ("arm64", True, False, "26300", 26300, ["mount", "dismount"]),
        ("x64", False, False, "26300", 26300, []),
        ("x64", True, True, "26300", 26300, []),
    ],
)
def test_native_check_validates_media_and_only_invokes_scan(
    tmp_path: Path,
    arch: str,
    valid_hash: bool,
    setup_busy: bool,
    expected_build: str,
    image_build: int,
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
            "WUO_FEATURE_BUILD": expected_build,
            "WUO_FEATURE_ARCH": arch,
            "WUO_TEST_SETUP_BUSY": str(int(setup_busy)),
            "WUO_TEST_IMAGE_BUILD": str(image_build),
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
        assert report["build"] == image_build
    else:
        assert result.returncode != 0
        expected_error = (
            "pending Windows"
            if setup_busy
            else ("one Windows" if valid_hash else "differs from")
        )
        assert expected_error in result.stderr
