"""Build a WinPE ISO for a single prepared recovery job."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from importlib.resources import files
from pathlib import Path
from uuid import UUID

from . import bundle
from .downloads import atomic_json, safe_name
from .target import TargetIdentity, read_target

COMPONENTS = (
    "WinPE-WMI",
    "WinPE-NetFX",
    "WinPE-Scripting",
    "WinPE-PowerShell",
    "WinPE-StorageWMI",
)
STARTNET = r"""@echo off
wpeinit
if errorlevel 1 exit /b 1
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%SystemRoot%\System32\wuo-startup.ps1"
exit /b %errorlevel%
"""


def write_startup(
    job_bundle: Path,
    system32: Path,
    target: TargetIdentity,
    windows_partition: UUID,
    efi_partition: UUID,
    *,
    check_only: bool,
) -> None:
    if windows_partition == efi_partition or any(
        partition.int == 0 for partition in (windows_partition, efi_partition)
    ):
        raise ValueError(
            "Distinct Windows and EFI GPT partition identifiers are required."
        )
    try:
        relative = job_bundle.resolve(strict=True).relative_to(
            target.windows.parent.resolve()
        )
    except ValueError as error:
        raise ValueError(
            "Keep the bundle in a directory on the target Windows volume."
        ) from error
    if not relative.parts or not all(safe_name(part) for part in relative.parts):
        raise ValueError("Choose a bundle path with ordinary Windows folder names.")
    manifest_path = job_bundle / "plan.json"
    snapshot = manifest_path.read_bytes()
    bundle.verify(job_bundle)
    if manifest_path.read_bytes() != snapshot:
        raise ValueError("The bundle plan changed during verification.")
    manifest = json.loads(snapshot)
    if (
        any(
            manifest["target"].get(key) != getattr(target, key)
            for key in (
                "machine_guid",
                "loader_guid",
                "release",
                "build",
                "revision",
                "architecture",
            )
        )
        or "recovery.cmd" not in manifest["paths"]
    ):
        raise ValueError("The recovery bundle does not match the running installation.")
    job_id = str(UUID(manifest["id"]))
    if "plan.json" in manifest["paths"]:
        raise ValueError("Unexpected recovery plan inventory.")
    root = target.windows.parent.resolve()
    job = root / bundle.JOBS_PATH / job_id
    marker = job / "target.id"
    prepared = job / "plan.sha256"
    digest = hashlib.sha256(snapshot).hexdigest()
    if (
        job.resolve() != job
        or any(
            not path.is_file() or path.is_symlink() or path.is_junction()
            for path in (marker, prepared)
        )
        or marker.read_text(encoding="ascii").strip() != job_id
        or prepared.read_text(encoding="ascii").strip() != digest
    ):
        raise ValueError(
            "The bundle does not match a prepared job on the Windows volume. Prepare it again for WinPE."
        )
    atomic_json(
        system32 / "wuo-job.json",
        {
            "job": job_id,
            "partitions": {
                "windows": str(windows_partition),
                "efi": str(efi_partition),
            },
            "bundle": str(relative),
            "mode": "check" if check_only else "apply",
            "files": {
                **manifest["paths"],
                "plan.json": digest,
            },
        },
    )
    script = files("wuo").joinpath("winpe_startup.ps1").read_bytes()
    (system32 / "wuo-startup.ps1").write_bytes(script)
    (system32 / "startnet.cmd").write_bytes(
        STARTNET.replace("\n", "\r\n").encode("ascii")
    )


def _run(
    command: list[str | Path],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        env=env,
        cwd=cwd,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ValueError(
            f"{Path(command[0]).name} returned {result.returncode}. {detail}"
        )
    return result.stdout.strip()


def _partition_guids(target: TargetIdentity) -> tuple[UUID, UUID]:
    drive = target.windows.drive
    if (
        len(drive) != 2
        or drive[1] != ":"
        or not drive[0].isascii()
        or not drive[0].isalpha()
    ):
        raise ValueError("The active Windows drive letter could not be determined.")
    powershell = target.windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = (
        "$ErrorActionPreference='Stop'; "
        f"$os = @(Get-Partition -DriveLetter '{drive[0]}'); "
        "$efi = @(Get-Partition | Where-Object { $_.IsSystem -and "
        "$_.GptType -eq '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' }); "
        "if ($os.Count -ne 1 -or $efi.Count -ne 1 -or !$os[0].Guid -or !$efi[0].Guid) "
        "{ throw 'Could not identify a unique Windows and active GPT EFI partition' }; "
        "@{windows=[string]$os[0].Guid; efi=[string]$efi[0].Guid} | ConvertTo-Json -Compress"
    )
    data = json.loads(
        _run([powershell, "-NoProfile", "-NonInteractive", "-Command", command])
    )
    return UUID(data["windows"]), UUID(data["efi"])


def _cmd_path(path: Path) -> str:
    value = str(path)
    if any(char in value for char in '"%!^\r\n&|<>'):
        raise ValueError("The ADK build path contains characters unsupported by CMD.")
    return value


def build_iso(
    job_bundle: Path, output: Path, *, check_only: bool, bootex: bool
) -> None:
    target = read_target()
    if target.architecture != "x64":
        raise ValueError("The WinPE ISO builder currently supports x64 installations.")
    output = output.resolve()
    if output.exists() or output.suffix.lower() != ".iso":
        raise ValueError("Choose a new .iso output file.")
    kits = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
    adk = kits / "Windows Kits/10/Assessment and Deployment Kit"
    environment = adk / "Deployment Tools/DandISetEnv.bat"
    dism = adk / "Deployment Tools/amd64/DISM/dism.exe"
    winpe_root = adk / "Windows Preinstallation Environment"
    copype = winpe_root / "copype.cmd"
    make_media = winpe_root / "MakeWinPEMedia.cmd"
    winpe = winpe_root / "amd64"
    components = winpe / "WinPE_OCs"
    packages = [
        package
        for name in COMPONENTS
        for package in (
            components / f"{name}.cab",
            components / "en-us" / f"{name}_en-us.cab",
        )
    ]
    if not all(
        path.is_file()
        for path in (
            environment,
            dism,
            copype,
            make_media,
            winpe / "en-us/winpe.wim",
            *packages,
        )
    ):
        raise ValueError(
            "Install the Microsoft ADK Deployment Tools and matching x64 WinPE add-on."
        )
    windows_partition, efi_partition = _partition_guids(target)
    output.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="wuo-winpe-", dir=output.parent)).resolve()
    mount = workspace / "mount"
    work = workspace / "media"
    build = workspace / "adk.cmd"
    runner = target.windows / "System32/cmd.exe"
    try:
        startup = workspace / "startup"
        startup.mkdir()
        write_startup(
            job_bundle,
            startup,
            target,
            windows_partition,
            efi_partition,
            check_only=check_only,
        )
        staged_iso = workspace / "wuo.iso"
        env = {
            **os.environ,
            "PATH": os.pathsep.join(
                str(folder) for folder in (target.windows / "System32", target.windows)
            ),
            "WUO_ADK_ENV": _cmd_path(environment),
            "WUO_ADK_COPYPE": _cmd_path(copype),
            "WUO_ADK_MAKEMEDIA": _cmd_path(make_media),
            "WUO_ADK_WORK": _cmd_path(work),
            "WUO_ADK_ISO": _cmd_path(staged_iso),
        }
        init = '@echo off\nsetlocal DisableDelayedExpansion\ncall "%WUO_ADK_ENV%" || exit /b 1\n'
        build.write_text(
            init
            + 'call "%WUO_ADK_COPYPE%" amd64 "%WUO_ADK_WORK%"\nexit /b %errorlevel%\n',
            encoding="ascii",
        )
        _run([runner, "/d", "/c", build], env=env, cwd=workspace)
        mount.mkdir()
        _run(
            [
                dism,
                "/English",
                "/Mount-Image",
                f"/ImageFile:{work / 'media/sources/boot.wim'}",
                "/Index:1",
                f"/MountDir:{mount}",
            ]
        )
        try:
            for package in packages:
                _run(
                    [
                        dism,
                        "/English",
                        f"/Image:{mount}",
                        "/Add-Package",
                        f"/PackagePath:{package}",
                    ]
                )
            for script in startup.iterdir():
                shutil.copyfile(script, mount / "Windows/System32" / script.name)
            _run([dism, "/English", "/Unmount-Image", f"/MountDir:{mount}", "/Commit"])
        except OSError, ValueError, KeyboardInterrupt:
            try:
                _run(
                    [
                        dism,
                        "/English",
                        "/Unmount-Image",
                        f"/MountDir:{mount}",
                        "/Discard",
                    ]
                )
            except (OSError, ValueError) as error:
                raise ValueError(
                    f"WinPE image unmount failed. Inspect the mount at {mount}."
                ) from error
            raise
        flag = " /bootex" if bootex else ""
        build.write_text(
            init
            + f'call "%WUO_ADK_MAKEMEDIA%" /ISO "%WUO_ADK_WORK%" "%WUO_ADK_ISO%"{flag}\nexit /b %errorlevel%\n',
            encoding="ascii",
        )
        _run([runner, "/d", "/c", build], env=env, cwd=workspace)
        staged_iso.rename(output)
    except (OSError, ValueError, KeyboardInterrupt) as error:
        raise ValueError(
            f"WinPE build stopped. Working files are at {workspace}. {error}"
        ) from error
    if workspace.resolve().parent == output.parent:
        shutil.rmtree(workspace)
