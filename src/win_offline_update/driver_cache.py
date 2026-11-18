"""Download and export an exact applicable driver from Windows Update Agent."""

import asyncio
import json
import shutil
import tempfile
from contextlib import suppress
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from .downloads import atomic_json, package_hashes, safe_name

if TYPE_CHECKING:
    from .target import TargetIdentity
    from .update import Offer


def check_offer(offer: Offer) -> int:
    if (
        offer.category != "drivers"
        or not offer.hardware_id
        or not offer.hardware_verified
    ):
        raise ValueError(
            "A Windows Update Agent driver with matching present hardware is required."
        )
    if offer.revision is None or offer.revision < 1:
        raise ValueError("Windows Update Agent did not supply a driver revision.")
    return offer.revision


def _verify_inf_signature(inf: Path, payload: Path) -> None:
    from ctypes import POINTER, Structure, WinDLL, WinError, byref, c_void_p, sizeof
    from ctypes.wintypes import BOOL, DWORD, LPCWSTR, WCHAR

    class Signer(Structure):
        _fields_ = [
            ("size", DWORD),
            ("catalog", WCHAR * 260),
            ("name", WCHAR * 260),
            ("version", WCHAR * 260),
            ("score", DWORD),
        ]

    setup = WinDLL("setupapi", use_last_error=True)
    verify = setup.SetupVerifyInfFileW
    verify.argtypes = (LPCWSTR, c_void_p, POINTER(Signer))
    verify.restype = BOOL
    signer = Signer()
    signer.size = sizeof(signer)
    if not verify(str(inf), None, byref(signer)):
        raise ValueError(
            f"Windows could not verify the signature of {inf.name}: {WinError()}"
        )
    catalog = Path(signer.catalog).resolve()
    if not catalog.is_file() or not catalog.is_relative_to(payload.resolve()):
        raise ValueError(
            f"The signed catalog for {inf.name} is missing from the export."
        )


async def _copy_from_cache(
    offer: Offer, identity: TargetIdentity, payload: Path
) -> list[str]:
    powershell = identity.windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = (
        files("win_offline_update")
        .joinpath("driver_cache.ps1")
        .read_text(encoding="utf-8")
    )
    request = json.dumps(
        {
            "update_id": offer.id,
            "update_revision": offer.revision,
            "hardware_id": offer.hardware_id,
            "destination": str(payload),
            "architecture": identity.architecture,
        }
    )
    process = await asyncio.create_subprocess_exec(
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(900):
            stdout, stderr = await process.communicate(request.encode("utf-8"))
    except (TimeoutError, asyncio.CancelledError) as error:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        if isinstance(error, TimeoutError):
            raise ValueError(
                "Windows Update Agent driver download timed out."
            ) from error
        raise
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(detail or "Windows Update Agent could not export the driver.")
    data = json.loads(stdout)
    infs = data.get("infs") if isinstance(data, dict) else None
    if (
        not isinstance(infs, list)
        or not infs
        or not all(isinstance(inf, str) for inf in infs)
    ):
        raise ValueError("Windows Update Agent exported no matching driver INF.")
    for relative in infs:
        inf = payload / relative
        if not inf.is_file() or not inf.resolve().is_relative_to(payload):
            raise ValueError("The matched driver INF is missing from the export.")
        await asyncio.to_thread(_verify_inf_signature, inf, payload)
    return infs


def _inventory(payload: Path) -> dict[str, dict[str, str | int]]:
    if not payload.is_dir() or payload.is_symlink() or payload.is_junction():
        raise ValueError("The driver export has a missing or unsafe payload directory.")
    records: dict[str, dict[str, str | int]] = {}
    for path in sorted(payload.rglob("*")):
        if (
            path.is_symlink()
            or path.is_junction()
            or not path.resolve().is_relative_to(payload)
        ):
            raise ValueError(f"Unsafe path in the exported driver: {path}")
        relative = path.relative_to(payload)
        if not all(safe_name(part) for part in relative.parts):
            raise ValueError(f"Unsafe filename in the exported driver: {relative}")
        if path.is_file():
            digest, _ = package_hashes(path)
            records[relative.as_posix()] = {
                "size": path.stat().st_size,
                "sha256": digest,
            }
    if not records:
        raise ValueError("Windows Update Agent exported no driver files.")
    return records


async def export_driver(offer: Offer, identity: TargetIdentity, output: Path) -> Path:
    revision = check_offer(offer)
    update_id = str(UUID(offer.id))
    manifest = {
        "schema_version": 1,
        "source": "Windows Update Agent",
        "update_id": update_id,
        "update_revision": revision,
        "hardware_id": offer.hardware_id,
        "title": offer.title,
        "architecture": identity.architecture,
        "windows_release": identity.release,
        "windows_build": identity.build,
    }
    output = await asyncio.to_thread(output.resolve)
    destination = output / f"{update_id}-r{revision}-wua"
    if destination.exists():
        manifest_path = destination / "manifest.json"
        if (
            destination.is_symlink()
            or destination.is_junction()
            or manifest_path.is_symlink()
        ):
            raise ValueError("Unsafe existing driver export.")
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        actual = await asyncio.to_thread(_inventory, destination / "payload")
        if (
            not isinstance(existing, dict)
            or actual != existing.get("files")
            or any(existing.get(key) != value for key, value in manifest.items())
        ):
            raise ValueError(f"Existing driver export changed: {destination}")
        infs = existing.get("infs")
        if (
            not isinstance(infs, list)
            or not infs
            or not all(
                isinstance(inf, str)
                and inf.casefold().endswith(".inf")
                and inf in actual
                for inf in infs
            )
        ):
            raise ValueError(
                f"Existing driver export has no matching INF record: {destination}"
            )
        return destination

    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wuo-driver-", dir=output) as directory:
        scratch = Path(directory)
        native = scratch / "native"
        native.mkdir()
        infs = await _copy_from_cache(offer, identity, native)
        inventory = await asyncio.to_thread(_inventory, native)
        if not set(infs).issubset(inventory):
            raise ValueError("The matching driver INF disappeared during export.")
        # Windows can retain handles to files inspected by SetupAPI. Publish a separate copy.
        staging = scratch / "driver"
        payload = staging / "payload"
        await asyncio.to_thread(
            shutil.copytree, native, payload, copy_function=shutil.copyfile
        )
        if await asyncio.to_thread(_inventory, payload) != inventory:
            raise ValueError("Driver package files changed during export.")
        atomic_json(
            staging / "manifest.json", {**manifest, "files": inventory, "infs": infs}
        )
        staging.rename(destination)
    return destination
