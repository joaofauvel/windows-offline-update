import json
from pathlib import Path

import pytest

from wuo import cli
from wuo.downloads import package_hashes
from wuo.target import TargetIdentity

JOB = "a1bad4cd-52c8-4e0b-bc75-dc24c29e4155"
OWNER = "9e1379fb-31bc-4c97-b14a-52aed0371c44"
LOADER = "32df251e-0f11-4e78-8559-23308eb4e23c"


@pytest.fixture(autouse=True)
def no_host_restart_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wuo.windows_updates.current_reboot_required", lambda _: False)


def test_main_help_shows_the_routine_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--help"]) == 0
    help_text = capsys.readouterr().out
    for command in ("setup", "update", "status", "search", "download"):
        assert command in help_text
    assert "prepare-drivers" not in help_text
    assert cli.main(["prepare-drivers", "--help"]) == 0


@pytest.mark.parametrize("corrupt", [False, True])
def test_only_a_complete_verified_bundle_is_published(
    tmp_path: Path, corrupt: bool
) -> None:
    from wuo.winre_menu import MENU_PATH, Menu

    identity = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", OWNER, LOADER
    )
    root = tmp_path / MENU_PATH
    root.mkdir(parents=True)
    (root / "installed.id").write_text(f"{OWNER}\n", encoding="ascii")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    runner = bundle / "recovery.cmd"
    runner.write_bytes(b"@echo off\r\n")
    inventory = {"recovery.cmd": package_hashes(runner)[0]}
    plan = bundle / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "id": JOB,
                "target": {
                    "machine_guid": OWNER,
                    "loader_guid": LOADER,
                    "release": "25H2",
                    "build": 26200,
                    "revision": 9974,
                    "architecture": "x64",
                },
                "paths": inventory,
            }
        ),
        encoding="ascii",
    )
    state = root.parent / "jobs" / JOB
    state.mkdir(parents=True)
    (state / "target.id").write_text(f"{JOB}\n", encoding="ascii")
    (state / "plan.sha256").write_text(package_hashes(plan)[0] + "\n", encoding="ascii")

    menu = Menu(identity)
    if corrupt:
        runner.write_bytes(b"wrong")
        with pytest.raises(ValueError, match="digest mismatch"):
            menu.publish(bundle)
        assert not (root / "pending.id").exists()
    else:
        destination = menu.publish(bundle)
        assert (root / "pending.id").read_text(encoding="ascii") == f"{JOB}\n"
        assert (destination / "hashes.txt").read_text(
            encoding="ascii"
        ).splitlines() == [
            f"{package_hashes(plan)[0]} plan.json",
            f"{inventory['recovery.cmd']} recovery.cmd",
        ]
        assert (state / "hashes.sha256").read_text(
            encoding="ascii"
        ).strip() == package_hashes(destination / "hashes.txt")[0]
        with pytest.raises(ValueError, match="already staged"):
            menu.publish(bundle)
        menu.discard()
        assert not (root / "pending.id").exists()
        assert destination.is_dir()


def test_status_leaves_an_interrupted_job_pending(tmp_path: Path) -> None:
    from wuo.winre_menu import MENU_PATH, Menu

    identity = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", OWNER, LOADER
    )
    root = tmp_path / MENU_PATH
    root.mkdir(parents=True)
    (root / "installed.id").write_text(f"{OWNER}\n", encoding="ascii")
    pending = root / "pending.id"
    pending.write_text(f"{JOB}\n", encoding="ascii")
    menu = Menu(identity)
    state = menu.job_state(JOB)
    (state / "apply.started").mkdir(parents=True)

    with pytest.raises(ValueError, match="servicing was interrupted"):
        menu.status()
    assert pending.read_text(encoding="ascii") == f"{JOB}\n"


def test_setup_can_replace_or_disable_saved_hooks_without_reinstalling_winre(
    tmp_path: Path,
) -> None:
    from wuo.hook_selection import read_selection
    from wuo.winre_menu import MENU_PATH, Menu

    identity = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", OWNER, LOADER
    )
    root = tmp_path / MENU_PATH
    root.mkdir(parents=True)
    (root / "installed.id").write_text(f"{OWNER}\n", encoding="ascii")
    source = tmp_path / "usb4.toml"
    source.write_text(
        "[[hooks]]\nphase = 'post'\nkey = 'Services\\UASPStor'\nname = 'Start'\nvalue = 0\n",
        encoding="ascii",
    )
    menu = Menu(identity)
    menu.configure("install", source)
    saved = menu.profile
    assert saved is not None and saved != source
    original = read_selection(saved)
    assert original.origins[0].location == str(source.resolve())
    source.write_text(source.read_text().replace("value = 0", "value = 1"))
    assert read_selection(menu.profile).hooks == original.hooks
    menu.configure("install", source)
    assert read_selection(menu.profile).hooks[0].value == 1
    assert read_selection(saved).hooks[0].value == 0
    menu.configure("install")
    assert menu.profile is None


def test_setup_displays_pending_hooks_when_saved_selection_is_disabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from wuo.winre_menu import MENU_PATH, Menu

    identity = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", OWNER, LOADER
    )
    root = tmp_path / MENU_PATH
    root.mkdir(parents=True)
    (root / "installed.id").write_text(f"{OWNER}\n", encoding="ascii")
    (root / "pending.id").write_text(f"{JOB}\n", encoding="ascii")
    pending = root / "bundles" / JOB
    pending.mkdir(parents=True)
    (pending / "hooks.toml").write_text(
        "[[hooks]]\nphase = 'post'\nkey = 'Services\\UASPStor'\nname = 'Start'\nvalue = 0\n",
        encoding="ascii",
    )
    (pending / "plan.json").write_text('{"hooks": [{}]}', encoding="ascii")
    menu = Menu(identity)
    menu.configure("install")
    menu.describe_hooks()
    output = capsys.readouterr().out
    assert "Hooks: none." in output
    assert f"Pending job {JOB}" in output
    assert "UASPStor" in output


def test_pending_job_cannot_restart_with_conflicting_hook_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wuo.winre_menu import MENU_PATH, Menu

    identity = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", OWNER, LOADER
    )
    root = tmp_path / MENU_PATH
    root.mkdir(parents=True)
    (root / "installed.id").write_text(f"{OWNER}\n", encoding="ascii")
    (root / "pending.id").write_text(f"{JOB}\n", encoding="ascii")
    menu = Menu(identity)
    called = []
    monkeypatch.setattr(menu, "offer_restart", lambda: called.append(True))
    with pytest.raises(ValueError, match="pending job has captured hooks"):
        menu.resume(allow_restart=False)
    assert called == []
    assert (root / "pending.id").is_file()


def test_stale_menu_cannot_publish_across_new_native_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wuo import windows_updates
    from wuo.winre_menu import MENU_PATH, Menu

    identity = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", OWNER, LOADER
    )
    root = tmp_path / MENU_PATH
    root.mkdir(parents=True)
    (root / "installed.id").write_text(OWNER, encoding="ascii")
    state = {"incomplete": False}

    def verify(*_: object) -> windows_updates.WindowsResult | None:
        if state["incomplete"]:
            return windows_updates.WindowsResult(
                False, False, (), root / "windows-updates.json"
            )
        return None

    monkeypatch.setattr(windows_updates, "verify", verify)
    stale = Menu(identity)
    assert stale.resume()
    state["incomplete"] = True  # Another scanner finished its native phase later.
    assert stale.windows_incomplete is False
    with pytest.raises(ValueError, match="Windows.*incomplete|Windows phase"):
        stale.publish(tmp_path / "not-yet-read")
    assert not (root / "pending.id").exists()


def test_restart_rechecks_native_phase_after_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wuo import windows_updates, winre_menu

    identity = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", OWNER, LOADER
    )
    menu = winre_menu.Menu(identity)
    menu.root.mkdir(parents=True)
    (menu.root / "installed.id").write_text(OWNER, encoding="ascii")
    (menu.root / "pending.id").write_text(JOB, encoding="ascii")
    state = {"incomplete": False}

    def verify(*_: object) -> windows_updates.WindowsResult | None:
        return (
            windows_updates.WindowsResult(
                False, True, (), menu.root / "windows-updates.json"
            )
            if state["incomplete"]
            else None
        )

    def consent(_: str) -> str:
        state["incomplete"] = True
        return "yes"

    monkeypatch.setattr(windows_updates, "verify", verify)
    monkeypatch.setattr(winre_menu.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", consent)
    monkeypatch.setattr(
        winre_menu, "_run", lambda *_: pytest.fail("The stale restart must not run.")
    )
    with pytest.raises(ValueError, match="Windows.*incomplete|Windows phase"):
        menu.offer_restart()


def test_incomplete_native_phase_does_not_offer_existing_recovery_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wuo import windows_updates
    from wuo.winre_menu import Menu

    identity = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", OWNER, LOADER
    )
    menu = Menu(identity)
    menu.root.mkdir(parents=True)
    (menu.root / "installed.id").write_text(OWNER, encoding="ascii")
    (menu.root / "pending.id").write_text(JOB, encoding="ascii")
    monkeypatch.setattr(
        windows_updates,
        "verify",
        lambda *_: windows_updates.WindowsResult(
            False, False, (), menu.root / "windows-updates.json"
        ),
    )
    monkeypatch.setattr(
        menu,
        "offer_restart",
        lambda: pytest.fail("Resolve the native phase before a recovery restart."),
    )
    assert menu.resume() is False
    assert menu.windows_incomplete


def test_pending_native_hooks_remain_visible_after_future_selection_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from wuo import windows_hooks, windows_updates
    from wuo.hook_selection import resolve_selection
    from wuo.winre_menu import Menu

    identity = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", OWNER, LOADER
    )
    menu = Menu(identity)
    menu.root.mkdir(parents=True)
    (menu.root / "installed.id").write_text(OWNER, encoding="ascii")
    (menu.root / "selection.json").write_text('{"profile": null}', encoding="ascii")
    selected = resolve_selection(("official/usb4",), None, tmp_path / "selected")
    monkeypatch.setattr(windows_hooks, "_backup", lambda *_: None)
    monkeypatch.setattr(windows_hooks, "apply", lambda *_: None)
    job_id = "55555555-5555-5555-5555-555555555555"
    with windows_hooks.capture(
        selected.path, menu.root, OWNER, identity.windows, job_id=job_id
    ):
        pass
    (menu.root / windows_updates.JOURNAL).write_text(
        json.dumps({"hooks_job_id": job_id, "phase_complete": False}), encoding="ascii"
    )
    menu.describe_hooks()
    output = capsys.readouterr().out
    assert "Saved hooks for future jobs" in output and "Hooks: none." in output
    assert "Pending Windows phase uses these captured hooks" in output
    assert "official/usb4" in output and "UASPStor" in output


def test_unfinished_native_phase_rejects_new_hook_flags_before_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wuo import windows_updates
    from wuo.winre_menu import Menu

    identity = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", OWNER, LOADER
    )
    menu = Menu(identity)
    menu.root.mkdir(parents=True)
    (menu.root / "installed.id").write_text(OWNER, encoding="ascii")
    monkeypatch.setattr(
        windows_updates,
        "verify",
        lambda *_: windows_updates.WindowsResult(
            False, False, (), menu.root / windows_updates.JOURNAL
        ),
    )
    with pytest.raises(ValueError, match="captured hooks"):
        menu.resume(allow_restart=False)
