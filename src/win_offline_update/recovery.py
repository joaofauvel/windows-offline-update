"""Render a bounded stock WinRE recovery job."""

from importlib.resources import files
from typing import TYPE_CHECKING
from uuid import UUID

from .downloads import safe_name

if TYPE_CHECKING:
    from .bundle import BundlePlan
    from .hooks import Hook


def _hook_lines(hooks: tuple[Hook, ...]) -> str:
    if not hooks:
        return "exit /b 0"
    lines = ["call :open_hook_hive || exit /b 1"]
    for operation in ("inspect", "write"):
        for hook in hooks:
            path = "%HOOK_HIVE%"
            if hook.scope == "control-set":
                path += r"\ControlSet%CS%"
            expected = hex(hook.expected) if hook.expected is not None else ""
            lines.extend(
                (
                    rf'set "HOOK_KEY={path}\{hook.key}"',
                    f'set "HOOK_NAME={hook.name}"',
                    f'set "HOOK_NEW={hook.value:#x}"',
                    f'set "HOOK_EXPECTED={expected}"',
                    f"call :{operation}_hook || goto hook_failed",
                )
            )
    lines.extend(
        (
            r'"%SYS%\reg.exe" unload "%HOOK_HIVE%" >>"%LOG%" 2>&1 || exit /b 1',
            "exit /b 0",
        )
    )
    return "\n".join(lines)


def render_runner(plan: BundlePlan) -> str:
    packages: list[str] = []
    for relative, size in plan.size.items():
        if not all(safe_name(part) for part in relative.split("/")) or size <= 0:
            raise ValueError("Unsafe package filename or size in the recovery plan.")
        filename = relative.replace("/", "\\")
        packages.extend(
            (
                rf'if not exist "%BUNDLE%{filename}" exit /b 1',
                rf'for %%F in ("%BUNDLE%{filename}") do if not "%%~zF"=="{size}" exit /b 1',
            )
        )
    expected = [
        path.split("/", 1)[1] for path in plan.size if path.startswith("packages/")
    ]
    if plan.kind == "drivers":
        packages.extend(
            (
                'set "PACKAGE_COUNT=0"',
                r'for /r "%BUNDLE%packages" %%F in (*) do set /a PACKAGE_COUNT+=1 >nul',
                f'if not "%PACKAGE_COUNT%"=="{len(expected)}" exit /b 1',
            )
        )
    else:
        allowed = " ".join(f'if /i not "%%~nxF"=="{name}"' for name in expected)
        packages.append(rf'for %%F in ("%BUNDLE%packages\*") do {allowed} exit /b 1')
        if plan.kind == "packages":
            packages.append(r'for /d %%D in ("%BUNDLE%packages\*") do exit /b 1')
    stages: list[str] = []
    for relative in plan.commands:
        if relative not in plan.size:
            raise ValueError("Servicing command has no verified package.")
        if plan.kind == "drivers" and not (
            relative.startswith("packages/") and relative.lower().endswith(".inf")
        ):
            raise ValueError("Driver servicing requires a verified INF.")
        if plan.kind == "packages" and not (
            relative.startswith("packages/")
            and len(relative.split("/")) == 2
            and relative.lower().endswith((".msu", ".cab"))
        ):
            raise ValueError("CBS servicing requires a verified MSU/CAB package.")
        filename = relative.replace("/", "\\")
        stages.extend(
            (rf'set "PACKAGE=%BUNDLE%{filename}"', "call :dism_add || exit /b 1")
        )
    state_checks: list[str] = []
    if plan.kind == "packages":
        from .bundle import _canonical_identity

        if not plan.package_identities or len(set(plan.package_identities)) != len(
            plan.package_identities
        ):
            raise ValueError("CBS servicing requires a canonical package inventory.")
        for index, identity in enumerate(plan.package_identities, 1):
            _canonical_identity(identity, plan.target.architecture)
            state_checks.extend(
                (
                    f'set "CBS_EXPECTED={identity}"',
                    rf'set "CBS_FILE=%JOB%\packages-after-{index:04}.txt"',
                    "call :verify_cbs_package || exit /b 1",
                )
            )
    state_checks.append("exit /b 0")
    template = (
        files("win_offline_update")
        .joinpath("recovery.cmd.in")
        .read_text(encoding="ascii")
    )
    replacements = {
        "JOB": str(UUID(plan.id)),
        "MACHINE": str(UUID(plan.target.machine_guid)),
        "LOADER": "{" + str(UUID(plan.target.loader_guid)) + "}",
        "BUILD": str(plan.target.build),
        "BEFORE": hex(plan.target.revision),
        "PACKAGES": "\n".join(packages),
        "STAGES": "\n".join(stages),
        "PACKAGE_STATES": "\n".join(state_checks),
        "DISM_ADD": '/Add-Driver /Driver:"%PACKAGE%"'
        if plan.kind == "drivers"
        else '/Add-Package /PackagePath:"%PACKAGE%"',
        "PRE_HOOKS": _hook_lines(
            tuple(hook for hook in plan.hooks if hook.phase == "pre")
        ),
        "POST_HOOKS": _hook_lines(
            tuple(hook for hook in plan.hooks if hook.phase == "post")
        ),
    }
    for name, value in replacements.items():
        template = template.replace(f"@@{name}@@", value)
    return template.replace("\n", "\r\n")
