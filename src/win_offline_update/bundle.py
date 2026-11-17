"""Package rules and verified recovery bundle preparation."""

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4

from .downloads import atomic_json, matches_digest, package_hashes, safe_name
from .hooks import Hook, load_profile
from .models import Update, UpdateFile
from .target import TargetIdentity, read_target

KB_ID = "950b2422-28ad-4493-a96c-03725fd0d8c2"
SSU = "SSU-26100.9971-x64.cab"
SSU_SHA256 = "b86aa6e031fa301464cd119f2f652c4192ad5b344ec6e1b4247d0d9ce528e764"
PACKAGE_PREFIXES = ("windows11.0-kb5043080-x64_", "windows11.0-kb5132061-x64_")
JOBS_PATH = Path("ProgramData/WinOfflineUpdate/jobs")


@dataclass(frozen=True, slots=True)
class ServicingPlan:
    files: tuple[UpdateFile, ...]
    commands: tuple[str, ...]
    target_revision: int


@dataclass(frozen=True, slots=True)
class BundlePlan:
    id: str
    update: Update
    target: TargetIdentity
    target_revision: int
    commands: tuple[str, ...]
    hooks: tuple[Hook, ...]
    paths: dict[str, str]
    size: dict[str, int]


def resolve_plan(update: Update, target: TargetIdentity) -> ServicingPlan:
    if update.id != KB_ID or update.kb != "KB5132061" or update.architecture != "x64":
        raise ValueError("This update currently requires manual servicing.")
    if (target.release, target.build, target.architecture) != (
        "25H2",
        26200,
        "x64",
    ) or not 6584 <= target.revision < 9974:
        raise ValueError(
            "KB5132061 requires a supported 25H2 x64 baseline older than 26200.9974."
        )
    selected = []
    for prefix in PACKAGE_PREFIXES:
        candidates = [
            item
            for item in update.files
            if re.fullmatch(re.escape(prefix) + r"[a-f0-9]{40}\.msu", item.name)
            and item.sha256
        ]
        if len(candidates) != 1:
            raise ValueError(
                "A Microsoft-verified checkpoint or cumulative package is missing."
            )
        selected.append(candidates[0])
    if len(update.files) != len(selected):
        raise ValueError("Unexpected packages in the Microsoft release file list.")
    return ServicingPlan(tuple(selected), (SSU, f"packages/{selected[-1].name}"), 9974)


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
            shutil.copyfile(source / file.name, path)
            sha256, sha1 = package_hashes(path)
            if not matches_digest(file, sha256, sha1):
                raise ValueError(f"Microsoft digest mismatch for {file.name}.")
            paths[relative] = sha256
            sizes[relative] = path.stat().st_size
        exe = shutil.which(extractor)
        if exe is None:
            raise ValueError(
                "The Microsoft package requires the 7-Zip command-line extractor."
            )
        extraction = subprocess.run(
            [exe, "e", str(bundle / plan.commands[-1]), SSU, f"-o{bundle}", "-y"],
            capture_output=True,
            check=False,
        )
        cab = bundle / SSU
        if extraction.returncode not in {0, 1} or not cab.is_file():
            raise ValueError(
                f"Could not extract the verified servicing stack (7-Zip {extraction.returncode})."
            )
        digest, _ = package_hashes(cab)
        if digest != SSU_SHA256:
            raise ValueError(
                "Extracted servicing stack does not match the supported package."
            )
        paths[SSU] = digest
        sizes[SSU] = cab.stat().st_size
        result = BundlePlan(
            job_id,
            update,
            target,
            plan.target_revision,
            plan.commands,
            hooks,
            paths,
            sizes,
        )
        if profile is not None:
            snapshot = bundle / "hooks.toml"
            shutil.copyfile(profile, snapshot)
            if load_profile(snapshot) != hooks:
                raise ValueError("The profile changed during preparation.")
            paths[snapshot.name], _ = package_hashes(snapshot)
        manifest = asdict(result)
        manifest["target"]["windows"] = str(target.windows)
        atomic_json(bundle / "plan.json", manifest)
        bundle.rename(output)
    return result


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
        if len(segments) not in {1, 2} or not all(safe_name(part) for part in segments):
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
        if (
            current.machine_guid != target["machine_guid"]
            or current.build != target["build"]
            or current.revision != manifest["target_revision"]
        ):
            raise ValueError(
                f"Expected update not verified on this installation. "
                f"Current build is {current.build}.{current.revision}."
            )
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
