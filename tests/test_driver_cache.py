import asyncio
import json
from pathlib import Path

import httpx
import pytest

from win_offline_update import catalog, driver_cache
from win_offline_update.models import Update, UpdateFile
from win_offline_update.target import TargetIdentity
from win_offline_update.update import Offer, parse_offers, run_update

DRIVER_ID = "702e6ed7-4f31-4f3b-9f49-de333467baf8"
HARDWARE_ID = r"SWC\VEN_MSFT&AUDIO_EFFECTPACK_VOICECLARITY"
OFFER = Offer(
    DRIVER_ID,
    "Microsoft driver (1.0.4.55794)",
    "drivers",
    HARDWARE_ID,
    True,
    revision=1,
)
TARGET = TargetIdentity(
    "25H2", 26200, 9974, "x64", Path("C:/Windows"), "machine", "", "Client"
)


def test_scan_records_the_driver_revision() -> None:
    data = {
        "status": 2,
        "hardware": [HARDWARE_ID],
        "updates": [
            {
                "id": DRIVER_ID,
                "title": OFFER.title,
                "type": 2,
                "hardware": HARDWARE_ID,
                "revision": 1,
            }
        ],
    }
    offers, _ = parse_offers(json.dumps(data))
    assert offers == [OFFER]


@pytest.mark.parametrize("dry_run", [True, False])
def test_catalog_miss_uses_opt_in_cache_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    from win_offline_update.cli import main

    assert main(["update", "--help"]) == 0
    assert "--driver-cache" in capsys.readouterr().out
    monkeypatch.setattr(
        "win_offline_update.update.scan_offers", lambda _: ([OFFER], None)
    )
    called = []

    async def export(offer: Offer, identity: TargetIdentity, root: Path) -> Path:
        called.append(offer)
        assert identity == TARGET
        return root / DRIVER_ID

    monkeypatch.setattr(driver_cache, "export_driver", export)

    async def run() -> int:
        transport = httpx.MockTransport(lambda _: httpx.Response(404))
        async with httpx.AsyncClient(transport=transport) as client:
            return await run_update(
                client, TARGET, tmp_path, driver_cache_fallback=True, dry_run=dry_run
            )

    assert asyncio.run(run()) == 0
    assert called == ([] if dry_run else [OFFER])
    assert "Windows Update Agent" in capsys.readouterr().out


def test_cache_export_is_atomic_and_verified_on_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copies = []

    async def copy(offer: Offer, identity: TargetIdentity, payload: Path) -> list[str]:
        copies.append(offer)
        (payload / "offer.inf").write_bytes(b"test inf")
        (payload / "offer.cat").write_bytes(b"test cat")
        return ["offer.inf"]

    monkeypatch.setattr(driver_cache, "_copy_from_cache", copy)
    path = asyncio.run(driver_cache.export_driver(OFFER, TARGET, tmp_path))
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["update_id"] == DRIVER_ID
    assert manifest["update_revision"] == 1
    assert manifest["infs"] == ["offer.inf"]
    assert asyncio.run(driver_cache.export_driver(OFFER, TARGET, tmp_path)) == path
    assert copies == [OFFER]
    (path / "payload" / "offer.cat").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(driver_cache.export_driver(OFFER, TARGET, tmp_path))


def test_cache_export_does_not_publish_partial_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def copy(offer: Offer, identity: TargetIdentity, payload: Path) -> list[str]:
        (payload / "partial.inf").write_bytes(b"partial")
        raise ValueError("cache copy failed")

    monkeypatch.setattr(driver_cache, "_copy_from_cache", copy)
    with pytest.raises(ValueError, match="copy failed"):
        asyncio.run(driver_cache.export_driver(OFFER, TARGET, tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_native_failure_shows_a_single_candidate_for_identical_catalog_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    product = "Windows 11 Client, version 25H2 and later, Servicing Drivers"
    file = UpdateFile(
        "driver.cab", "https://download.windowsupdate.com/driver.cab", sha256="a" * 64
    )
    candidates = [
        Update(
            id,
            OFFER.title,
            product=product,
            architecture="x64",
            classification="Drivers",
            files=(file,),
        )
        for id in ids
    ]

    async def search(client: httpx.AsyncClient, query: str) -> list[Update]:
        assert query == HARDWARE_ID
        return candidates

    async def show(client: httpx.AsyncClient, id: str) -> Update:
        if id == DRIVER_ID:
            raise catalog.SourceError("No public entry")
        return next(candidate for candidate in candidates if candidate.id == id)

    async def fails(offer: Offer, identity: TargetIdentity, path: Path) -> Path:
        raise ValueError("WUA was unable to export the driver")

    monkeypatch.setattr(
        "win_offline_update.update.scan_offers", lambda _: ([OFFER], None)
    )
    monkeypatch.setattr(catalog, "search", search)
    monkeypatch.setattr(catalog, "show", show)
    monkeypatch.setattr(driver_cache, "export_driver", fails)

    async def run() -> int:
        async with httpx.AsyncClient() as client:
            return await run_update(
                client, TARGET, tmp_path, driver_cache_fallback=True
            )

    assert asyncio.run(run()) == 1
    output = capsys.readouterr().out
    assert "Catalog candidate" in output
    assert ids[0] in output and ids[1] not in output
    assert list(tmp_path.iterdir()) == []
