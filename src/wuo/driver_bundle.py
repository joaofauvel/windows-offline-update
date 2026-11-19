"""Stage checked Windows Update Agent driver exports for a WinPE job."""

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID

from .downloads import package_hashes, safe_name
from .driver_cache import _verify_inf_signature

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .target import TargetIdentity


@dataclass(frozen=True, slots=True)
class DriverPayload:
    commands: tuple[str, ...]
    paths: dict[str, str]
    size: dict[str, int]


class _FileRecord(TypedDict):
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class _Export:
    update_id: UUID
    revision: int
    payload: Path
    hardware_id: str
    version: str
    files: dict[str, _FileRecord]
    infs: tuple[str, ...]


def _mode(path: Path) -> int:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError(f"Unsafe reparse point in driver export path: {path}")
    return info.st_mode


def _directory_path(path: Path, *, allow_missing: bool = False) -> Path:
    path = Path(os.path.abspath(path))
    for directory in (*reversed(path.parents), path):
        if allow_missing and not directory.exists(follow_symlinks=False):
            break
        if not stat.S_ISDIR(_mode(directory)):
            raise ValueError(f"Expected a directory for driver staging: {directory}")
    return path


def _inventory(payload: Path) -> dict[str, _FileRecord]:
    if not stat.S_ISDIR(_mode(payload)):
        raise ValueError(f"Expected a driver payload directory: {payload}")
    records: dict[str, _FileRecord] = {}
    directories = [payload]
    while directories:
        directory = directories.pop()
        names: set[str] = set()
        for path in sorted(directory.iterdir()):
            folded = path.name.casefold()
            if not safe_name(path.name) or folded in names:
                raise ValueError(
                    f"Unsafe or ambiguous filename in driver payload: {path}"
                )
            names.add(folded)
            mode = _mode(path)
            if stat.S_ISDIR(mode):
                directories.append(path)
            elif stat.S_ISREG(mode):
                digest, _ = package_hashes(path)
                records[path.relative_to(payload).as_posix()] = {
                    "sha256": digest,
                    "size": path.stat().st_size,
                }
            else:
                raise ValueError(f"Expected a regular file in driver payload: {path}")
    return records


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError("Duplicate keys in driver export manifest.")
    return data


def _read_export(source: Path, target: TargetIdentity) -> _Export:
    source = _directory_path(source)
    path = source / "manifest.json"
    if not stat.S_ISREG(_mode(path)):
        raise ValueError(f"Expected a regular driver export manifest: {path}")
    manifest = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
    )
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or manifest.get("source") != "Windows Update Agent"
    ):
        raise ValueError(f"Expected a Windows Update Agent driver export: {source}")
    if (
        type(manifest.get("windows_build")) is not int
        or manifest["windows_build"] != target.build
        or manifest.get("windows_release") != target.release
        or manifest.get("architecture") != target.architecture
    ):
        raise ValueError(
            f"Driver export does not match this Windows build and architecture: {source}"
        )
    update_id = manifest.get("update_id")
    try:
        identifier = UUID(update_id) if isinstance(update_id, str) else None
    except ValueError as error:
        raise ValueError(f"Invalid update UUID in driver export: {source}") from error
    revision = manifest.get("update_revision")
    if identifier is None or identifier.int == 0 or str(identifier) != update_id:
        raise ValueError(f"Invalid update UUID in driver export: {source}")
    if type(revision) is not int or not 1 <= revision <= 2_147_483_647:
        raise ValueError(f"Invalid update revision in driver export: {source}")
    hardware = manifest.get("hardware_id")
    title = manifest.get("title")
    if not isinstance(hardware, str) or not hardware.strip():
        raise ValueError(f"Missing hardware ID in driver export: {source}")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"Missing update title in driver export: {source}")
    version = re.search(r"\b\d+(?:\.\d+){3}\b", title)
    payload = source / "payload"
    inventory = _inventory(payload)
    listed = manifest.get("files")
    if (
        not inventory
        or inventory != listed
        or any(type(record["size"]) is not int for record in listed.values())
    ):
        raise ValueError(
            f"Driver payload inventory differs from the manifest: {source}"
        )
    infs = manifest.get("infs")
    if (
        not isinstance(infs, list)
        or not infs
        or any(
            not isinstance(inf, str)
            or not inf.casefold().endswith(".inf")
            or inf not in inventory
            or inventory[inf]["size"] == 0
            for inf in infs
        )
        or len(set(infs)) != len(infs)
    ):
        raise ValueError(
            f"Driver export must list distinct matching INF files: {source}"
        )
    return _Export(
        identifier,
        revision,
        payload,
        hardware,
        version.group() if version else "",
        inventory,
        tuple(infs),
    )


_VERIFY_WINDOWS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$architecture = @{x86=0; x64=9; arm64=12}[$request.architecture]
$hardware = @(Get-CimInstance Win32_PnPEntity -Filter 'Present=TRUE' | ForEach-Object {
    $_.HardwareID
    $_.CompatibleID
})
foreach ($export in $request.exports) {
    if ($hardware -notcontains $export.hardware_id) {
        throw "The driver hardware ID no longer matches a present device: $($export.hardware_id)"
    }
    foreach ($inf in $export.infs) {
        $match = Get-WindowsDriver -Online -Driver $inf | Where-Object {
            $_.HardwareId -eq $export.hardware_id -and $_.Architecture -eq $architecture -and
            (!$export.version -or [string]$_.Version -eq $export.version)
        } | Select-Object -First 1
        if (!$match) { throw "The INF does not match the offered hardware, architecture, and version: $inf" }
    }
}
"""


def _verify_windows_matches(exports: Sequence[_Export], target: TargetIdentity) -> None:
    powershell = target.windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
    request = {
        "architecture": target.architecture,
        "exports": [
            {
                "hardware_id": export.hardware_id,
                "version": export.version,
                "infs": [str(export.payload / inf) for inf in export.infs],
            }
            for export in exports
        ],
    }
    try:
        check = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", _VERIFY_WINDOWS],
            input=json.dumps(request).encode("ascii"),
            capture_output=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError("Windows driver matching timed out.") from error
    if check.returncode != 0:
        detail = check.stderr.decode("utf-8-sig", errors="replace").strip()
        raise ValueError(detail or "Windows could not validate driver INF matches.")


def stage_driver_exports(
    sources: Sequence[Path], target: TargetIdentity, bundle: Path
) -> DriverPayload:
    if not sources:
        raise ValueError("Select at least one Windows Update Agent driver export.")
    bundle = _directory_path(bundle, allow_missing=True)
    exports = [_read_export(source, target) for source in sources]
    if len({(export.update_id, export.revision) for export in exports}) != len(exports):
        raise ValueError("Duplicate Windows Update Agent driver export.")
    _verify_windows_matches(exports, target)
    for export in exports:
        for inf in export.infs:
            _verify_inf_signature(export.payload / inf, export.payload)

    commands: list[str] = []
    paths: dict[str, str] = {}
    sizes: dict[str, int] = {}
    bundle.mkdir(parents=True, exist_ok=True)
    packages = bundle / "packages"
    with tempfile.TemporaryDirectory(prefix=".driver-payload-", dir=bundle) as scratch:
        staged = Path(scratch) / "packages"
        for index, export in enumerate(exports, start=1):
            directory = f"{index:04d}"
            destination = staged / directory
            for relative, record in export.files.items():
                file = destination / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(export.payload / relative, file)
                bundled = f"packages/{directory}/{relative}"
                paths[bundled] = record["sha256"]
                sizes[bundled] = record["size"]
            if _inventory(destination) != export.files:
                raise ValueError(
                    f"Driver payload files changed during staging: {export.payload}"
                )
            commands.extend(f"packages/{directory}/{inf}" for inf in export.infs)
        if packages.exists(follow_symlinks=False):
            if not stat.S_ISDIR(_mode(packages)) or any(packages.iterdir()):
                raise ValueError(f"Driver package directory must be empty: {packages}")
            packages.rmdir()
        staged.rename(packages)
    return DriverPayload(tuple(commands), paths, sizes)
