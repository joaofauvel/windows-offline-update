from pathlib import Path

import pytest

from wuo import cli


@pytest.mark.parametrize("command", ["setup", "update"])
def test_named_hooks_are_visible_in_help(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([command, "--help"]) == 0
    output = capsys.readouterr().out
    assert "--hook" in output and "--no-hooks" in output


def test_hooks_help_and_main_help_work_without_windows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["hooks", "--help"]) == 0
    assert "refresh" in capsys.readouterr().out
    assert cli.main(["--help"]) == 0
    assert "hooks" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["setup", "update"])
def test_hook_selection_options_are_mutually_exclusive(command: str) -> None:
    assert (
        cli.main([command, "--hook", "official/usb4", "--profile", "local.toml"]) == 2
    )
    assert cli.main([command, "--hook", "official/usb4", "--no-hooks"]) == 2


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("manual", [False, True])
def test_update_shows_effective_saved_or_disabled_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    override: bool,
    manual: bool,
) -> None:
    from wuo import target
    from wuo.hooks import load_profile

    saved = tmp_path / "saved.toml"
    saved.write_text(
        "[[hooks]]\nphase = 'post'\nkey = 'Services\\UASPStor'\nname = 'Start'\nvalue = 0\n",
        encoding="ascii",
    )
    identity = target.TargetIdentity(
        "25H2",
        26200,
        9974,
        "x64",
        tmp_path / "Windows",
        "c3cf373e-3eec-48d7-b090-3cf4992de65d",
        "69350cf4-a81c-42ab-96f2-df2d8e9a35b0",
    )

    class TestMenu:
        installed = True
        profile = saved
        root = tmp_path

        def __init__(self, _: object) -> None:
            pass

        def resume(self, **_: object) -> bool:
            return True

    observed = []

    async def stage(*args: object) -> int:
        profile = args[-1]
        observed.append(load_profile(profile) if isinstance(profile, Path) else ())
        return 0

    monkeypatch.setattr(cli, "Menu", TestMenu)
    monkeypatch.setattr(target, "read_target", lambda **_: identity)
    monkeypatch.setattr(cli, "_stage_update", stage)
    flags = [
        "update",
        *(["--manual"] if manual else []),
        *(["--no-hooks"] if override else []),
    ]
    assert cli.main(flags) == 0
    output = capsys.readouterr().out
    if override:
        assert observed == [()]
        assert "Hooks: none." in output
    else:
        assert observed[0][0].key == r"Services\UASPStor"
        assert "UASPStor" in output and "post" in output
