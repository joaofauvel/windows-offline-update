import json
from pathlib import Path

import pytest

from win_offline_update import windows_hooks
from win_offline_update.hooks import Hook


def test_expected_values_are_checked_before_any_registry_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {("A", "Start"): 3, ("B", "Start"): 2}
    writes: list[tuple[str, str, int]] = []
    monkeypatch.setattr(windows_hooks, "_read", lambda key, name: values[key, name])
    monkeypatch.setattr(windows_hooks, "_write", lambda *args: writes.append(args))
    monkeypatch.setattr(windows_hooks, "_path", lambda hook: hook.key)
    hooks = (Hook("post", "A", "Start", 0, 3), Hook("post", "B", "Start", 0, 3))
    with pytest.raises(ValueError, match="expected"):
        windows_hooks.apply(hooks, "post")
    assert not writes


def test_hooks_require_successful_write_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_hooks, "_read", lambda *_: 3)
    monkeypatch.setattr(windows_hooks, "_write", lambda *_: None)
    monkeypatch.setattr(windows_hooks, "_path", lambda hook: hook.key)
    with pytest.raises(ValueError, match="readback"):
        windows_hooks.apply((Hook("post", "A", "Start", 0),), "post")


def test_post_hooks_run_when_native_installer_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "hooks.toml"
    profile.write_text(
        '[[hooks]]\nphase = "post"\nkey = "Services\\\\Test"\nname = "Start"\nvalue = 0\n'
    )
    phases: list[str] = []
    monkeypatch.setattr(windows_hooks, "_backup", lambda *_: None)
    monkeypatch.setattr(windows_hooks, "apply", lambda _, phase: phases.append(phase))
    with (
        pytest.raises(RuntimeError, match="installer"),
        windows_hooks.capture(profile, tmp_path, "machine", tmp_path),
    ):
        raise RuntimeError("installer")
    assert phases == ["pre", "post"]


def test_native_capture_preserves_validated_named_hook_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from win_offline_update.downloads import package_hashes
    from win_offline_update.hook_selection import read_selection, resolve_selection

    selected = resolve_selection(("official/usb4",), None, tmp_path / "selected")
    monkeypatch.setattr(windows_hooks, "_backup", lambda *_: None)
    monkeypatch.setattr(windows_hooks, "apply", lambda *_: None)
    job_id = "55555555-5555-5555-5555-555555555555"
    with windows_hooks.capture(
        selected.path, tmp_path, "machine", tmp_path, job_id=job_id
    ):
        pass
    captured = tmp_path / "windows-hook-jobs" / job_id
    assert (captured / "hooks.json").read_bytes() == (
        tmp_path / "selected/hooks.json"
    ).read_bytes()
    assert read_selection(captured / "hooks.toml").origins == selected.origins
    receipt = json.loads((captured / windows_hooks.RECEIPT).read_text())
    assert receipt["metadata_sha256"] == package_hashes(captured / "hooks.json")[0]
    assert (tmp_path / "windows-hooks.json").is_file()


def test_expected_hook_accepts_already_applied_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_hooks, "_read", lambda *_: 0)
    monkeypatch.setattr(windows_hooks, "_write", lambda *_: None)
    monkeypatch.setattr(windows_hooks, "_path", lambda hook: hook.key)
    windows_hooks.apply((Hook("post", "A", "Start", 0, 3),), "post")


def test_post_hooks_still_run_if_pre_write_or_phase_backup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "hooks.toml"
    profile.write_text(
        '[[hooks]]\nphase = "post"\nkey = "Test"\nname = "Start"\nvalue = 0\n'
    )
    phases: list[str] = []

    def backup(_: Path, path: Path) -> None:
        if path.name == "system-before-post.hiv":
            raise OSError("backup unavailable")

    def apply(_: object, phase: str) -> None:
        phases.append(phase)
        if phase == "pre":
            raise RuntimeError("pre write failed")

    monkeypatch.setattr(windows_hooks, "_backup", backup)
    monkeypatch.setattr(windows_hooks, "apply", apply)
    with (
        pytest.raises(ValueError, match="Post hooks were applied"),
        windows_hooks.capture(profile, tmp_path, "machine", tmp_path),
    ):
        pytest.fail("Installer must not run after a pre-hook failure.")
    assert phases == ["pre", "post"]
