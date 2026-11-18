import importlib
from pathlib import Path

import pytest


@pytest.mark.parametrize(("arguments", "exit_code"), [(["--help"], 0), (["bad"], 2)])
def test_cli_exit_code(arguments: list[str], exit_code: int) -> None:
    cli = importlib.import_module("win_offline_update.cli")
    assert cli.main(arguments) == exit_code


def test_download_uses_a_default_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from win_offline_update import cli, downloads
    from win_offline_update.models import DownloadManifest, Update

    package = Update("example-update", "An example")
    destinations = []

    async def resolve(*_: object) -> Update:
        return package

    async def download(*args: object, **_: object) -> DownloadManifest:
        destinations.append(args[2])
        return DownloadManifest(package, ())

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "_resolve", resolve)
    monkeypatch.setattr(downloads, "download", download)

    assert cli.main(["download", package.id]) == 0
    assert destinations == [tmp_path / "Downloads/WindowsOfflineUpdates" / package.id]
