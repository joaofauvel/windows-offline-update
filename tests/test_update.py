import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from win_offline_update import catalog, downloads
from win_offline_update.models import Update, UpdateFile
from win_offline_update.target import TargetIdentity

DRIVER = "11111111-1111-1111-1111-111111111111"
FEATURE = "22222222-2222-2222-2222-222222222222"
TARGET = TargetIdentity(
    "25H2", 26200, 9457, "x64", Path("C:/Windows"), "machine", "", "Client"
)


def test_update_help_accepts_the_new_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from win_offline_update.cli import main

    assert main(["update", "--help"]) == 0
    assert "--only" in capsys.readouterr().out


def test_scan_identifies_drivers_and_localized_feature_offers() -> None:
    from win_offline_update.update import parse_offers

    offers, issue = parse_offers(
        '{"status":3,"updates":['
        f'{{"id":"{DRIVER}","title":"Intel driver","type":2,"categories":[]}},'
        f'{{"id":"{FEATURE}","title":"Nouvelle version","type":1,'
        '"categories":["3689BDC8-B205-4AF4-8D4A-A63924C5E9D5"]}]}'
    )
    assert [(offer.id, offer.category) for offer in offers] == [
        (DRIVER, "drivers"),
        (FEATURE, "features"),
    ]
    assert issue and "partial" in issue.lower()


def test_update_dry_run_respects_category_filters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from win_offline_update.update import Offer, run_update

    def scan(_: TargetIdentity) -> tuple[list[Offer], str | None]:
        return [
            Offer(DRIVER, "Intel driver", "drivers", r"PCI\VEN_8086", True),
            Offer(FEATURE, "Windows upgrade", "features"),
        ], None

    async def show(_: httpx.AsyncClient, update_id: str) -> Update:
        assert update_id == DRIVER
        return Update(
            DRIVER,
            "Intel driver",
            architecture="x64",
            files=(
                UpdateFile(
                    "driver.cab", "https://download.windowsupdate.com/driver.cab"
                ),
            ),
        )

    def refuse(*_: object, **__: object) -> None:
        pytest.fail("Dry run attempted a download")

    monkeypatch.setattr("win_offline_update.update.scan_offers", scan)
    monkeypatch.setattr(catalog, "show", show)
    monkeypatch.setattr(downloads, "download", refuse)
    out = tmp_path / "downloads"

    async def run() -> int:
        async with httpx.AsyncClient() as client:
            return await run_update(
                client, TARGET, out, only=("drivers",), dry_run=True
            )

    assert asyncio.run(run()) == 0
    assert "driver.cab" in capsys.readouterr().out
    assert not out.exists()


def test_update_processes_all_categories_and_keeps_manual_next_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from win_offline_update.update import Offer, run_update

    def scan(_: TargetIdentity) -> tuple[list[Offer], str | None]:
        return [
            Offer(DRIVER, "Intel driver", "drivers", r"PCI\VEN_8086", True),
            Offer(FEATURE, "Windows upgrade", "features"),
        ], None

    async def show(_: httpx.AsyncClient, update_id: str) -> Update:
        if update_id == FEATURE:
            raise catalog.SourceError("No full standalone package")
        return Update(
            DRIVER,
            "Intel driver",
            architecture="x64",
            route="Review the matching signed INF.",
            files=(
                UpdateFile(
                    "driver.cab", "https://download.windowsupdate.com/driver.cab"
                ),
            ),
        )

    monkeypatch.setattr("win_offline_update.update.scan_offers", scan)
    monkeypatch.setattr(catalog, "show", show)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"fixture"))

    async def run() -> int:
        async with httpx.AsyncClient(transport=transport) as client:
            return await run_update(client, TARGET, tmp_path)

    assert asyncio.run(run()) == 1
    assert (tmp_path / DRIVER / "driver.cab").read_bytes() == b"fixture"
    result = capsys.readouterr()
    assert "signed INF" in result.out
    assert "software-download/windows11" in result.out


def test_driver_hardware_match_is_exact_and_case_insensitive() -> None:
    import json

    from win_offline_update.update import parse_offers

    update = {
        "id": DRIVER,
        "title": "Intel driver",
        "type": 2,
        "hardware": r"PCI\VEN_8086&DEV_1234",
    }

    def scan(hardware: str) -> bool:
        offers, _ = parse_offers(
            json.dumps({"status": 2, "updates": [update], "hardware": [hardware]})
        )
        return offers[0].hardware_verified

    assert scan(r"pci\ven_8086&dev_1234")
    assert not scan(r"PCI\VEN_8086&DEV_123")


def test_unpublished_driver_reports_hardware_id_and_manufacturer_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from win_offline_update.update import Offer, run_update

    hardware_id = r"PCI\VEN_8086&DEV_1234"
    offer = Offer(DRIVER, "Intel driver (1.2.3.4)", "drivers", hardware_id, True)
    monkeypatch.setattr(
        "win_offline_update.update.scan_offers", lambda _: ([offer], None)
    )

    async def run() -> int:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"Location": "/Thanks.aspx?id=190"})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            return await run_update(client, TARGET, tmp_path / "updates", dry_run=True)

    assert asyncio.run(run()) == 1
    summary = capsys.readouterr().out
    assert hardware_id in summary
    assert "manufacturer's signed package for Windows 25H2 x64" in summary
    assert not (tmp_path / "updates").exists()


def test_architecture_and_windows_release_checked_before_download() -> None:
    from win_offline_update.update import Offer, validate_applicability

    driver = Offer(DRIVER, "Intel driver", "drivers", r"PCI\VEN_8086", True)
    mixed = Update(
        DRIVER,
        "Intel driver",
        files=(
            UpdateFile("driver-arm64.cab", "https://download.windowsupdate.com/a"),
            UpdateFile("driver-amd64.cab", "https://download.windowsupdate.com/b"),
        ),
    )
    assert [
        file.name for file in validate_applicability(driver, mixed, TARGET).files
    ] == ["driver-amd64.cab"]
    opaque = replace(mixed, files=(replace(mixed.files[0], name="driver.cab"),))
    with pytest.raises(ValueError, match="architecture"):
        validate_applicability(driver, opaque, TARGET)
    with pytest.raises(ValueError, match="driver version"):
        validate_applicability(
            replace(driver, title="Intel driver (1.2.3.4)"),
            replace(opaque, title="Intel driver (1.2.3.5)", architecture="x64"),
            TARGET,
        )
    with pytest.raises(ValueError, match="Windows release"):
        validate_applicability(
            driver,
            replace(
                opaque,
                architecture="x64",
                product="Windows 11 Client, version 26H2 and later, Servicing Drivers",
            ),
            TARGET,
        )
    with pytest.raises(ValueError, match="hardware"):
        validate_applicability(Offer(DRIVER, "Intel driver", "drivers"), mixed, TARGET)
    with pytest.raises(ValueError, match="release"):
        validate_applicability(
            Offer(FEATURE, "Windows 11, version 24H2", "monthly"),
            Update(FEATURE, "Windows 11, version 24H2", architecture="x64"),
            TARGET,
        )


def test_missing_catalog_offer_resolves_only_the_matching_monthly_kb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from win_offline_update.update import _package, parse_offers

    offer = parse_offers(
        '{"status":2,"updates":[{"type":1,'
        f'"id":"{FEATURE}","title":"2026-11 Security Update (KB5132061) (26200.9974)"'
        "}]}"
    )[0][0]
    assert offer.category == "monthly"
    file = UpdateFile(
        "windows11.0-kb5132061-x64.msu", "https://download.windowsupdate.com/cu.msu"
    )
    title = "Cumulative Update for Windows 11, version 25H2 (KB5132061) (26200.9974)"
    candidates = [
        Update(DRIVER, title, "Windows 11", "arm64", kb="KB5132061"),
        Update(
            DRIVER, title.replace("25H2", "24H2"), "Windows 11", "x64", kb="KB5132061"
        ),
        Update(
            DRIVER,
            title.replace("Cumulative", "Dynamic Cumulative"),
            "Windows 11",
            "x64",
            kb="KB5132061",
        ),
        Update(DRIVER, title, "Windows 11", "x64", kb="KB5132061", files=(file,)),
    ]

    async def show(_: httpx.AsyncClient, update_id: str) -> Update:
        if update_id == FEATURE:
            raise catalog.SourceError("Not published")
        assert update_id == DRIVER
        return candidates[-1]

    async def search(_: httpx.AsyncClient, kb: str) -> list[Update]:
        assert kb == "KB5132061"
        return candidates

    monkeypatch.setattr(catalog, "search", search)
    monkeypatch.setattr(catalog, "show", show)

    async def run(identity: TargetIdentity = TARGET) -> Update:
        async with httpx.AsyncClient() as client:
            return await _package(client, offer, identity)

    assert asyncio.run(run()).files == (file,)
    with pytest.raises(ValueError, match="No unique standalone"):
        asyncio.run(run(replace(TARGET, installation_type="Server")))


def test_security_platform_uses_the_payload_named_by_the_agent() -> None:
    from win_offline_update.update import Offer, validate_applicability

    platform = UpdateFile(
        "securityhealthsetup_a.exe", "https://download.windowsupdate.com/p/a.exe"
    )
    other = UpdateFile(
        "securityhealthsetup_b.exe", "https://download.windowsupdate.com/p/b.exe"
    )
    package = Update(FEATURE, "Windows Security platform", files=(platform, other))
    offer = Offer(
        FEATURE,
        "Windows Security platform",
        "security-platform",
        payload_names=(platform.name,),
    )
    assert validate_applicability(offer, package, TARGET).files == (platform,)
    with pytest.raises(ValueError, match="architecture"):
        validate_applicability(
            Offer(FEATURE, offer.title, offer.category), package, TARGET
        )


@pytest.mark.parametrize("release", ["1.463.347.0", "1.463.348.0"])
def test_defender_fallback_proves_the_redirect_version(release: str) -> None:
    from win_offline_update.update import Offer, _defender_package

    title = (
        "Security Intelligence for Microsoft Defender (KB2267602) (Version 1.463.347.0)"
    )
    offer = Offer(DRIVER, title, "defender")
    location = f"https://definitionupdates.microsoft.com/packages/content/mpam-fe.exe?packageType=Signatures&packageVersion={release}&arch=amd64"

    def redirect(request: httpx.Request) -> httpx.Response:
        assert request.method == "HEAD"
        return httpx.Response(302, headers={"Location": location})

    async def run() -> Update:
        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect)) as client:
            return await _defender_package(client, offer, TARGET)

    if release == "1.463.347.0":
        assert asyncio.run(run()).files[0].url == location
    else:
        with pytest.raises(ValueError, match="offered version and architecture"):
            asyncio.run(run())


def test_reuse_checks_the_existing_manifest_and_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from win_offline_update.update import Offer, run_update

    offer = Offer(DRIVER, "Intel driver", "drivers", r"PCI\VEN_8086", True)
    standalone = Update(
        DRIVER,
        offer.title,
        architecture="x64",
        files=(
            UpdateFile("driver.cab", "https://download.windowsupdate.com/driver.cab"),
        ),
    )
    monkeypatch.setattr(
        "win_offline_update.update.scan_offers", lambda _: ([offer], None)
    )

    async def show(_: httpx.AsyncClient, __: str) -> Update:
        return standalone

    monkeypatch.setattr(catalog, "show", show)

    async def run(transport: httpx.MockTransport) -> int:
        async with httpx.AsyncClient(transport=transport) as client:
            return await run_update(client, TARGET, tmp_path)

    def refuse(_: httpx.Request) -> httpx.Response:
        pytest.fail("An existing manifest triggered a download")

    def redirect(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/driver.cab":
            return httpx.Response(302, headers={"Location": "/resolved.cab"})
        return httpx.Response(200, content=b"fixture")

    assert asyncio.run(run(httpx.MockTransport(redirect))) == 0
    assert asyncio.run(run(httpx.MockTransport(refuse))) == 0
    assert "Reused verified packages" in capsys.readouterr().out
    (tmp_path / DRIVER / "driver.cab").write_bytes(b"changed")
    assert asyncio.run(run(httpx.MockTransport(refuse))) == 1
    assert "Cannot verify existing package" in capsys.readouterr().out
