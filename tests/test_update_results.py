import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from win_offline_update import cli, update
from win_offline_update.models import Update, UpdateFile
from win_offline_update.target import TargetIdentity


def test_download_only_updates_are_recorded_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    identity = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", "machine", ""
    )
    offer = update.Offer(
        "a32ca1d0-ddd4-486b-b708-d941db4f1165",
        "Windows Security platform 10.0.29680.1000",
        "security-platform",
    )
    package = Update(
        offer.id,
        offer.title,
        architecture="x64",
        kb="KB5007651",
        files=(
            UpdateFile(
                "securityhealthsetup.exe",
                "https://download.windowsupdate.com/securityhealthsetup.exe",
            ),
        ),
        route="Run the Microsoft installer in Windows.",
    )
    monkeypatch.setattr(update, "scan_offers", lambda _: ([offer], None))

    async def resolve(*_: object) -> Update:
        return package

    monkeypatch.setattr(update, "_package", resolve)
    prepared = update.PreparedUpdates(tmp_path / "prepared")

    async def run() -> int:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"payload")
            )
        ) as client:
            return await update.run_update(
                client, identity, tmp_path / "downloads", prepared=prepared
            )

    assert asyncio.run(run()) == 0
    assert not prepared.drivers and not prepared.monthly
    assert len(prepared.manual) == 1
    item = prepared.manual[0]
    assert item.category == "security-platform" and item.title == offer.title
    assert item.directory == tmp_path / "downloads" / offer.id
    output = capsys.readouterr().out
    assert "Downloaded only" in output and "requires installation" in output


def test_update_does_not_report_a_download_only_run_as_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    identity = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", "machine", ""
    )
    args = argparse.Namespace(
        only=[], dry_run=False, driver_cache=True, extractor="7z.exe"
    )
    menu = SimpleNamespace(root=tmp_path)

    async def scan(*_: object, **options: object) -> int:
        prepared = options["prepared"]
        assert isinstance(prepared, update.PreparedUpdates)
        prepared.manual.append(
            update.ManualUpdate(
                "security-platform",
                "Windows Security platform",
                tmp_path / "download",
                "Run the installer in Windows.",
            )
        )
        return 0

    monkeypatch.setattr(cli, "run_update", scan)

    async def run() -> int:
        async with httpx.AsyncClient() as client:
            return await cli._stage_update(
                client, args, identity, menu, True, tmp_path / "downloads", None
            )

    assert asyncio.run(run()) == 1
    output = capsys.readouterr().out
    assert "1 update(s) still require installation" in output
