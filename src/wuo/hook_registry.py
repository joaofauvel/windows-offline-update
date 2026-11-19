"""Discover validated hook data from bundled resources and pinned Git objects."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib import metadata, resources
from pathlib import Path
from typing import Literal, TypedDict, cast
from urllib.parse import urlsplit
from uuid import uuid4

from .hooks import load_profile

OFFICIAL_REPOSITORY = "https://github.com/joaofauvel/wuo.git"
_LEGACY_OFFICIAL_REPOSITORY = "https://github.com/joaofauvel/windows-offline-update.git"
MAX_PROFILE_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_PROFILES = 128
MAX_GIT_BYTES = 128 * 1024 * 1024
GIT_TIMEOUT = 30.0
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])\Z")


@dataclass(frozen=True)
class HookOrigin:
    name: str
    source: str
    revision: str
    location: str
    sha256: str


@dataclass(frozen=True)
class HookProfile:
    origin: HookOrigin
    content: bytes


@dataclass(frozen=True)
class Freshness:
    state: Literal["current", "changed", "removed", "unknown"]
    detail: str


class _Source(TypedDict):
    repo: str
    ref: str
    revision: str
    snapshot: str
    profiles: dict[str, str]


def _name(value: str) -> str:
    if not _NAME.fullmatch(value) or _DEVICE.fullmatch(value):
        raise ValueError(f"Unsafe hook source or profile name: {value!r}")
    return value


def _ref(value: str) -> str:
    # Fetch accepts refspecs as well as refs. Do not allow a user-supplied
    # destination, wildcard, option, revision expression, or negative refspec.
    if (
        not value
        or len(value) > 256
        or value.startswith(("-", "+", ".", "/"))
        or value.endswith((".", "/", ".lock"))
        or any(char.isspace() or ord(char) < 32 for char in value)
        or any(char in value for char in ":*?[^~\\")
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(
            part.startswith(".") or part.endswith(".lock") for part in value.split("/")
        )
    ):
        raise ValueError("A hook source ref must name one branch, tag, or commit.")
    return value


def _repository(value: str) -> str:
    if (
        not value
        or value.startswith("-")
        or any(ord(char) < 32 for char in value)
        or "::" in value
    ):
        raise ValueError("Unsafe hook repository or unsupported transport.")
    if "://" in value:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "ssh"}
            or not parsed.hostname
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Hook repositories require HTTPS, SSH, or a local repository."
            )
        return value
    if not re.match(r"^[A-Za-z]:[\\/]", value) and re.fullmatch(
        r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9.-]*:[^\s]+", value
    ):
        return value
    path = Path(value).expanduser()
    if not path.is_dir():
        raise ValueError("The local hook repository does not exist.")
    return str(path.resolve())


def _validate_content(content: bytes) -> None:
    if not content or len(content) > MAX_PROFILE_BYTES:
        raise ValueError("A hook profile is empty or exceeds the profile size limit.")
    with tempfile.TemporaryDirectory(prefix="wuo-hook-validation-") as directory:
        path = Path(directory) / "profile.toml"
        path.write_bytes(content)
        try:
            load_profile(path)
        except (ValueError, UnicodeError) as error:
            raise ValueError(f"Invalid hook profile: {error}") from error


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_bounded(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError(f"Hook registry file exceeds its size limit: {path.name}")
    return content


def _disk_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            info = item.stat()
        except FileNotFoundError:
            # Git renames temporary pack files while a fetch is running.
            continue
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def _terminate_git(process: subprocess.Popen[bytes]) -> None:
    """Stop only the Git process tree created by this operation."""
    if sys.platform == "win32":
        taskkill = (
            Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/taskkill.exe"
        )
        with suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
    else:
        # _run_git creates this session, so its process group cannot contain
        # an existing user process or another registry operation.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        with suppress(ProcessLookupError):
            process.kill()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired as error:
        raise ValueError("Timed out stopping the hook source Git process.") from error


def _run_git(
    directory: Path,
    arguments: list[str],
    deadline: float,
    maximum: int = MAX_SOURCE_BYTES,
    *,
    fetch: bool = False,
) -> bytes:
    if time.monotonic() >= deadline:
        raise ValueError("Hook source Git operation timed out.")
    environment = os.environ.copy()
    # Retain user authentication and SSH configuration, but isolate Git's
    # repository selection and prohibit arbitrary remote helpers and replaces.
    for key in list(environment):
        if key in {
            "GIT_DIR",
            "GIT_COMMON_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_COUNT",
            "GIT_NAMESPACE",
        } or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            del environment[key]
    environment.update(
        {
            "GIT_ALLOW_PROTOCOL": "https:ssh:file",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "gc.auto=0",
        "-c",
        "maintenance.auto=false",
        "-c",
        "uploadpack.packObjectsHook=",
        "-c",
        "fetch.fsckObjects=true",
        "-c",
        "transfer.fsckObjects=true",
        "-C",
        str(directory),
        *arguments,
    ]
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                start_new_session=os.name != "nt",
            )
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise ValueError("Hook source Git operation timed out.")
                    if (
                        os.fstat(stdout.fileno()).st_size > maximum
                        or os.fstat(stderr.fileno()).st_size > MAX_SOURCE_BYTES
                        or (fetch and _disk_size(directory) > MAX_GIT_BYTES)
                    ):
                        raise ValueError("Hook source exceeds its Git size limit.")
                    with suppress(subprocess.TimeoutExpired):
                        process.wait(
                            timeout=min(0.05, max(0.001, deadline - time.monotonic()))
                        )
            except BaseException:
                _terminate_git(process)
                raise
            if os.fstat(stdout.fileno()).st_size > maximum:
                raise ValueError("Hook source exceeds its Git output size limit.")
            if fetch and _disk_size(directory) > MAX_GIT_BYTES:
                raise ValueError("Hook source exceeds its Git repository size limit.")
            if process.returncode:
                stderr.seek(0)
                detail = stderr.read(2048).decode("utf-8", errors="replace").strip()
                raise ValueError(
                    f"Cannot read hook repository: {detail or 'Git failed.'}"
                )
            stdout.seek(0)
            return stdout.read(maximum + 1)
    except OSError as error:
        raise ValueError(f"Cannot run Git for hook repository: {error}") from error


def _fetch(repo: str, ref: str) -> tuple[str, dict[str, bytes]]:
    # Nothing from the source is checked out. Read only regular root hook blobs
    # after resolving FETCH_HEAD to an immutable commit.
    deadline = time.monotonic() + GIT_TIMEOUT
    with tempfile.TemporaryDirectory(prefix="wuo-hook-git-") as directory:
        bare = Path(directory)
        template = bare / "empty-template"
        template.mkdir()
        _run_git(bare, ["init", "--bare", f"--template={template}"], deadline)
        _run_git(
            bare,
            [
                "fetch",
                "--no-tags",
                "--depth=1",
                "--no-recurse-submodules",
                "--",
                repo,
                ref,
            ],
            deadline,
            fetch=True,
        )
        revision = (
            _run_git(bare, ["rev-parse", "--verify", "FETCH_HEAD^{commit}"], deadline)
            .decode()
            .strip()
        )
        if not _COMMIT.fullmatch(revision):
            raise ValueError("Git did not resolve the hook source to a commit.")
        tree = _run_git(bare, ["ls-tree", "-z", revision, "--", "hooks"], deadline)
        if not tree:
            return revision, {}
        entry, path = tree.rstrip(b"\0").split(b"\t", 1)
        if path != b"hooks" or not entry.startswith(b"040000 tree "):
            raise ValueError("The hooks path must be a regular Git directory.")
        tree = _run_git(bare, ["ls-tree", "-z", f"{revision}:hooks"], deadline)
        profiles: dict[str, bytes] = {}
        total = 0
        for record in tree.split(b"\0"):
            if not record:
                continue
            fields, raw_name = record.split(b"\t", 1)
            if not raw_name.lower().endswith(b".toml"):
                continue
            mode, kind, oid = fields.split(b" ")
            if mode not in {b"100644", b"100755"} or kind != b"blob":
                raise ValueError(
                    "Hook profiles must be regular Git blobs, not links or submodules."
                )
            filename = raw_name.decode("utf-8", errors="strict")
            if not filename.endswith(".toml"):
                raise ValueError("Hook profile filenames must end in .toml.")
            name = _name(filename[:-5])
            if len(profiles) >= MAX_PROFILES:
                raise ValueError("Hook source exceeds its profile count limit.")
            size = int(_run_git(bare, ["cat-file", "-s", oid.decode()], deadline))
            if size > MAX_PROFILE_BYTES or total + size > MAX_SOURCE_BYTES:
                raise ValueError("Hook source exceeds its profile content size limit.")
            content = _run_git(
                bare, ["cat-file", "blob", oid.decode()], deadline, MAX_PROFILE_BYTES
            )
            _validate_content(content)
            total += len(content)
            profiles[name] = content
        return revision, profiles


class HookRegistry:
    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            local = os.environ.get("LOCALAPPDATA")
            root = Path(local) if local else Path.home() / "AppData" / "Local"
            root = root / "wuo" / "hooks"
        self.root = root.absolute()
        self._checks: dict[tuple[str, str], tuple[str, dict[str, bytes]] | str] = {}

    def _safe(self, path: Path) -> Path:
        if not path.is_relative_to(self.root):
            raise ValueError("Unsafe hook registry path.")
        for ancestor in reversed(self.root.parents):
            self._not_link(ancestor)
        candidate = self.root
        self._not_link(candidate)
        if candidate.exists() and not candidate.is_dir():
            raise ValueError("The hook registry root must be a directory.")
        for part in path.relative_to(self.root).parts:
            candidate /= part
            self._not_link(candidate)
        return path

    @staticmethod
    def _not_link(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError(f"Unsafe link or reparse path in hook registry: {path}")

    def _sources(self) -> dict[str, _Source]:
        manifest = self._safe(self.root / "registry.json")
        if not manifest.exists():
            return {}
        try:
            data = json.loads(_read_bounded(manifest, MAX_SOURCE_BYTES))
            if (
                not isinstance(data, dict)
                or data.get("schema_version") != 1
                or not isinstance(data.get("sources"), dict)
            ):
                raise ValueError("Invalid hook registry manifest.")
            sources: dict[str, _Source] = {}
            for name, source in data["sources"].items():
                _name(name)
                if not isinstance(source, dict) or set(source) != {
                    "repo",
                    "ref",
                    "revision",
                    "snapshot",
                    "profiles",
                }:
                    raise ValueError("Invalid hook source metadata.")
                if not all(
                    isinstance(source[key], str)
                    for key in ("repo", "ref", "revision", "snapshot")
                ):
                    raise ValueError("Invalid hook source metadata fields.")
                _ref(source["ref"])
                if name == "official" and (
                    source["repo"]
                    not in (OFFICIAL_REPOSITORY, _LEGACY_OFFICIAL_REPOSITORY)
                    or source["ref"] != "HEAD"
                ):
                    raise ValueError(
                        "The reserved official hook source has invalid upstream metadata."
                    )
                if not _COMMIT.fullmatch(source["revision"]) or not re.fullmatch(
                    r"[0-9a-f]{32}", source["snapshot"]
                ):
                    raise ValueError("Invalid hook source revision or snapshot path.")
                profiles = source["profiles"]
                if not isinstance(profiles, dict) or len(profiles) > MAX_PROFILES:
                    raise ValueError("Invalid hook source profile inventory.")
                for profile, digest in profiles.items():
                    _name(profile)
                    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
                        raise ValueError("Invalid hook profile content digest.")
                sources[name] = cast(_Source, source)
            return sources
        except (OSError, UnicodeError, TypeError) as error:
            raise ValueError(f"Cannot read hook registry: {error}") from error

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        self._safe(self.root).mkdir(parents=True, exist_ok=True)
        path = self._safe(self.root / ".registry.lock")
        # An OS-owned lock is automatically released when a process exits.
        # Keep the file itself: removing it could allow two separate inodes
        # to be locked by concurrent callers.
        with path.open("a+b") as lock:
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise ValueError(
                    "The hook registry is busy; retry after the other hook command finishes."
                ) from error
            try:
                yield
            finally:
                lock.seek(0)
                if sys.platform == "win32":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write_sources(self, sources: dict[str, _Source]) -> None:
        self._safe(self.root).mkdir(parents=True, exist_ok=True)
        manifest = self._safe(self.root / "registry.json")
        descriptor, temporary = tempfile.mkstemp(
            prefix=".registry-", suffix=".json", dir=self.root
        )
        path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    {"schema_version": 1, "sources": sources}, stream, sort_keys=True
                )
                stream.flush()
                os.fsync(stream.fileno())
            self._safe(manifest)
            path.replace(manifest)
        finally:
            path.unlink(missing_ok=True)

    def _publish(
        self, repo: str, ref: str, revision: str, profiles: dict[str, bytes]
    ) -> _Source:
        snapshot = uuid4().hex
        destination = self._safe(self.root / "snapshots" / snapshot)
        destination.mkdir(parents=True)
        for name, content in profiles.items():
            path = self._safe(destination / f"{name}.toml")
            with path.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        return {
            "repo": repo,
            "ref": ref,
            "revision": revision,
            "snapshot": snapshot,
            "profiles": {name: _digest(content) for name, content in profiles.items()},
        }

    @staticmethod
    def _official() -> list[HookProfile]:
        bundled = resources.files("wuo").joinpath("bundled_hooks")
        if not bundled.is_dir():
            # Source checkouts keep the canonical collection at repository root.
            editable = Path(__file__).resolve().parents[2] / "hooks"
            if not editable.is_dir():
                raise ValueError("The installed official hook collection is missing.")
            bundled = editable
        try:
            revision = metadata.version("wuo")
        except metadata.PackageNotFoundError:
            revision = "uninstalled"
        profiles = []
        for path in sorted(bundled.iterdir(), key=lambda item: item.name):
            if not path.name.endswith(".toml"):
                continue
            name = _name(path.name[:-5])
            content = path.read_bytes()
            _validate_content(content)
            profiles.append(
                HookProfile(
                    HookOrigin(
                        f"official/{name}",
                        "official",
                        revision,
                        OFFICIAL_REPOSITORY,
                        _digest(content),
                    ),
                    content,
                )
            )
        return profiles

    def _profile(self, source_name: str, name: str, source: _Source) -> HookProfile:
        digest = source["profiles"].get(name)
        if digest is None:
            raise ValueError(f"Unknown hook profile: {source_name}/{name}")
        path = self._safe(self.root / "snapshots" / source["snapshot"] / f"{name}.toml")
        try:
            content = _read_bounded(path, MAX_PROFILE_BYTES)
        except OSError as error:
            raise ValueError(f"Cannot read cached hook profile: {error}") from error
        if _digest(content) != digest:
            raise ValueError(f"Cached hook profile hash changed: {source_name}/{name}")
        _validate_content(content)
        return HookProfile(
            HookOrigin(
                f"{source_name}/{name}",
                source_name,
                source["revision"],
                source["repo"],
                digest,
            ),
            content,
        )

    def list_profiles(self) -> list[HookProfile]:
        sources = self._sources()
        profiles = [] if "official" in sources else self._official()
        for source_name, source in sorted(sources.items()):
            profiles.extend(
                self._profile(source_name, name, source)
                for name in sorted(source["profiles"])
            )
        return profiles

    def get(self, name: str) -> HookProfile:
        parts = name.split("/")
        if len(parts) != 2:
            raise ValueError("A named hook profile must use source/profile.")
        source_name, profile_name = map(_name, parts)
        source = self._sources().get(source_name)
        if source is not None:
            return self._profile(source_name, profile_name, source)
        if source_name == "official":
            for profile in self._official():
                if profile.origin.name == name:
                    return profile
        raise ValueError(f"Unknown hook profile: {name}")

    def add(self, repo: str, name: str, ref: str = "HEAD") -> list[HookProfile]:
        _name(name)
        if name == "official":
            raise ValueError("The official hook source name is reserved.")
        with self._mutation():
            sources = self._sources()
            if name in sources:
                raise ValueError(
                    f"Hook source already exists: {name}; use hooks refresh."
                )
            repo, ref = _repository(repo), _ref(ref)
            revision, profiles = _fetch(repo, ref)
            if not profiles:
                raise ValueError(
                    "The repository contains no regular hooks/*.toml profiles."
                )
            source = self._publish(repo, ref, revision, profiles)
            sources[name] = source
            self._write_sources(sources)
            self._checks.pop((repo, ref), None)
            return [self._profile(name, key, source) for key in sorted(profiles)]

    def refresh(self, name: str | None = None) -> list[HookProfile]:
        with self._mutation():
            sources = self._sources()
            if name is not None and name != "official" and name not in sources:
                raise ValueError(f"Unknown hook source: {name}")
            names = (
                [name]
                if name is not None
                else ["official", *sorted(key for key in sources if key != "official")]
            )
            refreshed: list[HookProfile] = []
            fetched: dict[tuple[str, str], tuple[str, dict[str, bytes]]] = {}
            for source_name in names:
                if source_name == "official":
                    repo, ref = _repository(OFFICIAL_REPOSITORY), "HEAD"
                else:
                    source = sources[source_name]
                    repo, ref = _repository(source["repo"]), _ref(source["ref"])
                self._checks.pop((repo, ref), None)
                if (repo, ref) not in fetched:
                    fetched[(repo, ref)] = _fetch(repo, ref)
                revision, profiles = fetched[(repo, ref)]
                replacement = self._publish(repo, ref, revision, profiles)
                sources[source_name] = replacement
                refreshed.extend(
                    self._profile(source_name, key, replacement)
                    for key in sorted(profiles)
                )
            self._write_sources(sources)
            return refreshed

    def remove(self, name: str) -> None:
        _name(name)
        if name == "official":
            raise ValueError(
                "The official hook source is reserved and cannot be removed."
            )
        with self._mutation():
            sources = self._sources()
            if name not in sources:
                raise ValueError(f"Unknown hook source: {name}")
            del sources[name]
            self._write_sources(sources)
            self._checks.clear()

    def check(self, origin: HookOrigin) -> Freshness:
        try:
            parts = origin.name.split("/")
            if (
                len(parts) != 2
                or parts[0] != origin.source
                or not _HASH.fullmatch(origin.sha256)
            ):
                return Freshness("unknown", "Invalid saved hook provenance.")
            source_name, profile_name = map(_name, parts)
            if source_name == "official":
                repo, ref = OFFICIAL_REPOSITORY, "HEAD"
            else:
                source = self._sources().get(source_name)
                if source is None:
                    return Freshness(
                        "removed", f"Hook source {source_name} is no longer registered."
                    )
                if source["repo"] != origin.location:
                    return Freshness(
                        "removed",
                        f"Hook source {source_name} now identifies a different repository.",
                    )
                repo, ref = _repository(source["repo"]), _ref(source["ref"])
            key = (repo, ref)
            if key not in self._checks:
                try:
                    self._checks[key] = _fetch(repo, ref)
                except (ValueError, OSError, UnicodeError) as error:
                    self._checks[key] = str(error)
            result = self._checks[key]
            if isinstance(result, str):
                return Freshness("unknown", result)
            revision, profiles = result
            content = profiles.get(profile_name)
            if content is None:
                return Freshness(
                    "removed", f"{origin.name} is absent at upstream {revision}."
                )
            if _digest(content) == origin.sha256:
                return Freshness(
                    "current", f"{origin.name} matches upstream {revision}."
                )
            return Freshness(
                "changed",
                f"{origin.name} has different content at upstream {revision}.",
            )
        except (ValueError, OSError, UnicodeError) as error:
            return Freshness("unknown", str(error))
