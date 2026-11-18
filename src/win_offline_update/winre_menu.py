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
from .hooks import load_profile
from .target import TargetIdentity

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

    def publish(self, source: Path) -> Path:
        with self._lock():
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
        bundle.verify(self.root / "bundles" / job_id, post_boot=True)
        print(f"Update verified. Log: {state / 'recovery.log'}")
        return True

    def status(self) -> None:
        if not self.installed:
            print("WinRE menu is not installed. Run wuo setup.")
            return
        with self._lock():
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

    def resume(self) -> bool:
        with self._lock():
            job_id = self._pending_id()
            if job_id is None:
                return True
            if self._verify_completed(job_id):
                (self.root / "pending.id").unlink()
                return True
            state = self.job_state(job_id)
            if (state / "apply.started").exists():
                raise ValueError(
                    f"Offline servicing was interrupted. See {state / 'recovery.log'}."
                )
        print("An update is already staged.")
        self.offer_restart()
        return False

    def offer_restart(self) -> None:
        pending = self._pending_id()
        print("In recovery, select Troubleshoot > Windows Offline Update.")
        if not sys.stdin.isatty():
            print("Run wuo update in an administrator terminal when ready to restart.")
            return
        if input("Restart into Windows Recovery now? [y/N] ").strip().lower() in {
            "y",
            "yes",
        }:
            with self._lock():
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
        resources = files("win_offline_update")
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
                shutil.copyfile(profile, snapshot)
                load_profile(snapshot)
                command.extend(("-Profile", snapshot))
            output = _run(command)
        if output.strip():
            print(output.strip())
