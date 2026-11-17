"""Read the running installation's identity before preparing recovery media."""

import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TargetIdentity:
    release: str
    build: int
    revision: int
    architecture: str
    windows: Path
    machine_guid: str
    loader_guid: str


def read_target(*, read_loader: bool = True) -> TargetIdentity:
    if os.name != "nt":
        raise ValueError(
            "Recovery preparation requires a running Windows installation."
        )
    import winreg

    windows = Path(os.environ["SYSTEMROOT"])
    if windows.name.casefold() != "windows" or windows.parent != Path(windows.anchor):
        raise ValueError(
            "Expected a Windows directory at the root of the system volume."
        )
    access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        access=access,
    ) as key:
        release = str(winreg.QueryValueEx(key, "DisplayVersion")[0])
        build = int(winreg.QueryValueEx(key, "CurrentBuildNumber")[0])
        revision = int(winreg.QueryValueEx(key, "UBR")[0])
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", access=access
    ) as key:
        machine_guid = str(UUID(str(winreg.QueryValueEx(key, "MachineGuid")[0])))
    loader_guid = ""
    if read_loader:
        bcd = subprocess.run(
            [windows / "System32" / "bcdedit.exe", "/enum", "{current}", "/v"],
            capture_output=True,
            text=True,
            check=False,
        )
        identifier = re.search(
            r"^identifier\s+\{([a-f0-9-]{36})\}\s*$", bcd.stdout, re.I | re.M
        )
        os_device = re.search(
            r"^osdevice\s+partition=(\w:)\s*$", bcd.stdout, re.I | re.M
        )
        if (
            bcd.returncode != 0
            or identifier is None
            or os_device is None
            or os_device.group(1).casefold() != windows.drive.casefold()
        ):
            raise ValueError(
                "Could not verify the active Windows loader. Run an elevated English "
                "Windows terminal and confirm the current entry boots this partition."
            )
        loader_guid = str(UUID(identifier.group(1)))
    architecture = {"AMD64": "x64", "ARM64": "arm64"}.get(
        platform.machine().upper(), "x86"
    )
    return TargetIdentity(
        release, build, revision, architecture, windows, machine_guid, loader_guid
    )
