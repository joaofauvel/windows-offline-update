"""Compose registry hooks and retain verifiable, immutable selection provenance."""

import base64
import binascii
import hashlib
import json
import re
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from .hook_registry import Freshness, HookOrigin, HookProfile, HookRegistry
from .hooks import Hook, load_profile

_MAX_PROFILE_BYTES = 1024 * 1024
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_PROFILES = 64
_ORIGIN_FIELDS = {"name", "source", "revision", "location", "sha256"}


@dataclass(frozen=True, slots=True)
class Selection:
    path: Path | None
    origins: tuple[HookOrigin, ...]
    hooks: tuple[Hook, ...]


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_limited(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"Hook file exceeds the {limit}-byte size limit: {path}")
    return content


def _parse_content(content: bytes) -> tuple[Hook, ...]:
    if len(content) > _MAX_PROFILE_BYTES:
        raise ValueError("Hook profile exceeds the size limit.")
    with tempfile.TemporaryDirectory(prefix="wuo-hook-validation-") as temporary:
        path = Path(temporary) / "profile.toml"
        path.write_bytes(content)
        return load_profile(path)


def _combine(profiles: Sequence[HookProfile]) -> tuple[Hook, ...]:
    hooks: list[Hook] = []
    targets: dict[tuple[str, str, str, str], str] = {}
    for profile in profiles:
        if _digest(profile.content) != profile.origin.sha256:
            raise ValueError(
                f"Hook profile SHA-256 does not match {profile.origin.name}."
            )
        for hook in _parse_content(profile.content):
            identity = (
                hook.phase,
                hook.scope,
                hook.key.casefold(),
                hook.name.casefold(),
            )
            if identity in targets:
                raise ValueError(
                    f"Conflicting registry hooks in {targets[identity]} and "
                    f"{profile.origin.name}: {hook.phase}/{hook.scope}/"
                    f"{hook.key}/{hook.name}."
                )
            targets[identity] = profile.origin.name
            hooks.append(hook)
    return tuple(hooks)


def _serialize_hooks(hooks: tuple[Hook, ...]) -> bytes:
    if not hooks:
        return b"hooks = []\n"
    entries: list[str] = []
    for hook in hooks:
        fields = [
            "[[hooks]]",
            f"phase = {json.dumps(hook.phase)}",
            f"scope = {json.dumps(hook.scope)}",
            f"key = {json.dumps(hook.key)}",
            f"name = {json.dumps(hook.name)}",
            f"value = {hook.value}",
        ]
        if hook.expected is not None:
            fields.append(f"expected = {hook.expected}")
        entries.append("\n".join(fields))
    return ("\n\n".join(entries) + "\n").encode("utf-8")


def resolve_selection(
    names: Sequence[str],
    profile: Path | None,
    directory: Path,
    registry: HookRegistry | None = None,
) -> Selection:
    """Capture selected bytes and provenance in a caller-owned directory."""
    if names and profile is not None:
        raise ValueError("--hook and --profile are mutually exclusive.")
    if not names and profile is None:
        return Selection(None, (), ())
    if len(names) > _MAX_PROFILES:
        raise ValueError(f"Select at most {_MAX_PROFILES} hook profiles.")

    if profile is not None:
        source = profile.resolve()
        content = _read_limited(source, _MAX_PROFILE_BYTES)
        profiles = [
            HookProfile(
                HookOrigin(
                    source.name, "local", "snapshot", str(source), _digest(content)
                ),
                content,
            )
        ]
    else:
        available = registry if registry is not None else HookRegistry()
        profiles = [available.get(name) for name in names]

    hooks = _combine(profiles)
    content = profiles[0].content if len(profiles) == 1 else _serialize_hooks(hooks)
    if len(content) > _MAX_PROFILE_BYTES:
        raise ValueError("Combined hook profile exceeds the size limit.")
    metadata = {
        "schema": 1,
        "profile_sha256": _digest(content),
        "origins": [
            {
                **asdict(item.origin),
                "content_base64": base64.b64encode(item.content).decode("ascii"),
            }
            for item in profiles
        ],
    }
    metadata_content = (
        json.dumps(metadata, indent=2, ensure_ascii=True) + "\n"
    ).encode("utf-8")
    if len(metadata_content) > _MAX_METADATA_BYTES:
        raise ValueError("Hook selection metadata exceeds the size limit.")
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = directory / "profile.toml"
    snapshot.write_bytes(content)
    (directory / "hooks.json").write_bytes(metadata_content)
    return Selection(snapshot, tuple(item.origin for item in profiles), hooks)


def _read_origin(record: object) -> HookProfile:
    if not isinstance(record, dict) or set(record) != _ORIGIN_FIELDS | {
        "content_base64"
    }:
        raise ValueError("Invalid hook selection origin metadata schema.")
    if any(
        not isinstance(record[name], str) or not record[name] for name in _ORIGIN_FIELDS
    ):
        raise ValueError(
            "Hook selection origin metadata fields must be nonempty strings."
        )
    if re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None:
        raise ValueError("Invalid SHA-256 in hook origin metadata.")
    encoded = record["content_base64"]
    if not isinstance(encoded, str):
        raise ValueError("Invalid captured profile content in hook metadata.")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError(
            "Invalid captured profile content in hook metadata."
        ) from error
    origin = HookOrigin(**{name: record[name] for name in _ORIGIN_FIELDS})
    return HookProfile(origin, content)


def read_selection(profile: Path | None) -> Selection:
    """Read a snapshot, validating any provenance against its captured operations."""
    if profile is None:
        return Selection(None, (), ())
    content = _read_limited(profile, _MAX_PROFILE_BYTES)
    hooks = _parse_content(content)
    metadata_path = profile.with_name("hooks.json")
    if not metadata_path.exists():
        origin = HookOrigin(
            profile.name,
            "local",
            "snapshot",
            str(profile.resolve()),
            _digest(content),
        )
        return Selection(profile, (origin,), hooks)
    try:
        metadata = json.loads(_read_limited(metadata_path, _MAX_METADATA_BYTES))
    except (ValueError, UnicodeError) as error:
        raise ValueError("Invalid hook selection metadata.") from error
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"schema", "profile_sha256", "origins"}
        or type(metadata.get("schema")) is not int
        or metadata["schema"] != 1
        or not isinstance(metadata.get("origins"), list)
        or not 1 <= len(metadata["origins"]) <= _MAX_PROFILES
    ):
        raise ValueError("Invalid hook selection metadata schema.")
    if metadata["profile_sha256"] != _digest(content):
        raise ValueError("Hook snapshot SHA-256 does not match its metadata.")
    profiles = [_read_origin(record) for record in metadata["origins"]]
    if _combine(profiles) != hooks:
        raise ValueError(
            "Captured origin operations do not match the hook profile snapshot."
        )
    if len(profiles) == 1 and profiles[0].content != content:
        raise ValueError(
            "Captured origin bytes do not match the hook profile snapshot."
        )
    return Selection(profile, tuple(item.origin for item in profiles), hooks)


def _local_freshness(origin: HookOrigin) -> Freshness:
    try:
        current = _read_limited(Path(origin.location), _MAX_PROFILE_BYTES)
    except FileNotFoundError:
        return Freshness(
            "removed", "The original local profile is no longer available."
        )
    except (OSError, ValueError) as error:
        return Freshness("unknown", f"Cannot check the original local profile: {error}")
    if _digest(current) != origin.sha256:
        return Freshness(
            "changed", "The original local profile differs from this snapshot."
        )
    return Freshness("current", "The original local profile matches this snapshot.")


def describe_selection(
    selection: Selection,
    registry: HookRegistry | None = None,
    check_freshness: bool = True,
) -> None:
    """Print the pinned selection, effective operations, and content freshness."""
    if selection.path is None:
        print("Hooks: none.")
        return
    reselection = ["wuo", "setup"]
    for origin in selection.origins:
        reselection.extend(["--hook", origin.name])
    print("Hooks:")
    for origin in selection.origins:
        print(f"  {origin.name} (source: {origin.source}; revision: {origin.revision})")
        print(f"    SHA-256: {origin.sha256}")
        print(f"    Location: {origin.location}")
        if not check_freshness:
            continue
        is_local_snapshot = origin.source == "local" and origin.revision == "snapshot"
        if is_local_snapshot:
            freshness = _local_freshness(origin)
        else:
            if registry is None:
                registry = HookRegistry()
            try:
                freshness = registry.check(origin)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                freshness = Freshness("unknown", str(error))
        if freshness.state == "current":
            print(f"    Content: current. {freshness.detail}")
            continue
        print(f"    Warning: content {freshness.state}. {freshness.detail}")
        if is_local_snapshot:
            command = subprocess.list2cmdline(
                ["wuo", "setup", "--profile", origin.location]
            )
            print(f"    To save the local profile again: {command}")
        else:
            print(f"    Refresh discovery: wuo hooks refresh {origin.source}")
            print(
                "    Save a refreshed selection: "
                f"{subprocess.list2cmdline(reselection)}"
            )
    if not selection.hooks:
        print("  Registry operations: none.")
    for hook in selection.hooks:
        expected = "" if hook.expected is None else f" (expected {hook.expected})"
        print(
            f"  {hook.phase} [{hook.scope}] {hook.key} / {hook.name} = "
            f"{hook.value} DWORD{expected}"
        )
