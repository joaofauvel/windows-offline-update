"""Check whether Windows Setup can upgrade from a selected ISO."""

import asyncio
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from hashlib import sha256
from importlib.resources import as_file, files
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import httpx
from bs4 import BeautifulSoup

from .recovery import render_feature_precommit
from .target import TargetIdentity, read_target

if TYPE_CHECKING:
    from .hooks import Hook
    from .update import Offer

_RELEASE_HISTORY = "https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information"
_RELEASE = re.compile(r"\b[0-9]{2}H[12]\b", re.I)
_HEADING = re.compile(r"Version ([0-9]{2}H[12]) \(OS build ([0-9]+)\)", re.I)


@dataclass(frozen=True, slots=True)
class FeatureMedia:
    iso: Path
    sha256: str
    rollback: bool = True


@dataclass(frozen=True, slots=True)
class FeatureImage:
    index: int
    build: int
    revision: int
    edition: str
    architecture: str
    language: str


def feature_setup_assets(
    job: UUID,
    identity: TargetIdentity,
    media: FeatureMedia,
    image: FeatureImage,
    hooks: tuple[Hook, ...],
) -> tuple[bytes, bytes]:
    """Build the request and callback for the protected Windows continuation."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", media.sha256):
        raise ValueError("Supply the ISO's Microsoft-published SHA-256 digest.")
    if image.architecture != identity.architecture:
        raise ValueError("The verified image does not match the running architecture.")
    callback = render_feature_precommit(
        str(job), identity, image, hooks, rollback=media.rollback
    ).encode("ascii")
    request = {
        "schema": 1,
        "id": str(job),
        "machine": str(UUID(identity.machine_guid)),
        "source": f"{identity.build}.{identity.revision}",
        "iso": str(media.iso),
        "sha256": media.sha256.lower(),
        "image": asdict(image),
        "callback_sha256": sha256(callback).hexdigest(),
    }
    return json.dumps(request, sort_keys=True).encode("ascii"), callback


def offered_release(offer: Offer, identity: TargetIdentity) -> str | None:
    """Find offers eligible for an explicitly requested Setup preview."""
    if (
        identity.installation_type.casefold() != "client"
        or not _RELEASE.fullmatch(identity.release)
        or offer.category != "features"
        or offer.revision is None
        or offer.revision < 1
        or offer.kb_articles
        or not re.search(r"\bWindows 11\b", offer.title, re.I)
        or re.search(r"\b(KB[0-9]+|enablement)\b", offer.title, re.I)
    ):
        return None
    releases = [match.group() for match in _RELEASE.finditer(offer.title)]
    if len(releases) == 1 and releases[0].upper() > identity.release.upper():
        return releases[0].upper()
    return None


def select_offer(offers: list[Offer], identity: TargetIdentity) -> tuple[Offer, str]:
    candidates = [
        (offer, release)
        for offer in offers
        if (release := offered_release(offer, identity)) is not None
    ]
    if not candidates:
        raise ValueError("No eligible Windows 11 feature offer for a Setup preview.")
    if len(candidates) != 1:
        raise ValueError(
            "Multiple Windows Setup preview candidates matched this installation."
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


def _run_media(
    identity: TargetIdentity,
    iso: Path,
    digest: str,
    build: int | None,
    *,
    expected: FeatureImage | None = None,
) -> FeatureImage:
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
    # PowerShell 7 modules can shadow the native Windows PowerShell modules.
    native_env = {
        name: value
        for name, value in os.environ.items()
        if name.upper() != "PSMODULEPATH"
    }
    operation = "feature compatibility check"
    arguments = []
    if expected is not None:
        operation = "deferred feature preparation"
        arguments.append("-Prepare")
        native_env.update(
            WUO_FEATURE_INDEX=str(expected.index),
            WUO_FEATURE_REVISION=str(expected.revision),
            WUO_FEATURE_EDITION=expected.edition,
            WUO_FEATURE_LANGUAGE=expected.language,
        )
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
                *arguments,
            ],
            env={
                **native_env,
                "WUO_FEATURE_ISO": str(iso),
                "WUO_FEATURE_SHA256": digest,
                "WUO_FEATURE_SOURCE": f"{identity.build}.{identity.revision}",
                "WUO_FEATURE_BUILD": "" if build is None else str(build),
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
                print(f"The {operation} is still running…", flush=True)
    if process.returncode:
        raise ValueError(stderr.strip() or f"The native {operation} failed.")
    report = json.loads(stdout)
    if (
        not isinstance(report, dict)
        or type(report.get("setup_code")) is not int
        or type(report.get("index")) is not int
        or report["index"] < 1
        or type(report.get("revision")) is not int
        or report["revision"] < 0
        or type(report.get("build")) is not int
        or report["build"] <= identity.build
        or (build is not None and report["build"] != build)
        or report.get("architecture") != identity.architecture
        or not isinstance(report.get("edition"), str)
        or not isinstance(report.get("language"), str)
    ):
        raise ValueError("The native feature check returned an invalid result.")
    image = FeatureImage(
        index=report["index"],
        build=report["build"],
        revision=report["revision"],
        edition=report["edition"],
        architecture=report["architecture"],
        language=report["language"],
    )
    if expected is None:
        require_scan_success(report["setup_code"])
    elif report["setup_code"] != 0 or image != expected:
        raise ValueError("Windows Setup did not prepare the verified image.")
    print(
        f"  Image {image.index}, {image.edition}, {image.architecture}, "
        f"{image.language}, build {image.build}.{image.revision}."
    )
    return image


def _scan_media(
    identity: TargetIdentity, iso: Path, digest: str, build: int | None
) -> FeatureImage:
    return _run_media(identity, iso, digest, build)


def prepare_media(
    identity: TargetIdentity, media: FeatureMedia, image: FeatureImage
) -> FeatureImage:
    """Prepare the verified image in Windows without committing the upgrade."""
    if image.build <= identity.build or image.architecture != identity.architecture:
        raise ValueError(
            "The verified feature image does not match the running Windows."
        )
    return _run_media(
        identity, media.iso, media.sha256.lower(), image.build, expected=image
    )


async def check_feature(
    client: httpx.AsyncClient,
    identity: TargetIdentity,
    offers: list[Offer],
    media: FeatureMedia,
) -> FeatureImage:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", media.sha256):
        raise ValueError("Supply the ISO's Microsoft-published SHA-256 digest.")
    if identity.installation_type.casefold() != "client" or identity.build < 22000:
        raise ValueError("The ISO compatibility preview requires Windows 11 client.")
    build = None
    if offers:
        offer, release = select_offer(offers, identity)
        response = await client.get(_RELEASE_HISTORY)
        response.raise_for_status()
        build = release_build(response.text, offer, release, identity)
    if await asyncio.to_thread(read_target, read_loader=False) != identity:
        raise ValueError(
            "The running Windows identity changed during the feature check."
        )
    policy = "enabled by default" if media.rollback else "skipped on request"
    print(f"  Planned rollback and backups for this feature upgrade: {policy}.")
    if build is None:
        print(
            "Checking explicitly supplied media without a Windows Update feature offer."
        )
    print("Checking the ISO and running Windows Setup compatibility scan…", flush=True)
    image = await asyncio.to_thread(
        _scan_media, identity, media.iso, media.sha256.lower(), build
    )
    print("Windows Setup confirmed compatibility with keeping apps and data.")
    print("Feature upgrade check only. No installation or recovery job was started.")
    return image
