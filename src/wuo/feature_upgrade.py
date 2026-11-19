"""Preflight a Windows Setup feature offer without staging an installation."""

import asyncio
import json
import os
import re
import subprocess
from importlib.resources import as_file, files
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from .target import TargetIdentity, read_target
from .update import Offer, scan_offers

_RELEASE_HISTORY = "https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information"
_RELEASE = re.compile(r"\b[0-9]{2}H[12]\b", re.I)
_HEADING = re.compile(r"Version ([0-9]{2}H[12]) \(OS build ([0-9]+)\)", re.I)


def select_offer(offers: list[Offer], identity: TargetIdentity) -> tuple[Offer, str]:
    if identity.installation_type.casefold() != "client":
        raise ValueError("Full client feature upgrades require Windows Client.")
    if not _RELEASE.fullmatch(identity.release):
        raise ValueError("Could not identify the installed Windows release.")
    candidates: list[tuple[Offer, str]] = []
    for offer in offers:
        if (
            offer.category != "features"
            or not offer.revision
            or not re.search(r"\bWindows 11\b", offer.title, re.I)
            or re.search(r"\b(KB[0-9]+|enablement)\b", offer.title, re.I)
        ):
            continue
        releases = _RELEASE.findall(offer.title)
        if len(releases) == 1 and releases[0].upper() > identity.release.upper():
            candidates.append((offer, releases[0].upper()))
    if not candidates:
        raise ValueError("No eligible full Windows 11 feature upgrade was offered.")
    if len(candidates) != 1:
        raise ValueError(
            "Multiple full feature upgrade offers matched this installation."
        )
    return candidates[0]


def release_build(
    history: str, offer: Offer, release: str, identity: TargetIdentity
) -> int:
    builds = set()
    for heading in BeautifulSoup(history, "html.parser").find_all(
        ["strong", "summary", "h2", "h3", "h4"]
    ):
        match = _HEADING.fullmatch(heading.get_text(" ", strip=True))
        if match and match[1].upper() == release:
            builds.add(int(match[2]))
    if len(builds) != 1:
        raise ValueError(
            f"Could not confirm the Windows {release} base build with Microsoft."
        )
    build = builds.pop()
    if build <= identity.build:
        raise ValueError(f"Windows {release} does not have a newer base build.")
    explicit = {
        int(value) for value in re.findall(r"\b([0-9]{5})\.[0-9]+\b", offer.title)
    }
    if explicit and explicit != {build}:
        raise ValueError(
            "The offer specifies a different base build from release history."
        )
    return build


def require_scan_success(code: int) -> None:
    code &= 0xFFFFFFFF
    if code == 0xC1900210:
        return
    message = {
        0xC1900200: "this device does not meet the upgrade requirements",
        0xC1900204: "Setup cannot keep apps and data",
        0xC1900208: "Setup found a compatibility block",
        0xC190020E: "Setup needs more free disk space",
        0xC1900215: "Setup could not select a matching Windows image",
    }.get(code, "Setup did not confirm compatibility")
    raise ValueError(f"{message} (0x{code:08X}). Check the Windows Setup Panther logs.")


def _scan_media(identity: TargetIdentity, iso: Path, digest: str, build: int) -> None:
    iso = iso.resolve(strict=True)
    if (
        not re.fullmatch(r"[A-Za-z]:", iso.drive)
        or iso.suffix.casefold() != ".iso"
        or not iso.is_file()
    ):
        raise ValueError("Supply a local Microsoft Windows installation ISO.")
    # Use the same native DISM and PowerShell view even under an x86 interpreter.
    native = identity.windows / "Sysnative"
    if not native.is_dir():
        native = identity.windows / "System32"
    powershell = native / "WindowsPowerShell/v1.0/powershell.exe"
    with (
        as_file(files("wuo").joinpath("feature_upgrade.ps1")) as script,
        subprocess.Popen(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script,
            ],
            env={
                **os.environ,
                "WUO_FEATURE_ISO": str(iso),
                "WUO_FEATURE_SHA256": digest,
                "WUO_FEATURE_SOURCE": f"{identity.build}.{identity.revision}",
                "WUO_FEATURE_BUILD": str(build),
                "WUO_FEATURE_ARCH": identity.architecture,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        ) as process,
    ):
        while True:
            try:
                stdout, stderr = process.communicate(timeout=30)
                break
            except subprocess.TimeoutExpired:
                print("The feature compatibility check is still running…", flush=True)
    if process.returncode:
        raise ValueError(stderr.strip() or "The native feature check failed.")
    report = json.loads(stdout)
    if (
        not isinstance(report, dict)
        or type(report.get("setup_code")) is not int
        or type(report.get("index")) is not int
        or type(report.get("revision")) is not int
        or report.get("build") != build
        or report.get("architecture") != identity.architecture
        or not isinstance(report.get("edition"), str)
        or not isinstance(report.get("language"), str)
    ):
        raise ValueError("The native feature check returned an invalid result.")
    require_scan_success(report["setup_code"])
    print(
        f"  Image {report['index']}, {report['edition']}, {report['architecture']}, "
        f"{report['language']}, build {report['build']}.{report['revision']}."
    )


async def check_feature(
    client: httpx.AsyncClient,
    identity: TargetIdentity,
    iso: Path,
    digest: str,
    *,
    rollback: bool,
) -> int:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ValueError("Supply the ISO's Microsoft-published SHA-256 digest.")
    print("Checking full feature upgrade offers from Windows Update Agent…", flush=True)
    try:
        offers, issue = await asyncio.to_thread(scan_offers, identity)
    except subprocess.TimeoutExpired as error:
        raise ValueError("Windows Update Agent scan timed out.") from error
    if issue:
        raise ValueError(issue)
    offer, release = select_offer(offers, identity)
    response = await client.get(_RELEASE_HISTORY)
    response.raise_for_status()
    build = release_build(response.text, offer, release, identity)
    if await asyncio.to_thread(read_target, read_loader=False) != identity:
        raise ValueError(
            "The running Windows identity changed during the feature check."
        )
    print(f"  Offered: {offer.title} ({offer.id}, revision {offer.revision})")
    policy = "enabled by default" if rollback else "skipped on request"
    print(f"  Planned rollback and backups for this feature upgrade: {policy}.")
    print("Checking the ISO and running Windows Setup compatibility scan…", flush=True)
    await asyncio.to_thread(_scan_media, identity, iso, digest.lower(), build)
    print("Windows Setup confirmed compatibility with keeping apps and data.")
    print("Feature upgrade check only. No installation or recovery job was started.")
    return 0
