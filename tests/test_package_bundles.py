import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from win_offline_update import bundle
from win_offline_update.models import Update, UpdateFile
from win_offline_update.recovery import render_runner
from win_offline_update.target import TargetIdentity

UPDATE_ID = "74d220af-2680-4c76-8b90-b415b8f35b0d"
MACHINE = "11111111-1111-1111-1111-111111111111"
LOADER = "22222222-2222-2222-2222-222222222222"
IDENTITY = "Package_for_DotNetRollup~31bf3856ad364e35~amd64~~10.0.12345.2"
OLDER_IDENTITY = IDENTITY.removesuffix("2") + "1"
PAYLOAD = b"verified Microsoft payload"
MUM = (
    b'<assembly xmlns="urn:schemas-microsoft-com:asm.v3" manifestVersion="1.0">'
    b'<assemblyIdentity name="Package_for_DotNetRollup" version="10.0.12345.2" '
    b'processorArchitecture="amd64" language="neutral" publicKeyToken="31bf3856ad364e35"/>'
    b'<package identifier="Test" releaseType="Update"/></assembly>'
)


def target(tmp_path: Path) -> TargetIdentity:
    return TargetIdentity(
        "future-release", 31000, 12, "x64", tmp_path / "Windows", MACHINE, LOADER
    )


def update(*names: str) -> Update:
    return Update(
        UPDATE_ID,
        "Windows or .NET update",
        architecture="x64",
        files=tuple(
            UpdateFile(
                name,
                f"https://download.windowsupdate.com/{name}",
                hashlib.sha256(PAYLOAD).hexdigest(),
            )
            for name in names
        ),
    )


def test_generic_eligibility_does_not_require_a_kb_or_build_table(
    tmp_path: Path,
) -> None:
    plan = bundle.resolve_plan(update("framework.msu"), target(tmp_path))
    assert plan.kind == "packages"
    assert plan.files[0].name == "framework.msu"


@pytest.mark.parametrize(
    "change",
    ["unverified", "unsafe", "duplicate", "installer", "architecture", "host"],
)
def test_generic_eligibility_rejects_unsupported_or_unverified_payloads(
    tmp_path: Path, change: str
) -> None:
    selected = update("framework.msu")
    file = selected.files[0]
    if change == "unverified":
        selected = replace(selected, files=(replace(file, sha256=None),))
    elif change == "unsafe":
        selected = replace(selected, files=(replace(file, name="../framework.msu"),))
    elif change == "duplicate":
        selected = replace(selected, files=(file, replace(file, name="FRAMEWORK.MSU")))
    elif change == "installer":
        selected = update("setup.exe")
    elif change == "architecture":
        selected = replace(selected, architecture="arm64")
    else:
        selected = replace(selected, files=(replace(file, url="https://other.test/a"),))
    with pytest.raises(ValueError):
        bundle.resolve_plan(selected, target(tmp_path))


@pytest.fixture
def native_tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    settings: dict[str, object] = {
        "identity": IDENTITY,
        "state": "Staged",
        "applicable": "Yes",
        "members": ["framework.cab", "WSUSSCAN.cab", "update.xml"],
        "commands": [],
    }
    monkeypatch.setattr(shutil, "which", lambda _: "7z.exe")

    def run(command: list[str | Path], **_: object) -> subprocess.CompletedProcess[str]:
        args = [str(arg) for arg in command]
        commands = settings["commands"]
        assert isinstance(commands, list)
        commands.append(args)
        assert not any(arg in {"/Add-Package", "/Add-Driver"} for arg in args)
        if args[0] == "7z.exe":
            members = settings["members"]
            assert isinstance(members, list)
            if args[1] == "l":
                archive = Path(args[-1])
                if archive.suffix.casefold() != ".msu":
                    members = ["update.mum", "update.cat"]
                    if archive.name in {"DesktopDeployment.cab", "metadata.cab"}:
                        members = ["other.bin"]
                    if settings.get("missing_main") and archive.suffix == ".wim":
                        members = ["component.mum"]
                output = "\n\n".join(f"Path = {name}\nSize = 20" for name in members)
                return subprocess.CompletedProcess(command, 0, output, "")
            destination = Path(next(arg[2:] for arg in args if arg.startswith("-o")))
            destination.mkdir(parents=True, exist_ok=True)
            # e extracts only the explicitly requested archive member.
            for name in args[3:]:
                if name in {"update.mum", "update.cat"}:
                    (destination / name).write_bytes(
                        MUM if name.endswith(".mum") else b"cat"
                    )
                elif name in members:
                    (destination / name).write_bytes(
                        b"checkpoint"
                        if "checkpoint" in Path(args[2]).name
                        else b"extracted payload"
                    )
            return subprocess.CompletedProcess(command, 0, "", "")
        identity = str(settings["identity"])
        package_path = next(
            (
                Path(arg.split(":", 1)[1])
                for arg in args
                if arg.startswith("/PackagePath:")
            ),
            None,
        )
        if (
            package_path is not None
            and package_path.is_file()
            and package_path.read_bytes() == b"checkpoint"
        ):
            identity = OLDER_IDENTITY
        text = f"Package Identity : {identity}\nState : {settings['state']}\n"
        if settings.get("applicable") is not None:
            text += f"Applicable : {settings['applicable']}\n"
        return subprocess.CompletedProcess(command, 0, text, "")

    monkeypatch.setattr(subprocess, "run", run)
    return settings


def prepare(tmp_path: Path, *names: str) -> bundle.BundlePlan:
    source = tmp_path / "source"
    source.mkdir()
    for name in names:
        (source / name).write_bytes(PAYLOAD)
    return bundle.prepare(
        update(*names), target(tmp_path), source, tmp_path / "recovery"
    )


def test_prepare_records_native_cbs_identity_and_preserves_original_msu(
    tmp_path: Path, native_tools: dict[str, object]
) -> None:
    result = prepare(tmp_path, "framework.msu")
    assert result.kind == "packages"
    assert result.package_identities == (IDENTITY,)
    assert (tmp_path / "recovery/packages/framework.msu").read_bytes() == PAYLOAD
    assert not list((tmp_path / "recovery/packages").glob("*.cab"))
    commands = native_tools["commands"]
    assert isinstance(commands, list)
    assert any("/Get-PackageInfo" in command for command in commands)
    recorded = json.loads((tmp_path / "recovery/plan.json").read_text())
    assert recorded["package_identities"] == [IDENTITY]
    assert recorded["kind"] == "packages"


def test_standalone_cab_uses_native_cbs_metadata_without_extractor(
    tmp_path: Path, native_tools: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    result = prepare(tmp_path, "enablement.cab")
    assert result.commands == ("packages/enablement.cab",)
    assert result.package_identities == (IDENTITY,)


@pytest.mark.parametrize(
    "identity",
    [
        "Package~31bf3856ad364e35~arm64~~10.0.12345.2",
        "Package~31bf3856ad364e35~amd64~~10.0.12345.2&echo bad",
        "not a canonical CBS identity",
    ],
)
def test_prepare_rejects_wrong_architecture_or_unsafe_native_metadata(
    tmp_path: Path, native_tools: dict[str, object], identity: str
) -> None:
    native_tools["identity"] = identity
    with pytest.raises(ValueError, match="identity|architecture"):
        prepare(tmp_path, "framework.msu")
    assert not (tmp_path / "recovery").exists()


def test_wim_backed_msu_checks_root_manifest_with_native_dism(
    tmp_path: Path, native_tools: dict[str, object]
) -> None:
    native_tools["members"] = ["DesktopDeployment.cab", "quality.wim", "metadata.cab"]
    result = prepare(tmp_path, "quality.msu")
    assert result.package_identities == (IDENTITY,)
    assert result.commands == ("packages/quality.msu",)
    commands = native_tools["commands"]
    assert isinstance(commands, list)
    assert any(
        any("/PackagePath:" in arg and "metadata" in arg for arg in command)
        for command in commands
    )


def test_known_kb_is_prepared_generically_without_a_supported_build_gate(
    tmp_path: Path, native_tools: dict[str, object]
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "quality.msu").write_bytes(PAYLOAD)
    selected = replace(
        update("quality.msu"),
        id="950b2422-28ad-4493-a96c-03725fd0d8c2",
        kb="KB5132061",
    )
    result = bundle.prepare(selected, target(tmp_path), source, tmp_path / "recovery")
    assert result.kind == "packages"
    assert result.package_identities == (IDENTITY,)


def test_wim_main_manifest_must_match_native_package_identity(
    tmp_path: Path, native_tools: dict[str, object]
) -> None:
    native_tools["members"] = ["quality.wim"]
    native_tools["identity"] = OLDER_IDENTITY
    with pytest.raises(ValueError, match="identity"):
        prepare(tmp_path, "quality.msu")
    assert not (tmp_path / "recovery").exists()


def test_wim_missing_main_manifest_does_not_verify_only_the_servicing_stack(
    tmp_path: Path, native_tools: dict[str, object]
) -> None:
    native_tools["members"] = ["SSU-31000-x64.cab", "quality.wim"]
    native_tools["missing_main"] = True
    with pytest.raises(ValueError, match="WIM|main.*package"):
        prepare(tmp_path, "quality.msu")
    assert not (tmp_path / "recovery").exists()


def test_checkpoint_chain_calls_target_msu_with_all_originals_beside_it(
    tmp_path: Path, native_tools: dict[str, object]
) -> None:
    result = prepare(tmp_path, "checkpoint.msu", "quality.msu")
    assert result.commands == ("packages/quality.msu",)
    assert set(result.package_identities) == {IDENTITY, OLDER_IDENTITY}
    assert {p.name for p in (tmp_path / "recovery/packages").iterdir()} == {
        "checkpoint.msu",
        "quality.msu",
    }


@pytest.mark.parametrize("state", ["unknown state", "Installed & echo unexpected"])
def test_package_metadata_rejects_unrecognized_native_state(
    tmp_path: Path, native_tools: dict[str, object], state: str
) -> None:
    native_tools["state"] = state
    with pytest.raises(ValueError, match="state|metadata"):
        prepare(tmp_path, "framework.cab")


@pytest.mark.parametrize("applicable", ["No", None, "Unknown"])
def test_external_package_requires_native_confirmation_of_target_applicability(
    tmp_path: Path, native_tools: dict[str, object], applicable: str | None
) -> None:
    native_tools["applicable"] = applicable
    with pytest.raises(ValueError, match="applicab"):
        prepare(tmp_path, "framework.cab")
    assert not (tmp_path / "recovery").exists()


@pytest.mark.skipif(os.name != "nt", reason="Runs only isolated Windows CMD stubs")
@pytest.mark.parametrize(
    ("state", "identity", "exit_code"),
    [
        ("Installed", IDENTITY, 0),
        ("Install Pending", IDENTITY, 0),
        ("Superseded", IDENTITY, 0),
        ("Staged", IDENTITY, 1),
        ("Installed", OLDER_IDENTITY, 1),
    ],
)
def test_cmd_package_state_parser_checks_exact_identity_and_completion(
    tmp_path: Path,
    native_tools: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    identity: str,
    exit_code: int,
) -> None:
    result = prepare(tmp_path, "framework.cab")
    runner = render_runner(result)
    routines = runner.split("\r\n:verify_package_states\r\n", 1)[1].split(
        "\r\n:open_hook_hive\r\n", 1
    )[0]
    # Exercise the real batch parser using a disposable .cmd executable stub.
    # No system DISM, registry, drive mapping, or image is touched.
    routines = routines.replace('"%SYS%\\dism.exe"', 'call "%SYS%\\dism.cmd"')
    (tmp_path / "dism.cmd").write_text(
        "@echo off\n"
        "echo Package Identity : %WUO_TEST_IDENTITY%\n"
        "echo State : %WUO_TEST_STATE%\n"
        "exit /b 0\n",
        encoding="ascii",
    )
    script = tmp_path / "check.cmd"
    script.write_text(
        "@echo off\nsetlocal EnableExtensions DisableDelayedExpansion\n"
        f'set "SYS={tmp_path}"\nset "JOB={tmp_path}"\n'
        f'set "LOG={tmp_path / "check.log"}"\n'
        "call :verify_package_states\nexit /b %errorlevel%\n"
        ":verify_package_states\n" + routines,
        encoding="ascii",
    )
    monkeypatch.undo()
    completed = subprocess.run(
        [
            str(Path(os.environ["SYSTEMROOT"]) / "System32/cmd.exe"),
            "/d",
            "/c",
            str(script),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "WUO_TEST_IDENTITY": identity, "WUO_TEST_STATE": state},
        check=False,
    )
    assert completed.returncode == exit_code, completed.stdout + completed.stderr
    assert identity in (tmp_path / "packages-after-0001.txt").read_text()


def test_generic_postboot_uses_cbs_state_after_enablement_changes_build(
    tmp_path: Path, native_tools: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(tmp_path, "enablement.cab")
    current = replace(target(tmp_path), build=32000, revision=1)
    monkeypatch.setattr(bundle, "read_target", lambda **_: current)
    manifest = json.loads((tmp_path / "recovery/plan.json").read_text())
    job = tmp_path / bundle.JOBS_PATH / manifest["id"]
    (job / "state.ready").write_text(manifest["id"])
    native_tools["state"] = "Installed"
    native_tools["applicable"] = None  # Identity queries do not require this field.
    bundle.verify(tmp_path / "recovery", post_boot=True)
    native_tools["state"] = "Install Pending"
    with pytest.raises(ValueError, match="Installed|pending|verified"):
        bundle.verify(tmp_path / "recovery", post_boot=True)


def test_generic_postboot_requires_successful_offline_marker(
    tmp_path: Path, native_tools: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(tmp_path, "enablement.cab")
    monkeypatch.setattr(bundle, "read_target", lambda **_: target(tmp_path))
    native_tools["state"] = "Installed"
    with pytest.raises(ValueError, match="marker"):
        bundle.verify(tmp_path / "recovery", post_boot=True)


def test_generic_runner_records_package_states_before_ready_and_repair(
    tmp_path: Path, native_tools: dict[str, object]
) -> None:
    result = prepare(tmp_path, "framework.msu")
    runner = render_runner(result)
    assert "@@" not in runner
    assert IDENTITY in runner
    apply = runner.split("\r\n:repair\r\n", 1)[0]
    assert apply.index("call :verify_package_states") < apply.index('state.ready" echo')
    repair = runner.split("\r\n:repair\r\n", 1)[1].split("\r\n:validate_target", 1)[0]
    assert repair.index("call :verify_package_states") < repair.index(
        "call :post_hooks"
    )
    assert "packages-after" in runner
    assert "/Get-PackageInfo" in runner
    assert "/IgnoreCheck" not in runner
