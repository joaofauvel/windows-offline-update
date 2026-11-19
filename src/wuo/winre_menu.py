"""Stage and inspect a job for the locally installed WinRE menu."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from importlib.resources import as_file, files
from pathlib import Path
from uuid import UUID

from . import bundle
from .downloads import package_hashes
from .target import TargetIdentity
lazy from . import windows_updates
lazy from .hook_registry import HookRegistry
lazy from .hook_selection import describe_selection, read_selection, resolve_selection
lazy from .update import Offer

MENU_PATH = bundle.JOBS_PATH.parent / "menu"


def _id(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("Invalid WinRE job or installation ID.")
    return value


def _read_id(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except PermissionError:
        raise ValueError(
            "Open an administrator terminal to use the WinRE menu."
        ) from None
    return _id(value.removesuffix("\n"))


def _run(command: list[str | Path]) -> str:
    result = subprocess.run(
        command, capture_output=True, text=True, errors="replace", check=False
    )
    if result.returncode:
        raise ValueError(
            result.stderr.strip()
            or result.stdout.strip()
            or f"{Path(command[0]).name} exited with {result.returncode}."
        )
    return result.stdout


class Menu:
    def __init__(self, target: TargetIdentity) -> None:
        self.target = target
        self.root = target.windows.parent / MENU_PATH
        self.windows_incomplete = False

    @property
    def installed(self) -> bool:
        marker = _read_id(self.root / "installed.id")
        if marker is None:
            return False
        if marker != self.target.machine_guid:
            raise ValueError("The WinRE menu belongs to another Windows installation.")
        return True

    @property
    def profile(self) -> Path | None:
        selected = self.root / "selection.json"
        if selected.is_file():
            record = json.loads(selected.read_text(encoding="ascii"))
            if not isinstance(record, dict) or record.keys() != {"profile"}:
                raise ValueError("Invalid saved hook selection.")
            profile = record["profile"]
            if profile is None:
                return None
            if not isinstance(profile, str):
                raise ValueError("Invalid saved hook profile.")
            version = _id(profile)
            path = self.root / "selections" / version / "profile.toml"
            if path.resolve() != path or not path.is_file():
                raise ValueError("The saved hook profile is missing or unsafe.")
            return path
        path = self.root / "profile.toml"
        return path if path.is_file() else None

    @contextmanager
    def _lock(self) -> Iterator[None]:
        if not self.installed:
            raise ValueError("Run wuo setup first.")
        with (self.root / "update.lock").open("a+b") as lock:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise ValueError(
                        "Another wuo process is changing the pending job."
                    ) from None
            yield

    def job_state(self, job_id: str) -> Path:
        return self.target.windows.parent / bundle.JOBS_PATH / job_id

    def _pending_id(self) -> str | None:
        return _read_id(self.root / "pending.id")

    def _windows_complete_locked(self) -> bool:
        result = windows_updates.verify(self.target, self.root)
        self.windows_incomplete = result is not None and not result.complete
        return not self.windows_incomplete

    def _require_windows_complete_locked(self) -> None:
        if not self._windows_complete_locked():
            raise ValueError(
                "The Windows phase is incomplete. Run wuo status and resolve it before publishing or restarting recovery."
            )

    def check_windows(self) -> bool:
        with self._lock():
            return self._windows_complete_locked()

    @contextmanager
    def feature_preview(self) -> Iterator[None]:
        if not self.installed:
            yield
            return
        with self._lock():
            if self._pending_id() is not None:
                raise ValueError(
                    "Resolve the pending recovery job with wuo status first."
                )
            windows_updates.require_completed_phase(self.target, self.root)
            yield

    def publish(self, source: Path) -> Path:
        with self._lock():
            self._require_windows_complete_locked()
            if self._pending_id() is not None:
                raise ValueError("An update is already staged. Run wuo status.")
            plan_bytes = (source / "plan.json").read_bytes()
            bundle.verify(source)
            plan = json.loads(plan_bytes)
            job_id = _id(plan["id"])
            if any(
                plan["target"].get(key) != getattr(self.target, key)
                for key in (
                    "machine_guid",
                    "loader_guid",
                    "release",
                    "build",
                    "revision",
                    "architecture",
                )
            ):
                raise ValueError(
                    "The bundle belongs to a different Windows installation or baseline."
                )
            job = self.job_state(job_id)
            if (job / "apply.started").exists() or (job / "state.ready").exists():
                raise ValueError("This servicing job has already started.")
            if _read_id(job / "target.id") != job_id:
                raise ValueError("The bundle has no matching Windows target marker.")

            bundles = self.root / "bundles"
            bundles.mkdir(exist_ok=True)
            destination = bundles / job_id
            with tempfile.TemporaryDirectory(dir=bundles, prefix="stage-") as temporary:
                draft = Path(temporary) / "bundle"
                draft.mkdir()
                (draft / "plan.json").write_bytes(plan_bytes)
                plan_digest = package_hashes(draft / "plan.json")[0]
                if (job / "plan.sha256").read_text(
                    encoding="ascii"
                ).strip() != plan_digest:
                    raise ValueError(
                        "The bundle plan has changed since it was prepared."
                    )
                inventory = plan["paths"]
                if (
                    not {"recovery.cmd"} <= inventory.keys()
                    or {"plan.json", "hashes.txt"} & inventory.keys()
                ):
                    raise ValueError("Invalid recovery bundle inventory.")
                for relative in inventory:
                    copy = draft / relative
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source / relative, copy)
                bundle.verify(draft)
                hashes = {**inventory, "plan.json": plan_digest}
                (draft / "hashes.txt").write_bytes(
                    "".join(
                        f"{digest} {relative}\r\n"
                        for relative, digest in sorted(hashes.items())
                    ).encode("ascii")
                )
                pending = Path(temporary) / "pending.id"
                pending.write_bytes(f"{job_id}\r\n".encode("ascii"))
                anchor = Path(temporary) / "hashes.sha256"
                anchor.write_bytes(
                    f"{package_hashes(draft / 'hashes.txt')[0]}\r\n".encode("ascii")
                )
                draft.rename(destination)
                os.replace(anchor, job / "hashes.sha256")
                os.replace(pending, self.root / "pending.id")
            return destination

    def _verify_completed(self, job_id: str) -> bool:
        state = self.job_state(job_id)
        marker = _read_id(state / "state.ready")
        if marker is None:
            return False
        if marker != job_id:
            raise ValueError(
                f"The servicing marker does not match. See {state / 'recovery.log'}."
            )
        prepared = self.root / "bundles" / job_id
        bundle.verify(prepared, post_boot=True)
        plan = json.loads((prepared / "plan.json").read_text(encoding="utf-8"))
        kind = "Driver" if plan.get("kind") == "drivers" else "Windows package"
        print(f"{kind} recovery job verified. Log: {state / 'recovery.log'}")
        return True

    def status(self) -> None:
        if not self.installed:
            print("WinRE menu is not installed. Run wuo setup.")
            return
        self.describe_hooks()
        with self._lock():
            windows_updates.verify(self.target, self.root)
            job_id = self._pending_id()
            if job_id is None:
                print("No update staged.")
                return
            if self._verify_completed(job_id):
                (self.root / "pending.id").unlink()
                return
            state = self.job_state(job_id)
            if (state / "apply.started").exists():
                raise ValueError(
                    f"Offline servicing was interrupted. See {state / 'recovery.log'}."
                )
            print("Update staged. Run wuo update to enter recovery.")

    def discard(self) -> None:
        with self._lock():
            (self.root / "pending.id").unlink(missing_ok=True)
        print("Pending job cleared. Its files and logs are retained.")

    def resume(self, *, allow_restart: bool = True) -> bool:
        with self._lock():
            self.describe_pending_hooks()
            windows_result = windows_updates.verify(self.target, self.root)
            self.windows_incomplete = (
                windows_result is not None and not windows_result.complete
            )
            if self.windows_incomplete and not allow_restart:
                raise ValueError(
                    "The unfinished Windows phase has captured hooks. Finish it with a plain wuo update before selecting hooks for a new phase."
                )
            if windows_result is not None and windows_result.reboot_required:
                print("Restart Windows to finish the Windows phase before continuing.")
                return False
            job_id = self._pending_id()
            if job_id is None:
                return True
            if self.windows_incomplete:
                print(
                    "The Windows phase is incomplete. Resolve it before restarting the staged recovery job."
                )
                return False
            self.describe_hooks()
            if self._verify_completed(job_id):
                (self.root / "pending.id").unlink()
                return True
            state = self.job_state(job_id)
            if (state / "apply.started").exists():
                raise ValueError(
                    f"Offline servicing was interrupted. See {state / 'recovery.log'}."
                )
        print("An update is already staged.")
        if not allow_restart:
            raise ValueError(
                "The pending job has captured hooks. Finish it or run wuo status --discard before selecting hooks for a new job."
            )
        self.offer_restart()
        return False

    def install_windows(
        self, offers: list[Offer], profile: Path | None, *, hooks_explicit: bool = False
    ) -> windows_updates.WindowsResult:
        with self._lock():
            if self._pending_id() is not None:
                raise ValueError(
                    "Finish the pending recovery job before installing Windows security updates."
                )
            result = windows_updates.install(
                offers,
                self.target,
                self.root,
                profile=profile,
                hooks_explicit=hooks_explicit,
            )
            self.windows_incomplete = not result.complete
            return result

    def describe_hooks(self, registry: HookRegistry | None = None) -> None:
        if not self.installed:
            print("Saved hooks: none. WinRE menu is not installed.")
            return
        registry = registry or HookRegistry()
        print("Saved hooks for future jobs")
        describe_selection(read_selection(self.profile), registry)
        self.describe_pending_hooks(registry)

    def describe_pending_hooks(self, registry: HookRegistry | None = None) -> None:
        native_journal = self.root / windows_updates.JOURNAL
        if native_journal.is_file():
            native = json.loads(native_journal.read_text(encoding="utf-8-sig"))
            if native.get("phase_complete") is True:
                print("Previous Windows security phase: completed.")
            else:
                captured = native.get("hooks_job_id")
                print("Pending Windows phase uses these captured hooks")
                if captured:
                    job = self.root / "windows-hook-jobs" / _id(captured)
                    captured_profile = job / "hooks.toml"
                    receipt = json.loads(
                        (job / "windows-hooks.json").read_text(encoding="utf-8")
                    )
                    if (
                        receipt.get("profile_sha256") is not None
                        and not captured_profile.is_file()
                    ):
                        raise ValueError(
                            "The captured Windows hook profile is missing."
                        )
                    describe_selection(
                        read_selection(
                            captured_profile if captured_profile.is_file() else None
                        ),
                        registry,
                    )
                else:
                    print(
                        "  Captured hook selection is unavailable. Review the native phase journal."
                    )
        job_id = self._pending_id()
        if job_id is not None:
            pending = self.root / "bundles" / job_id
            if not (pending / "plan.json").is_file():
                print(f"Pending job {job_id}: hook selection unavailable.")
                return
            plan = json.loads((pending / "plan.json").read_text(encoding="utf-8"))
            profile = pending / "hooks.toml" if plan.get("hooks") else None
            print(f"Pending job {job_id} uses these captured hooks")
            describe_selection(read_selection(profile), registry)

    def offer_restart(self) -> None:
        with self._lock():
            self._require_windows_complete_locked()
            pending = self._pending_id()
        print(
            "In recovery, select Troubleshoot > wuo (Windows Offline Update on older installations)."
        )
        if not sys.stdin.isatty():
            print("Run wuo update in an administrator terminal when ready to restart.")
            return
        if input("Restart into Windows Recovery now? [y/N] ").strip().lower() in {
            "y",
            "yes",
        }:
            with self._lock():
                self._require_windows_complete_locked()
                if pending is None or self._pending_id() != pending:
                    raise ValueError("The staged update changed. Run wuo status.")
                from xml.etree import ElementTree

                registration = self.target.windows / "System32/Recovery/ReAgent.xml"
                try:
                    settings = ElementTree.parse(registration)
                except (OSError, ElementTree.ParseError) as error:
                    raise ValueError(
                        "Could not read the Windows recovery registration."
                    ) from error
                if any(
                    settings.find(f"{name}[@state='1']") is None
                    for name in ("InstallState", "OemTool")
                ):
                    raise ValueError(
                        "The WinRE menu is no longer registered. Run wuo status to review the staged job."
                    )
                system32 = self.target.windows / "System32"
                _run([system32 / "reagentc.exe", "/boottore"])
                _run([system32 / "shutdown.exe", "/r", "/t", "0"])

    def remove(self) -> None:
        if not self.installed:
            print("WinRE menu is not installed.")
            return
        with self._lock():
            job_id = self._pending_id()
            if job_id is not None:
                if not self._verify_completed(job_id):
                    raise ValueError(
                        "Finish or clear the staged job before removing the menu."
                    )
                (self.root / "pending.id").unlink()
            self.configure("uninstall")

    def configure(self, action: str, profile: Path | None = None) -> None:
        selected = read_selection(profile)
        if action == "install" and self.installed:
            with self._lock():
                self._save_selection(selected.path)
            print("Saved hooks for future update jobs.")
            return
        resources = files("wuo")
        with ExitStack() as workspace:
            script = workspace.enter_context(
                as_file(resources.joinpath("winre_setup.ps1"))
            )
            launcher = workspace.enter_context(
                as_file(resources.joinpath("winre_launcher.cmd.in"))
            )
            command: list[str | Path] = [
                self.target.windows / "System32/WindowsPowerShell/v1.0/powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script,
                "-Action",
                action,
                "-Root",
                self.root,
                "-Launcher",
                launcher,
                "-MachineGuid",
                self.target.machine_guid,
            ]
            if profile is not None:
                folder = workspace.enter_context(
                    tempfile.TemporaryDirectory(prefix="wuo-profile-")
                )
                snapshot = Path(folder) / "profile.toml"
                metadata = profile.with_name("hooks.json")
                if metadata.is_file():
                    shutil.copyfile(profile, snapshot)
                    shutil.copyfile(metadata, snapshot.with_name("hooks.json"))
                else:
                    resolve_selection((), profile, Path(folder))
                read_selection(snapshot)
                command.extend(("-Profile", snapshot))
                command.extend(("-HookMetadata", snapshot.with_name("hooks.json")))
            output = _run(command)
        if output.strip():
            print(output.strip())

    def _save_selection(self, profile: Path | None) -> None:
        from uuid import uuid4

        from .downloads import atomic_json

        if profile is None:
            atomic_json(self.root / "selection.json", {"profile": None})
            return
        # Keep a complete snapshot behind one atomic pointer so readers see one generation.
        selected = read_selection(profile)
        generations = self.root / "selections"
        generations.mkdir(exist_ok=True)
        version = str(uuid4())
        destination = generations / version
        with tempfile.TemporaryDirectory(dir=generations, prefix="save-") as temporary:
            draft = Path(temporary) / "selection"
            draft.mkdir()
            metadata = profile.with_name("hooks.json")
            if metadata.is_file():
                shutil.copyfile(profile, draft / "profile.toml")
                shutil.copyfile(metadata, draft / "hooks.json")
            else:
                resolve_selection((), profile, draft)
            check = read_selection(draft / "profile.toml")
            if check.hooks != selected.hooks or check.origins != selected.origins:
                raise ValueError("Hook selection changed while saving.")
            draft.rename(destination)
            atomic_json(self.root / "selection.json", {"profile": version})
