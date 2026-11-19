"""Download Microsoft packages without publishing a partial manifest."""

import asyncio
import hashlib
import json
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .models import DownloadedFile, DownloadManifest, Update, UpdateFile

HOSTS = {
    "definitionupdates.microsoft.com",
    "download.microsoft.com",
    "go.microsoft.com",
    "download.windowsupdate.com",
    "dl.delivery.mp.microsoft.com",
}
HOST_SUFFIXES = (".download.windowsupdate.com", ".dl.delivery.mp.microsoft.com")


class DownloadError(ValueError):
    """A transfer or its integrity checks failed."""


def checked_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or (host not in HOSTS and not host.endswith(HOST_SUFFIXES))
    ):
        raise DownloadError(
            f"Download URL is outside the approved Microsoft hosts: {url}"
        )
    return url


def catalog_download_url(url: str) -> str:
    # Older Catalog records may publish an HTTP link to the same HTTPS-enabled CDN.
    return checked_url("https://" + url[7:] if url.startswith("http://") else url)


def safe_name(name: str) -> bool:
    reserved = r"(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)"
    return (
        re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._+~ -]{0,199}", name) is not None
        and not name.endswith((".", " "))
        and re.match(reserved, name, re.IGNORECASE) is None
        and name.casefold() != "manifest.json"
    )


def package_hashes(path: Path) -> tuple[str, str]:
    sha256 = hashlib.sha256()
    sha1 = hashlib.sha1(usedforsecurity=False)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            sha256.update(chunk)
            sha1.update(chunk)
    return sha256.hexdigest(), sha1.hexdigest()


def matches_digest(file: UpdateFile, sha256: str, sha1: str) -> bool:
    return (file.sha256 is None or file.sha256 == sha256) and (
        file.sha1 is None or file.sha1 == sha1
    )


def verify_download(update: Update, destination: Path) -> None:
    """Recheck a completed download before the update workflow uses it again."""
    manifest_path = destination / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise DownloadError(f"No regular download manifest in {destination}.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    try:
        same_update = (
            manifest["schema_version"] == 1 and manifest["update"]["id"] == update.id
        )
        records = {record["name"]: record for record in manifest["files"]}
        original = {
            record["name"]: record["url"] for record in manifest["update"]["files"]
        }
        listed = {file.name for file in update.files}
    except (KeyError, TypeError) as error:
        raise DownloadError(f"Invalid download manifest in {destination}.") from error
    if (
        not same_update
        or records.keys() != listed
        or len(manifest["files"]) != len(update.files)
    ):
        raise DownloadError(
            f"The downloaded files in {destination} differ from the offered package set."
        )
    for file in update.files:
        path = destination / file.name
        if (
            not safe_name(file.name)
            or path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(destination.resolve())
        ):
            raise DownloadError(f"Missing or unsafe package: {path}")
        sha256, sha1 = package_hashes(path)
        record = records[file.name]
        if (
            original.get(file.name) != file.url
            or record.get("size") != path.stat().st_size
            or record.get("sha256") != sha256
            or not matches_digest(file, sha256, sha1)
        ):
            raise DownloadError(f"Cannot verify existing package: {path}")


def atomic_json(path: Path, value: object) -> None:
    with tempfile.NamedTemporaryFile(
        dir=path.parent, mode="w", encoding="utf-8", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.close()
            temporary.replace(path)
        finally:
            stream.close()
            temporary.unlink(missing_ok=True)


def _publish_file(temporary: Path, target: Path) -> None:
    if target.exists():
        raise DownloadError(f"Output file appeared during transfer: {target}")
    temporary.rename(target)


async def _transfer(
    client: httpx.AsyncClient, file: UpdateFile, target: Path
) -> DownloadedFile:
    url = catalog_download_url(file.url)
    for _ in range(6):
        async with client.stream(
            "GET",
            checked_url(url),
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        ) as response:
            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise DownloadError(f"Missing redirect target for {file.name}.")
                url = urljoin(url, location)
                continue
            response.raise_for_status()
            if (
                response.status_code != 200
                or response.headers.get("Content-Encoding", "identity") != "identity"
                or response.headers.get("Content-Type", "").lower().startswith("text/")
            ):
                raise DownloadError(f"Unexpected payload response for {file.name}.")
            length = response.headers.get("Content-Length")
            if length is None and file.sha256 is None and file.sha1 is None:
                raise DownloadError(
                    f"No download length or digest was provided for {file.name}."
                )
            sha256 = hashlib.sha256()
            sha1 = hashlib.sha1(usedforsecurity=False)
            size = 0
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                        stream.write(chunk)
                        sha256.update(chunk)
                        sha1.update(chunk)
                        size += len(chunk)
                    if size == 0 or (
                        length is not None
                        and (not length.isdecimal() or size != int(length))
                    ):
                        raise DownloadError(
                            f"Unexpected download length for {file.name}."
                        )
                    if not matches_digest(file, sha256.hexdigest(), sha1.hexdigest()):
                        raise DownloadError(
                            f"Microsoft digest mismatch for {file.name}."
                        )
                    stream.close()
                    await asyncio.to_thread(_publish_file, temporary, target)
                finally:
                    stream.close()
                    await asyncio.to_thread(temporary.unlink, missing_ok=True)
            return DownloadedFile(file.name, size, sha256.hexdigest(), url)
    raise DownloadError(f"Too many redirects for {file.name}.")


async def download(
    client: httpx.AsyncClient,
    update: Update,
    destination: Path,
    *,
    concurrency: int = 3,
) -> DownloadManifest:
    if not 1 <= concurrency <= 16:
        raise DownloadError("Concurrency must be between 1 and 16.")
    if not update.files:
        raise DownloadError("This update has no standalone files.")
    names = [file.name.casefold() for file in update.files]
    if len(set(names)) != len(names) or not all(
        safe_name(file.name) for file in update.files
    ):
        raise DownloadError(
            "The package list contains an unsafe or duplicate filename."
        )
    await asyncio.to_thread(destination.mkdir, parents=True, exist_ok=True)
    limit = asyncio.Semaphore(concurrency)

    async def fetch(file: UpdateFile) -> DownloadedFile:
        async with limit:
            target = destination / file.name
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise DownloadError(f"Output path is not a regular file: {target}")
                sha256, sha1 = await asyncio.to_thread(package_hashes, target)
                if (not file.sha256 and not file.sha1) or not matches_digest(
                    file, sha256, sha1
                ):
                    raise DownloadError(
                        f"Existing file cannot be verified against Microsoft: {target}"
                    )
                return DownloadedFile(
                    file.name, target.stat().st_size, sha256, file.url
                )
            for attempt in range(3):
                try:
                    return await _transfer(client, file, target)
                except httpx.HTTPError as error:
                    retry = isinstance(error, httpx.TransportError) or (
                        isinstance(error, httpx.HTTPStatusError)
                        and error.response.status_code in {429, 500, 502, 503, 504}
                    )
                    if not retry or attempt == 2:
                        raise DownloadError(
                            f"Transfer failed for {file.name}: {error}"
                        ) from error
                    await asyncio.sleep(2**attempt)
            raise AssertionError("retry loop must return or raise")

    manifest_path = destination / "manifest.json"
    lock = destination / ".download-active"
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise DownloadError(
            f"A download is active or was interrupted in {destination}."
        ) from error
    try:
        if manifest_path.exists():
            raise DownloadError(
                f"A completed manifest already exists in {destination}."
            )
        downloaded = await asyncio.gather(
            *(fetch(file) for file in update.files), return_exceptions=True
        )
        files = []
        for result in downloaded:
            if isinstance(result, BaseException):
                raise result
            files.append(result)
        manifest = DownloadManifest(update, tuple(files))
        atomic_json(manifest_path, asdict(manifest))
        return manifest
    finally:
        lock.rmdir()
