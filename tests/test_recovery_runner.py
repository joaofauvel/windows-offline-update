import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from win_offline_update.bundle import BundlePlan
from win_offline_update.hooks import Hook
from win_offline_update.models import Update
from win_offline_update.target import TargetIdentity


@pytest.fixture
def plan() -> BundlePlan:
    return BundlePlan(
        "11111111-1111-1111-1111-111111111111",
        Update("test", "Windows update"),
        TargetIdentity(
            "25H2",
            26200,
            9457,
            "x64",
            Path("C:/Windows"),
            "33333333-3333-3333-3333-333333333333",
            "22222222-2222-2222-2222-222222222222",
        ),
        9974,
        ("SSU-26100.9971-x64.cab", "packages/target.msu"),
        (Hook("post", r"Services\USBHUB3", "Start", 0),),
        {},
        {
            "SSU-26100.9971-x64.cab": 10,
            "packages/target.msu": 20,
            "packages/checkpoint.msu": 30,
        },
    )


@pytest.mark.skipif(os.name != "nt", reason="Runs Windows CMD")
def test_recovery_runner_refuses_running_windows(
    plan: BundlePlan, tmp_path: Path
) -> None:
    from win_offline_update.recovery import render_runner

    script = tmp_path / "recovery.cmd"
    script.write_bytes(render_runner(plan).encode("ascii"))
    result = subprocess.run(
        [
            Path(os.environ["SYSTEMROOT"]) / "System32" / "cmd.exe",
            "/d",
            "/c",
            r".\recovery.cmd",
            "apply",
            "W:",
            "S:",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Start this command from WinRE" in result.stdout
    assert list(tmp_path.iterdir()) == [script]


def test_runner_places_preflight_before_mutating_commands(plan: BundlePlan) -> None:
    from win_offline_update.recovery import render_runner

    script = render_runner(plan)
    assert script.index("call :validate_target") < script.index("call :apply_packages")
    assert script.index("call :inspect_packages") < script.index("call :pre_hooks")
    assert script.index("call :pre_hooks") < script.index("call :apply_packages")
    assert script.index("call :apply_packages") < script.index("call :post_hooks")
    stages = script.split("\r\n:apply_packages\r\n", 1)[1].split(
        "\r\n:dism_add\r\n", 1
    )[0]
    assert stages.index("SSU-26100.9971-x64.cab") < stages.index("target.msu")
    assert "state.ready" in script
    assert "@@" not in script


def test_repair_uses_post_hooks_without_installing_packages(plan: BundlePlan) -> None:
    from win_offline_update.recovery import render_runner

    script = render_runner(plan)
    repair = script.split("\r\n:repair\r\n", 1)[1].split("\r\n:validate_target\r\n", 1)[
        0
    ]
    assert "call :post_hooks" in repair
    assert "call :apply_packages" not in repair
    assert "state.ready" in repair
    assert "/Add-Package" not in repair


@pytest.fixture
def recovery_lab(
    plan: BundlePlan, tmp_path: Path
) -> Iterator[tuple[Callable[..., subprocess.CompletedProcess[str]], Path, Path]]:
    if os.name != "nt":
        pytest.skip("Runs Windows CMD")
    from win_offline_update.recovery import render_runner

    windows = Path(os.environ["SYSTEMROOT"])
    csc = windows / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not csc.exists() or any(Path(f"{letter}:/").exists() for letter in "XWS"):
        pytest.skip(
            "Requires .NET Framework csc.exe and unused X, W and S drive letters"
        )
    system = windows / "System32"
    fake_winre = tmp_path / "winre"
    fake_system = fake_winre / "Windows/System32"
    fake_system.mkdir(parents=True)
    (fake_winre / "Windows/Temp").mkdir()
    fake_os = tmp_path / "target"
    config = fake_os / "Windows/System32/Config"
    config.mkdir(parents=True)
    (config / "SOFTWARE").touch()
    (config / "SYSTEM").touch()
    fake_efi = tmp_path / "esp"
    (fake_efi / "EFI/Microsoft/Boot").mkdir(parents=True)
    (fake_efi / "EFI/Microsoft/Boot/BCD").touch()
    job = fake_os / "ProgramData/WinOfflineUpdate/jobs" / plan.id
    job.mkdir(parents=True)
    (job / "target.id").write_text(f"{plan.id}\n", encoding="ascii")
    source = Path(__file__).with_name("fixtures") / "WinReTools.cs"
    executable = fake_system / "reg.exe"
    subprocess.run(
        [csc, "/nologo", f"/out:{executable}", source], check=True, capture_output=True
    )
    for name in ("dism.exe", "bcdedit.exe"):
        shutil.copyfile(executable, fake_system / name)
    shutil.copyfile(system / "findstr.exe", fake_system / "findstr.exe")
    bundle = tmp_path / "bundle"
    (bundle / "packages").mkdir(parents=True)
    for relative, size in plan.size.items():
        (bundle / relative).write_bytes(b"0" * size)
    # Keep the actual Windows environment for the .NET test executables.
    runner = render_runner(plan).replace(
        r'if /i not "%SystemRoot%"=="X:\Windows" goto not_winre',
        f'if /i not "%SystemRoot%"=="{windows}" goto not_winre',
        1,
    )
    (bundle / "recovery.cmd").write_bytes(runner.encode("ascii"))
    trace = tmp_path / "trace.txt"
    subst = system / "subst.exe"
    mapped = []
    try:
        for letter, path in zip(
            ("X:", "W:", "S:"), (fake_winre, fake_os, fake_efi), strict=True
        ):
            subprocess.run([subst, letter, path], check=True, capture_output=True)
            mapped.append(letter)

        def run(mode: str, **settings: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [system / "cmd.exe", "/d", "/c", r".\recovery.cmd", mode, "W:", "S:"],
                cwd=bundle,
                env={
                    **os.environ,
                    "WOU_TEST_TRACE": str(trace),
                    **settings,
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=40,
            )

        yield run, job, trace
    finally:
        for letter in reversed(mapped):
            subprocess.run([subst, letter, "/D"], check=True, capture_output=True)


def test_cmd_apply_handles_restart_required_and_repairs_only_hooks(
    recovery_lab,
) -> None:
    run, job, trace = recovery_lab
    result = run("apply")
    assert result.returncode == 0, (result.stdout, result.stderr, trace.read_text())
    assert (job / "state.ready").exists()
    commands = trace.read_text()
    assert commands.count("/Add-Package") == 2
    assert "USBHUB3" in (job / "post-values.txt").read_text()
    assert (job / "recovery.log").read_text().count("DISM exit status 3010") == 2
    result = run("repair-hooks")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert trace.read_text().count("/Add-Package") == 2


def test_cmd_wrong_efi_cannot_apply(recovery_lab) -> None:
    run, job, trace = recovery_lab
    result = run("apply", WOU_TEST_BAD_EFI="1")
    assert result.returncode == 3
    assert not (job / "apply.started").exists()
    assert "/Add-Package" not in trace.read_text()


def test_cmd_failed_dism_is_journaled_and_requires_inspection(recovery_lab) -> None:
    run, job, trace = recovery_lab
    result = run("apply", WOU_TEST_DISM_STATUS="5")
    assert result.returncode == 5
    assert (job / "apply.started").is_dir()
    assert "DISM exit status 5" in (job / "recovery.log").read_text()
    assert not (job / "state.ready").exists()
    assert run("apply").returncode == 4
    assert trace.read_text().count("/Add-Package") == 1


def test_cmd_failed_hook_unloads_system_hive(recovery_lab) -> None:
    run, job, trace = recovery_lab
    result = run("apply", WOU_TEST_HOOK_ERROR="1")
    assert result.returncode == 5
    assert "reg unload HKLM\\WOU_SYSTEM_" in trace.read_text()
    assert not (job / "state.ready").exists()
