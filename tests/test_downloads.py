import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest

BASE = "https://catalog.sf.dl.delivery.mp.microsoft.com/lab/"


def test_download_does_not_follow_off_host_redirect(tmp_path: Path) -> None:
    from wuo.downloads import DownloadError, download
    from wuo.models import Update, UpdateFile

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "catalog.sf.dl.delivery.mp.microsoft.com"
        return httpx.Response(302, headers={"Location": "https://example.com/payload"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(DownloadError, match="Microsoft"):
                await download(
                    client,
                    Update(
                        "test",
                        "Update",
                        files=(UpdateFile("update.msu", BASE + "update.msu"),),
                    ),
                    tmp_path,
                )

    asyncio.run(run())
    assert not (tmp_path / "manifest.json").exists()


def test_download_saves_all_verified_packages(tmp_path: Path) -> None:
    from wuo.downloads import download
    from wuo.models import Update, UpdateFile

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=request.url.path.encode("ascii"))

    files = tuple(
        UpdateFile(
            name,
            BASE + name,
            sha256=hashlib.sha256(f"/lab/{name}".encode()).hexdigest(),
        )
        for name in ("checkpoint.msu", "target.msu")
    )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await download(client, Update("test", "Update", files=files), tmp_path)

    asyncio.run(run())
    assert (tmp_path / "checkpoint.msu").read_bytes() == b"/lab/checkpoint.msu"
    assert (tmp_path / "target.msu").read_bytes() == b"/lab/target.msu"
    assert (tmp_path / "manifest.json").is_file()


def test_short_response_leaves_no_manifest(tmp_path: Path) -> None:
    from wuo.downloads import DownloadError, download
    from wuo.models import Update, UpdateFile

    async def run() -> None:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"Content-Length": "32"}, content=b"x"
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(DownloadError, match="length"):
                await download(
                    client,
                    Update(
                        "test",
                        "Update",
                        files=(UpdateFile("target.msu", BASE + "target.msu"),),
                    ),
                    tmp_path,
                )

    asyncio.run(run())
    assert list(tmp_path.iterdir()) == []


def test_lengthless_digestless_response_leaves_no_manifest(tmp_path: Path) -> None:
    from wuo.downloads import DownloadError, download
    from wuo.models import Update, UpdateFile

    async def run() -> None:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(b"x"))
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(DownloadError, match="length or digest"):
                update = Update(
                    "test",
                    "Update",
                    files=(UpdateFile("mpam-fe.exe", BASE + "mpam-fe.exe"),),
                )
                await download(client, update, tmp_path)

    asyncio.run(run())
    assert list(tmp_path.iterdir()) == []
