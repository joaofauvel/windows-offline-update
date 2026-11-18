"""Captured SYSTEM-registry hooks around a Windows security installation phase."""

import json
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from .downloads import atomic_json, package_hashes
from .hook_selection import read_selection
from .hooks import Hook, load_profile

RECEIPT = "windows-hooks.json"


def _read(key: str, name: str) -> int:
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\" + key) as registry:
        value, kind = winreg.QueryValueEx(registry, name)
    if kind != winreg.REG_DWORD or type(value) is not int:
        raise ValueError(f"The hook target is not an existing DWORD: {key} / {name}")
    return value


def _write(key: str, name: str, value: int) -> None:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\" + key, 0, winreg.KEY_SET_VALUE
    ) as registry:
        winreg.SetValueEx(registry, name, 0, winreg.REG_DWORD, value)


def _path(hook: Hook) -> str:
    if hook.scope == "system":
        return hook.key
    selected = _read("Select", "Default")
    if not 1 <= selected <= 999:
        raise ValueError("The default SYSTEM control set is invalid.")
    return f"ControlSet{selected:03d}\\{hook.key}"


def apply(hooks: tuple[Hook, ...], phase: str) -> None:
    selected = [(hook, _path(hook)) for hook in hooks if hook.phase == phase]
    for hook, key in selected:
        current = _read(key, hook.name)
        if hook.expected is not None and current not in (hook.expected, hook.value):
            raise ValueError(
                f"The hook expected {hook.expected} at {key} / {hook.name}, found {current}."
            )
    for hook, key in selected:
        _write(key, hook.name, hook.value)
        if _read(key, hook.name) != hook.value:
            raise ValueError(f"Registry hook readback failed: {key} / {hook.name}")
        print(f"  {phase} hook applied: SYSTEM\\{key} / {hook.name} = {hook.value}")


def _backup(windows: Path, path: Path) -> None:
    process = subprocess.run(
        [windows / "System32/reg.exe", "save", r"HKLM\SYSTEM", path],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
        check=False,
    )
    if process.returncode or not path.is_file():
        raise ValueError(
            f"Could not back up SYSTEM before applying hooks: {process.stderr.strip() or process.stdout.strip()}"
        )


@contextmanager
def capture(
    profile: Path | None,
    root: Path,
    machine_guid: str,
    windows: Path,
    *,
    job_id: str | None = None,
) -> Iterator[None]:
    hooks = load_profile(profile) if profile else ()
    job_id = str(UUID(job_id)) if job_id else str(uuid4())
    job = root / "windows-hook-jobs" / job_id
    job.mkdir(parents=True)
    record: dict[str, str | None] = {
        "job_id": job_id,
        "machine_guid": machine_guid,
        "state": "prepared",
        "profile_sha256": None,
        "metadata_sha256": None,
    }
    if profile is not None:
        snapshot = job / "hooks.toml"
        shutil.copyfile(profile, snapshot)
        metadata = profile.with_name("hooks.json")
        if metadata.is_file():
            shutil.copyfile(metadata, snapshot.with_name("hooks.json"))
            record["metadata_sha256"] = package_hashes(
                snapshot.with_name("hooks.json")
            )[0]
        read_selection(snapshot)
        # Load the captured bytes, so later edits to the source cannot change this phase.
        hooks = load_profile(snapshot)
        record["profile_sha256"] = package_hashes(snapshot)[0]
    receipt = job / RECEIPT
    pointer = root / RECEIPT
    atomic_json(receipt, record)
    if hooks:
        _backup(windows, job / "system-before-pre.hiv")
    try:
        atomic_json(pointer, record)
        apply(hooks, "pre")
        record["state"] = "installing"
        atomic_json(receipt, record)
        atomic_json(pointer, record)
        yield
    finally:
        backup_error: OSError | ValueError | subprocess.SubprocessError | None = None
        if hooks:
            try:
                _backup(windows, job / "system-before-post.hiv")
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                backup_error = error
        apply(hooks, "post")
        record["state"] = (
            "post-applied" if backup_error is None else "post-applied-backup-failed"
        )
        atomic_json(receipt, record)
        atomic_json(pointer, record)
        if backup_error is not None:
            raise ValueError(
                f"Post hooks were applied, but their phase backup failed. The initial SYSTEM backup is at {job}. {backup_error}"
            ) from backup_error


def verify(
    root: Path,
    machine_guid: str,
    *,
    job_id: str | None = None,
    check_values: bool = True,
) -> None:
    receipt = (
        root / RECEIPT
        if job_id is None
        else root / "windows-hook-jobs" / str(UUID(job_id)) / RECEIPT
    )
    if not receipt.is_file():
        if job_id is not None:
            raise ValueError("The captured Windows hook receipt is missing.")
        return
    try:
        record = json.loads(receipt.read_text(encoding="utf-8"))
        captured_id = str(UUID(record["job_id"]))
        if job_id is not None and captured_id != job_id:
            raise ValueError(
                "The captured hooks do not match the Windows update phase."
            )
        if record["machine_guid"].casefold() != machine_guid.casefold():
            raise ValueError(
                "The native hook receipt belongs to another Windows installation."
            )
        if record["state"] != "post-applied":
            raise ValueError(
                f"The captured Windows registry hooks did not finish. Review {receipt} and its SYSTEM backups before restarting."
            )
        if not check_values:
            return
        digest = record["profile_sha256"]
        if digest is None:
            return
        snapshot = root / "windows-hook-jobs" / captured_id / "hooks.toml"
        if package_hashes(snapshot)[0] != digest:
            raise ValueError("The captured Windows hook profile has changed.")
        metadata_digest = record.get("metadata_sha256")
        if (
            metadata_digest is not None
            and package_hashes(snapshot.with_name("hooks.json"))[0] != metadata_digest
        ):
            raise ValueError("The captured Windows hook provenance has changed.")
        read_selection(snapshot)
        for hook in load_profile(snapshot):
            if hook.phase == "post" and _read(_path(hook), hook.name) != hook.value:
                raise ValueError(
                    f"A captured post hook no longer matches: {hook.key} / {hook.name}"
                )
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"Invalid Windows hook receipt: {receipt}") from error


def captured_profile(root: Path, machine_guid: str, job_id: str) -> Path | None:
    """Resolve the validated immutable selection owned by an unfinished phase."""
    verify(root, machine_guid, job_id=job_id)
    job = root / "windows-hook-jobs" / str(UUID(job_id))
    record = json.loads((job / RECEIPT).read_text(encoding="utf-8"))
    return job / "hooks.toml" if record["profile_sha256"] is not None else None
