import json
import os
import subprocess
from dataclasses import replace
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from uuid import UUID

import pytest

from win_offline_update.bundle import BundlePlan
from win_offline_update.hooks import Hook
from win_offline_update.recovery import render_runner
from win_offline_update.target import TargetIdentity

JOB = "11111111-1111-1111-1111-111111111111"
OS_PARTITION = "22222222-2222-2222-2222-222222222222"
EFI_PARTITION = "33333333-3333-3333-3333-333333333333"
MACHINE = "44444444-4444-4444-4444-444444444444"
LOADER = "55555555-5555-5555-5555-555555555555"


def test_driver_runner_keeps_both_hook_phases_for_a_general_profile() -> None:
    command = "packages/0001/vendor/sample.inf"
    plan = BundlePlan(
        JOB,
        None,
        TargetIdentity("25H2", 26200, 9974, "x64", Path("C:/Windows"), MACHINE, LOADER),
        9974,
        (command,),
        (
            Hook("pre", r"Example\Device", "Flag1", 7, scope="system"),
            Hook("post", r"Services\Example", "Flag2", 9),
        ),
        {},
        {command: 42, "packages/0001/vendor/sample.cat": 97},
        kind="drivers",
    )
    runner = render_runner(plan)
    assert r'set "PACKAGE=%BUNDLE%packages\0001\vendor\sample.inf"' in runner
    assert '/Add-Driver /Driver:"%PACKAGE%"' in runner
    assert "/ForceUnsigned" not in runner
    assert "/Add-Package" not in runner
    assert r'"HOOK_KEY=%HOOK_HIVE%\Example\Device"' in runner
    assert r'"HOOK_KEY=%HOOK_HIVE%\ControlSet%CS%\Services\Example"' in runner
    assert runner.index("call :pre_hooks") < runner.index("call :apply_packages")
    assert runner.index("call :apply_packages") < runner.index("call :post_hooks")
    with pytest.raises(ValueError, match="INF"):
        render_runner(replace(plan, commands=("packages/0001/vendor/sample.cat",)))


@pytest.mark.skipif(os.name != "nt", reason="Uses Windows path semantics")
def test_winpe_plan_binds_partitions_bundle_hashes_and_check_mode(
    tmp_path: Path,
) -> None:
    from win_offline_update.downloads import package_hashes
    from win_offline_update.winpe import write_startup

    windows = tmp_path / "Windows"
    windows.mkdir()
    bundle = tmp_path / "WinOfflineJobs" / "my bundle"
    bundle.mkdir(parents=True)
    (bundle / "recovery.cmd").write_text("@echo off\nexit /b 0\n", encoding="ascii")
    digest, _ = package_hashes(bundle / "recovery.cmd")
    target = TargetIdentity("25H2", 26200, 9974, "x64", windows, MACHINE, LOADER)
    manifest = {
        "id": JOB,
        "kind": "drivers",
        "target": {
            "machine_guid": MACHINE,
            "loader_guid": LOADER,
            "release": "25H2",
            "build": 26200,
            "revision": 9974,
            "architecture": "x64",
        },
        "paths": {"recovery.cmd": digest},
    }
    plan_path = bundle / "plan.json"
    plan_path.write_text(json.dumps(manifest), encoding="utf-8")
    prepared_job = tmp_path / "ProgramData" / "WinOfflineUpdate" / "jobs" / JOB
    prepared_job.mkdir(parents=True)
    (prepared_job / "target.id").write_text(JOB, encoding="ascii")
    (prepared_job / "plan.sha256").write_text(
        sha256(plan_path.read_bytes()).hexdigest(), encoding="ascii"
    )
    output = tmp_path / "mounted" / "Windows" / "System32"
    output.mkdir(parents=True)
    write_startup(
        bundle, output, target, UUID(OS_PARTITION), UUID(EFI_PARTITION), check_only=True
    )
    recorded = json.loads((output / "wuo-job.json").read_text(encoding="utf-8"))
    assert recorded["job"] == JOB
    assert recorded["partitions"] == {"windows": OS_PARTITION, "efi": EFI_PARTITION}
    assert recorded["bundle"] == r"WinOfflineJobs\my bundle"
    assert recorded["files"]["recovery.cmd"] == digest
    assert recorded["mode"] == "check"
    assert "wpeinit" in (output / "startnet.cmd").read_text(encoding="ascii")
    assert (output / "wuo-startup.ps1").is_file()
    (bundle / "recovery.cmd").write_text("changed", encoding="ascii")
    with pytest.raises(ValueError, match="digest mismatch"):
        write_startup(
            bundle,
            output,
            target,
            UUID(OS_PARTITION),
            UUID(EFI_PARTITION),
            check_only=False,
        )
    manifest["paths"] = {
        "recovery.cmd": sha256((bundle / "recovery.cmd").read_bytes()).hexdigest()
    }
    plan_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="prepared job"):
        write_startup(
            bundle,
            output,
            target,
            UUID(OS_PARTITION),
            UUID(EFI_PARTITION),
            check_only=False,
        )


@pytest.mark.skipif(os.name != "nt", reason="Runs Windows PowerShell")
@pytest.mark.parametrize(
    "failure", ["", "duplicate", "digest", "apply", "apply-failure"]
)
def test_winpe_startup_checks_before_running_with_changed_letters(
    tmp_path: Path, failure: str
) -> None:
    volume = tmp_path / "windows-volume"
    bundle = volume / "WinOfflineJobs~" / "my bundle"
    job = volume / "ProgramData" / "WinOfflineUpdate" / "jobs" / JOB
    bundle.mkdir(parents=True)
    job.mkdir(parents=True)
    (job / "target.id").write_text(f"{JOB}\n", encoding="ascii")
    trace = tmp_path / "trace.txt"
    runner = bundle / "recovery.cmd"
    runner.write_text(
        '@echo %*>>"%WUO_TEST_TRACE%"\n'
        '@if /I "%~1"=="apply" if "%WUO_TEST_FAILURE%"=="apply-failure" exit /b 5\n'
        "@exit /b 0\n",
        encoding="ascii",
    )
    plan = bundle / "plan.json"
    plan.write_text("{}", encoding="ascii")
    (tmp_path / "wuo-job.json").write_text(
        json.dumps(
            {
                "job": JOB,
                "partitions": {"windows": OS_PARTITION, "efi": EFI_PARTITION},
                "bundle": r"WinOfflineJobs~\my bundle",
                "mode": "apply" if failure.startswith("apply") else "check",
                "files": {
                    "plan.json": sha256(plan.read_bytes()).hexdigest(),
                    "recovery.cmd": sha256(runner.read_bytes()).hexdigest(),
                },
            }
        ),
        encoding="ascii",
    )
    if failure == "digest":
        runner.write_text('@echo unexpected>>"%WUO_TEST_TRACE%"\n', encoding="ascii")

    script = files("win_offline_update").joinpath("winpe_startup.ps1").read_text()
    guard = next(line for line in script.splitlines() if "CurrentControlSet" in line)
    # Use process-local PS drives and simulated storage cmdlets. No disk is remapped.
    script = script.replace(guard, "    if ($false) {")
    reboot = next(line for line in script.splitlines() if "wpeutil.exe" in line)
    script = script.replace(reboot, "    & $env:WUO_TEST_REBOOT")
    reboot_stub = tmp_path / "reboot-stub.cmd"
    reboot_stub.write_text(
        '@echo reboot>>"%WUO_TEST_TRACE%"\n@exit /b 0\n', encoding="ascii"
    )
    prefix = f"""
$drives = @(Get-PSDrive -PSProvider FileSystem | Select-Object -ExpandProperty Name)
$letters = @('R','T','U','V','W','Y','Z') | Where-Object {{ $_ -notin $drives }}
if ($letters.Count -lt 2) {{ throw 'The test needs two unused PowerShell drive names.' }}
New-PSDrive -Name $letters[0] -PSProvider FileSystem -Root $env:WUO_TEST_VOLUME | Out-Null
$partitions = @(
    [pscustomobject]@{{ Guid='{OS_PARTITION}'; DriveLetter=$letters[0] }}
    [pscustomobject]@{{ Guid='{EFI_PARTITION}'; DriveLetter=$letters[1]; GptType='c12a7328-f81f-11d2-ba4b-00a0c93ec93b' }}
)
if ($env:WUO_TEST_FAILURE -eq 'duplicate') {{ $partitions += $partitions[0] }}
function Get-Partition {{ $partitions }}
Write-Host ('Test letters: ' + $letters[0] + ' ' + $letters[1])
"""
    launch = tmp_path / "wuo-startup.ps1"
    launch.write_text(prefix + script, encoding="ascii")
    result = subprocess.run(
        [
            Path(os.environ["SYSTEMROOT"])
            / "System32/WindowsPowerShell/v1.0/powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            launch,
        ],
        env={
            **os.environ,
            "WUO_TEST_VOLUME": str(volume),
            "WUO_TEST_TRACE": str(trace),
            "WUO_TEST_FAILURE": failure,
            "WUO_TEST_REBOOT": str(reboot_stub),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if failure in {"duplicate", "digest"}:
        assert result.returncode == 1, result.stdout + result.stderr
        assert (
            "found 2" if failure == "duplicate" else "Bundle digest mismatch"
        ) in result.stdout
        assert not trace.exists()
    else:
        target, efi = result.stdout.splitlines()[0].split(": ")[1].split()
        expected = [f"check {target}: {efi}:"]
        if failure.startswith("apply"):
            expected.append(f"apply {target}: {efi}:")
        if failure == "apply":
            expected.append("reboot")
        assert trace.read_text().splitlines() == expected
        assert result.returncode == (1 if failure == "apply-failure" else 0), (
            result.stdout + result.stderr
        )


def test_iso_builder_uses_adk_paths_and_discards_on_component_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from win_offline_update import winpe

    kits = tmp_path / "Program Files"
    adk = kits / "Windows Kits/10/Assessment and Deployment Kit"
    pe = adk / "Windows Preinstallation Environment"
    deployment = adk / "Deployment Tools"
    prereqs = [
        deployment / "DandISetEnv.bat",
        deployment / "amd64/DISM/dism.exe",
        pe / "copype.cmd",
        pe / "MakeWinPEMedia.cmd",
        pe / "amd64/en-us/winpe.wim",
    ]
    for component in winpe.COMPONENTS:
        prereqs.extend(
            [
                pe / "amd64/WinPE_OCs" / f"{component}.cab",
                pe / "amd64/WinPE_OCs/en-us" / f"{component}_en-us.cab",
            ]
        )
    for path in prereqs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    target = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", MACHINE, LOADER
    )
    monkeypatch.setenv("PROGRAMFILES(X86)", str(kits))
    monkeypatch.setattr(winpe, "read_target", lambda: target)
    monkeypatch.setattr(
        winpe, "_partition_guids", lambda _: (UUID(OS_PARTITION), UUID(EFI_PARTITION))
    )
    monkeypatch.setattr(winpe, "write_startup", lambda *_, **__: None)
    commands: list[tuple[str | Path, ...]] = []

    def run(
        command: list[str | Path],
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> str:
        commands.append(tuple(command))
        if Path(command[0]).name == "cmd.exe":
            assert cwd and cwd.parent == tmp_path
            assert env and Path(env["WUO_ADK_COPYPE"]) == pe / "copype.cmd"
            assert Path(env["WUO_ADK_MAKEMEDIA"]) == pe / "MakeWinPEMedia.cmd"
            assert 'call "%WUO_ADK_COPYPE%"' in Path(command[-1]).read_text()
        if "/Add-Package" in command:
            raise ValueError("mock component error")
        return ""

    monkeypatch.setattr(winpe, "_run", run)
    output = tmp_path / "job.iso"
    with pytest.raises(ValueError, match="mock component error"):
        winpe.build_iso(tmp_path / "bundle", output, check_only=True, bootex=False)
    assert "/Mount-Image" in commands[1]
    assert commands[2][-1] == f"/PackagePath:{prereqs[5]}"
    assert "/Discard" in commands[-1]
    assert not output.exists()
