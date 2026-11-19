"""Package rules and verified recovery bundle preparation."""

import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from .downloads import (
    atomic_json,
    checked_url,
    matches_digest,
    package_hashes,
    safe_name,
)
from .hooks import Hook, load_profile
from .models import Update, UpdateFile
from .recovery import render_runner
from .target import TargetIdentity, read_target

JOBS_PATH = Path("ProgramData/WinOfflineUpdate/jobs")
_CBS_IDENTITY = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}~[a-fA-F0-9]{16}~"
    r"(?P<architecture>amd64|x86|arm64|neutral)~[A-Za-z0-9-]{0,35}~"
    r"[0-9]+(?:\.[0-9]+){3}"
)
_CBS_ARCHITECTURES = {"x64": "amd64", "x86": "x86", "arm64": "arm64"}


@dataclass(frozen=True, slots=True)
class ServicingPlan:
    files: tuple[UpdateFile, ...]
    commands: tuple[str, ...]
    target_revision: int
    kind: Literal["packages"] = "packages"


@dataclass(frozen=True, slots=True)
class BundlePlan:
    id: str
    update: Update | None
    target: TargetIdentity
    target_revision: int
    commands: tuple[str, ...]
    hooks: tuple[Hook, ...]
    paths: dict[str, str]
    size: dict[str, int]
    kind: Literal["cumulative", "drivers", "packages"] = "cumulative"
    package_identities: tuple[str, ...] = ()
    package_payload_identities: tuple[str, ...] = ()


def resolve_plan(update: Update, target: TargetIdentity) -> ServicingPlan:
    """Check eligibility without reading files or consulting the running OS."""
    if update.classification.casefold() == "drivers":
        raise ValueError("Driver CABs require driver servicing, not package servicing.")
    if target.architecture not in _CBS_ARCHITECTURES or update.architecture not in {
        None,
        target.architecture,
        "neutral",
    }:
        raise ValueError("Package architecture does not match the Windows target.")
    try:
        UUID(update.id)
    except (ValueError, AttributeError) as error:
        raise ValueError(
            "This update currently requires manual servicing: invalid update ID."
        ) from error
    if not update.files:
        raise ValueError(
            "No standalone MSU/CAB package is available for offline servicing."
        )
    names: set[str] = set()
    for file in update.files:
        if not safe_name(file.name) or file.name.casefold() in names:
            raise ValueError("Unsafe or duplicate offline package filename.")
        names.add(file.name.casefold())
        if Path(file.name).suffix.casefold() not in {".msu", ".cab"}:
            raise ValueError(
                "Offline package servicing supports CBS MSU/CAB payloads only."
            )
        checked_url(file.url)
        if not (file.sha256 or file.sha1) or any(
            digest is not None and re.fullmatch(pattern, digest) is None
            for digest, pattern in (
                (file.sha256, r"[a-f0-9]{64}"),
                (file.sha1, r"[a-f0-9]{40}"),
            )
        ):
            raise ValueError("The offline package has no valid Microsoft digest.")
    return ServicingPlan(
        update.files,
        tuple(f"packages/{file.name}" for file in update.files),
        target.revision,
        kind="packages",
    )


def _canonical_identity(identity: str, architecture: str) -> str:
    match = _CBS_IDENTITY.fullmatch(identity)
    if match is None:
        raise ValueError("Windows returned an invalid canonical CBS package identity.")
    if match["architecture"] not in {_CBS_ARCHITECTURES.get(architecture), "neutral"}:
        raise ValueError(
            "The CBS package architecture does not match the Windows target."
        )
    return identity


def _package_info(
    target: TargetIdentity, *, path: Path | None = None, identity: str | None = None
) -> tuple[str, str]:
    """Read package metadata only; never stage or install a package here."""
    dism = target.windows / "Sysnative/dism.exe"
    if not dism.is_file():
        dism = target.windows / "System32/dism.exe"
    argument = (
        f"/PackagePath:{path}" if path is not None else f"/PackageName:{identity}"
    )
    try:
        result = subprocess.run(
            [str(dism), "/English", "/Online", "/Get-PackageInfo", argument],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise ValueError(
            "Native Windows DISM is required to inspect CBS packages."
        ) from error
    if result.returncode:
        raise ValueError(
            f"Windows DISM could not inspect the CBS package ({result.returncode}): "
            f"{path.name if path is not None else identity}"
        )
    identities = re.findall(
        r"^\s*Package Identity\s*:\s*(.*?)\s*$", result.stdout, re.M
    )
    states = re.findall(r"^\s*State\s*:\s*(.*?)\s*$", result.stdout, re.M)
    if len(identities) != 1 or len(states) != 1:
        raise ValueError(
            "Windows returned incomplete CBS package identity/state metadata."
        )
    if states[0].casefold() not in {
        "not present",
        "uninstall pending",
        "staged",
        "resolved",
        "removed",
        "installed",
        "install pending",
        "superseded",
        "partially installed",
    }:
        raise ValueError("Windows returned an unrecognized CBS package state.")
    actual = _canonical_identity(identities[0], target.architecture)
    if path is not None:
        applicability = re.findall(
            r"^\s*Applicable\s*:\s*(.*?)\s*$", result.stdout, re.M
        )
        if len(applicability) != 1 or applicability[0].casefold() != "yes":
            raise ValueError(
                "Windows DISM did not confirm package applicability to this target: "
                f"{path.name} ({applicability[0] if len(applicability) == 1 else 'missing metadata'})."
            )
    if identity is not None and actual.casefold() != identity.casefold():
        raise ValueError("Windows returned a different CBS package identity.")
    return actual, states[0]


def _archive_members(exe: str, path: Path) -> tuple[str, ...]:
    listing = subprocess.run(
        [exe, "l", "-slt", "-ba", "-sccUTF-8", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    members = re.findall(r"^Path = (.+)$", listing.stdout, re.M)
    if (
        listing.returncode != 0
        or not members
        or len({member.casefold() for member in members}) != len(members)
        or re.search(r"^(?:Symbolic Link|Hard Link) =", listing.stdout, re.M)
    ):
        raise ValueError(
            "Package archive has an unsafe or unreadable member inventory."
        )
    return tuple(members)


def _extract_member(exe: str, archive: Path, member: str, output: Path) -> Path:
    if not safe_name(member):
        raise ValueError("Unsafe package archive member filename.")
    extraction = subprocess.run(
        [exe, "e", str(archive), member, f"-o{output}", "-y"],
        capture_output=True,
        check=False,
    )
    extracted = output / member
    if (
        extraction.returncode != 0
        or not extracted.is_file()
        or extracted.is_symlink()
        or extracted.is_junction()
        or not extracted.resolve().is_relative_to(output.resolve())
    ):
        raise ValueError("Could not extract a regular package metadata file.")
    return extracted


def _manifest_identity(path: Path, architecture: str) -> str:
    content = path.read_bytes()
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("Unsupported CBS manifest XML declarations.")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        raise ValueError("The main CBS package manifest is not valid XML.") from error
    namespace = "{urn:schemas-microsoft-com:asm.v3}"
    identities = root.findall(f"{namespace}assemblyIdentity")
    if (
        root.tag != f"{namespace}assembly"
        or len(identities) != 1
        or len(root.findall(f"{namespace}package")) != 1
    ):
        raise ValueError(
            "The main CBS package has no unambiguous root package identity."
        )
    fields = identities[0].attrib
    try:
        language = fields["language"]
        identity = "~".join(
            (
                fields["name"],
                fields["publicKeyToken"],
                fields["processorArchitecture"],
                "" if language == "neutral" else language,
                fields["version"],
            )
        )
    except KeyError as error:
        raise ValueError("The root CBS package identity is incomplete.") from error
    return _canonical_identity(identity, architecture)


def _msu_identities(
    path: Path, target: TargetIdentity, scratch: Path, extractor: str
) -> tuple[str, ...]:
    exe = shutil.which(extractor)
    if exe is None:
        raise ValueError("MSU inspection requires the 7-Zip command-line extractor.")
    members = _archive_members(exe, path)
    if not all(safe_name(member) for member in members):
        raise ValueError("MSU archive has unsafe member filenames or subdirectories.")
    scratch.mkdir()
    identities: list[str] = []
    image_metadata = False
    image_members = [
        member
        for member in members
        if Path(member).suffix.casefold() in {".wim", ".esd"}
    ]
    payloads = [
        member
        for member in members
        if Path(member).suffix.casefold() in {".cab", ".wim", ".esd"}
        and member.casefold() != "wsusscan.cab"
    ]
    for index, member in enumerate(payloads):
        archive = _extract_member(exe, path, member, scratch)
        inner = {name.casefold(): name for name in _archive_members(exe, archive)}
        if "update.mum" not in inner:
            if archive.suffix.casefold() == ".wim":
                raise ValueError(
                    "The main WIM package has no root CBS package manifest."
                )
            # Deployment helpers and aggregated/MSIX metadata are not CBS
            # packages. They remain inside the original MSU for DISM to use.
            continue
        if archive.suffix.casefold() == ".cab":
            identities.append(_package_info(target, path=archive)[0])
            continue
        if "update.cat" not in inner:
            raise ValueError("The main WIM/ESD package has no root package catalog.")
        metadata = scratch / f"metadata-{index}"
        metadata.mkdir()
        manifest = _extract_member(exe, archive, inner["update.mum"], metadata)
        _extract_member(exe, archive, inner["update.cat"], metadata)
        expected = _manifest_identity(manifest, target.architecture)
        try:
            actual, _ = _package_info(target, path=metadata)
        except ValueError as error:
            raise ValueError(
                "Native DISM could not verify the expanded main WIM/ESD package "
                f"metadata; this package format requires another servicing route: {error}"
            ) from error
        if actual.casefold() != expected.casefold():
            raise ValueError(
                "The main CBS manifest identity differs from native DISM metadata."
            )
        identities.append(actual)
        image_metadata = True
    if not identities or (image_members and not image_metadata):
        raise ValueError("The MSU contains no verified main CBS package metadata.")
    return tuple(identities)


def _inspect_packages(
    plan: ServicingPlan,
    target: TargetIdentity,
    staging: Path,
    scratch: Path,
    extractor: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Identify each complete payload; DISM orders checkpoint MSUs at apply time."""
    inventory: list[tuple[str, tuple[str, ...]]] = []
    for index, file in enumerate(plan.files):
        path = staging / "packages" / file.name
        identities = (
            (_package_info(target, path=path)[0],)
            if path.suffix.casefold() == ".cab"
            else _msu_identities(
                path, target, scratch / f"inspection-{index}", extractor
            )
        )
        if len(set(identities)) != len(identities):
            raise ValueError("The payload contains duplicate CBS package identities.")
        inventory.append((f"packages/{file.name}", identities))
    identities = tuple(
        dict.fromkeys(identity for _, group in inventory for identity in group)
    )
    # Multiple MSUs from a Catalog entry can be a checkpoint chain. Select the
    # highest identity version of the same package family as the target; retain
    # the originals beside it so DISM discovers prerequisites itself.
    if len(inventory) > 1:
        families = [
            {identity.rsplit("~", 1)[0].casefold() for identity in group}
            for _, group in inventory
        ]
        common = set.intersection(*families)
        common = {
            family
            for family in common
            if not family.split("~", 1)[0].startswith("package_for_servicingstack")
        }
        if len(common) != 1 or any(
            not name.lower().endswith(".msu") for name, _ in inventory
        ):
            raise ValueError(
                "Multiple independent CBS payloads need an explicit prerequisite order; "
                "prepare them as separate update jobs."
            )

        # SSUs can differ between checkpoint MSUs; a shared family proves the
        # target chain without using a title, filename, KB or OS-build table.
        def version(item: tuple[str, tuple[str, ...]]) -> tuple[int, ...]:
            return max(
                tuple(int(part) for part in identity.rsplit("~", 1)[1].split("."))
                for identity in item[1]
                if identity.rsplit("~", 1)[0].casefold() in common
            )

        inventory.sort(key=version)
        if len({version(item) for item in inventory}) != len(inventory):
            raise ValueError(
                "The MSUs do not identify distinct checkpoint package versions."
            )
        commands = (inventory[-1][0],)
    else:
        commands = (inventory[0][0],)
    # A checkpoint or combined MSU can carry dependency packages which DISM
    # legitimately skips when a newer dependency already satisfies the image.
    # Require the selected target's main roots, not registration of every CAB
    # extracted from all prerequisites. Retain the complete payload audit below.
    target_identities = inventory[-1][1]
    required = tuple(
        identity
        for identity in target_identities
        if not identity.split("~", 1)[0]
        .casefold()
        .startswith("package_for_servicingstack")
    )
    # A standalone servicing-stack update is itself the requested target.
    return commands, required or target_identities, identities


def read_update_id(source: Path) -> str:
    with (source / "manifest.json").open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    try:
        return str(UUID(manifest["update"]["id"]))
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError(
            "No valid Microsoft Catalog update ID in the download manifest."
        ) from error


def prepare(
    update: Update,
    target: TargetIdentity,
    source: Path,
    output: Path,
    *,
    profile: Path | None = None,
    extractor: str = "7z.exe",
) -> BundlePlan:
    plan = resolve_plan(update, target)
    hooks = load_profile(profile) if profile is not None else ()
    if output.exists():
        raise ValueError("Choose a new directory for the recovery bundle.")
    output.parent.mkdir(parents=True, exist_ok=True)
    job_id = str(uuid4())
    paths: dict[str, str] = {}
    sizes: dict[str, int] = {}
    with tempfile.TemporaryDirectory(dir=output.parent) as scratch:
        bundle = Path(scratch) / "bundle"
        (bundle / "packages").mkdir(parents=True)
        for file in plan.files:
            relative = f"packages/{file.name}"
            path = bundle / relative
            original = source / file.name
            if original.is_symlink() or not original.is_file():
                raise ValueError(
                    f"The package is not a regular source file: {file.name}"
                )
            shutil.copyfile(original, path)
            sha256, sha1 = package_hashes(path)
            if not matches_digest(file, sha256, sha1):
                raise ValueError(f"Microsoft digest mismatch for {file.name}.")
            paths[relative] = sha256
            sizes[relative] = path.stat().st_size
        commands, identities, payload_identities = _inspect_packages(
            plan, target, bundle, Path(scratch), extractor
        )
        result = BundlePlan(
            job_id,
            update,
            target,
            target.revision,
            commands,
            hooks,
            paths,
            sizes,
            kind="packages",
            package_identities=identities,
            package_payload_identities=payload_identities,
        )
        _publish_bundle(bundle, output, result, profile)
    return result


def prepare_drivers(
    sources: list[Path],
    target: TargetIdentity,
    output: Path,
    *,
    profile: Path | None = None,
) -> BundlePlan:
    from .driver_bundle import stage_driver_exports

    if output.exists():
        raise ValueError("Choose a new directory for the recovery bundle.")
    hooks = load_profile(profile) if profile is not None else ()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as scratch:
        bundle = Path(scratch) / "bundle"
        bundle.mkdir()
        payload = stage_driver_exports(sources, target, bundle)
        result = BundlePlan(
            str(uuid4()),
            None,
            target,
            target.revision,
            payload.commands,
            hooks,
            payload.paths,
            payload.size,
            kind="drivers",
        )
        _publish_bundle(bundle, output, result, profile)
    return result


def _publish_bundle(
    staging: Path, output: Path, plan: BundlePlan, profile: Path | None
) -> None:
    if profile is not None:
        snapshot = staging / "hooks.toml"
        shutil.copyfile(profile, snapshot)
        if load_profile(snapshot) != plan.hooks:
            raise ValueError("The profile changed during preparation.")
        plan.paths[snapshot.name], _ = package_hashes(snapshot)
        metadata = profile.with_name("hooks.json")
        if metadata.is_file():
            from .hook_selection import read_selection

            read_selection(profile)
            shutil.copyfile(metadata, staging / "hooks.json")
            read_selection(snapshot)
            plan.paths["hooks.json"], _ = package_hashes(staging / "hooks.json")
    summary = ["Hooks: none."]
    if plan.hooks:
        from .hook_selection import read_selection

        selection = read_selection(profile)
        summary = ["Enabled hook profiles"]
        summary.extend(
            f"  {origin.name}  revision={origin.revision}  sha256={origin.sha256}"
            for origin in selection.origins
        )
        for hook in plan.hooks:
            registry_root = (
                "SYSTEM" if hook.scope == "system" else r"SYSTEM\<selected control set>"
            )
            summary.append(
                f"  {hook.phase}: {registry_root}\\{hook.key} /v {hook.name} = {hook.value:#x}"
            )
    summary_file = staging / "hooks-summary.txt"
    summary_file.write_bytes(
        ("\r\n".join(summary) + "\r\n").encode("ascii", errors="backslashreplace")
    )
    plan.paths[summary_file.name], _ = package_hashes(summary_file)
    script = staging / "recovery.cmd"
    script.write_bytes(render_runner(plan).encode("ascii"))
    plan.paths[script.name], _ = package_hashes(script)
    manifest = asdict(plan)
    manifest["target"]["windows"] = str(plan.target.windows)
    atomic_json(staging / "plan.json", manifest)
    root = plan.target.windows.parent.resolve()
    jobs = root / JOBS_PATH
    if not jobs.resolve().is_relative_to(root):
        raise ValueError(
            "The recovery job directory resolves outside the target Windows volume."
        )
    job = jobs / plan.id
    job.mkdir(parents=True)
    marker = job / "target.id"
    plan_digest = job / "plan.sha256"
    try:
        marker.write_bytes(f"{plan.id}\r\n".encode("ascii"))
        digest, _ = package_hashes(staging / "plan.json")
        plan_digest.write_text(f"{digest}\n", encoding="ascii")
        staging.rename(output)
    except OSError:
        marker.unlink(missing_ok=True)
        plan_digest.unlink(missing_ok=True)
        job.rmdir()
        raise


def verify(bundle: Path, *, post_boot: bool = False) -> None:
    with (bundle / "plan.json").open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    try:
        recorded = manifest["paths"]
    except (KeyError, TypeError) as error:
        raise ValueError("Invalid recovery plan.") from error
    if not isinstance(recorded, dict) or not recorded:
        raise ValueError("The recovery plan has no recorded files.")
    for relative, digest in recorded.items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise ValueError("Invalid file or digest in recovery plan.")
        segments = relative.split("/")
        if (len(segments) > 1 and segments[0] != "packages") or not all(
            safe_name(part) for part in segments
        ):
            raise ValueError("Unsafe filename in recovery plan.")
        path = bundle / relative
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(bundle.resolve())
        ):
            raise ValueError(f"Bundle file is missing or unsafe: {relative}")
        actual, _ = package_hashes(path)
        if actual != digest:
            raise ValueError(f"Bundle file digest mismatch: {relative}")
    if post_boot:
        import winreg

        current = read_target(read_loader=False)
        target = manifest["target"]
        if current.machine_guid != target["machine_guid"]:
            raise ValueError(
                "Expected update belongs to a different Windows installation."
            )
        if manifest.get("kind") == "packages":
            _verify_packages_post_boot(current, manifest)
        elif (
            current.build != target["build"]
            or current.revision != manifest["target_revision"]
        ):
            raise ValueError(
                f"Expected update not verified on this installation. "
                f"Current build is {current.build}.{current.revision}."
            )
        if manifest.get("kind") == "drivers":
            _verify_drivers_post_boot(current, manifest)
        for hook in manifest["hooks"]:
            if hook["phase"] != "post":
                continue
            root = (
                r"SYSTEM\CurrentControlSet"
                if hook["scope"] == "control-set"
                else "SYSTEM"
            )
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, f"{root}\\{hook['key']}"
            ) as key:
                value, kind = winreg.QueryValueEx(key, hook["name"])
            if kind != winreg.REG_DWORD or value != hook["value"]:
                raise ValueError(
                    f"Post-boot DWORD assertion failed: {hook['key']} / {hook['name']}"
                )


def _verify_packages_post_boot(
    current: TargetIdentity, manifest: dict[str, object]
) -> None:
    job_id = str(UUID(str(manifest.get("id"))))
    ready = current.windows.parent / JOBS_PATH / job_id / "state.ready"
    if not ready.is_file() or ready.read_text(encoding="ascii").strip() != job_id:
        raise ValueError("The offline package job has no successful servicing marker.")
    identities = manifest.get("package_identities")
    if (
        not isinstance(identities, list)
        or not identities
        or not all(isinstance(identity, str) for identity in identities)
    ):
        raise ValueError("The offline package job has no canonical CBS inventory.")
    for identity in identities:
        if not isinstance(identity, str):
            raise ValueError("Invalid canonical CBS package identity.")
        _canonical_identity(identity, current.architecture)
        _, state = _package_info(current, identity=identity)
        if state.casefold() not in {"installed", "superseded"}:
            raise ValueError(
                f"CBS package is not verified Installed/Superseded after boot: {identity} ({state})."
            )


def _verify_drivers_post_boot(
    current: TargetIdentity, manifest: dict[str, object]
) -> None:
    job_id = str(UUID(str(manifest.get("id"))))
    ready = current.windows.parent / JOBS_PATH / job_id / "state.ready"
    if not ready.is_file() or ready.read_text(encoding="ascii").strip() != job_id:
        raise ValueError("The offline driver job has no successful servicing marker.")
    repository = current.windows / "System32/DriverStore/FileRepository"
    commands = manifest.get("commands")
    paths = manifest.get("paths")
    if not isinstance(commands, list) or not commands or not isinstance(paths, dict):
        raise ValueError("Invalid driver job inventory.")
    for relative in commands:
        if not isinstance(relative, str) or relative not in paths:
            raise ValueError("Invalid driver job INF record.")
        name = Path(relative).name
        if not safe_name(name) or Path(name).suffix.casefold() != ".inf":
            raise ValueError("Invalid driver job filename.")
        if not any(
            stored.is_file() and package_hashes(stored)[0] == paths[relative]
            for stored in repository.glob(f"{name}_*/{name}")
        ):
            raise ValueError(f"Driver INF was not found in the driver store: {name}")
