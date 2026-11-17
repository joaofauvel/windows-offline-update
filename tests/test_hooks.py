from pathlib import Path

import pytest


def test_profile_accepts_five_opt_in_service_values() -> None:
    from win_offline_update.hooks import load_profile

    profile = load_profile(Path(__file__).parents[1] / "examples" / "usb4.toml")
    assert [(hook.phase, hook.key, hook.name, hook.value) for hook in profile] == [
        ("post", r"Services\UASPStor", "Start", 0),
        ("post", r"Services\USBHUB3", "Start", 0),
        ("post", r"Services\USBXHCI", "Start", 0),
        ("post", r"Services\Usb4DeviceRouter", "Start", 0),
        ("post", r"Services\Usb4HostRouter", "Start", 0),
    ]


def test_hook_cannot_inject_cmd(tmp_path: Path) -> None:
    from win_offline_update.hooks import load_profile

    profile = tmp_path / "bad.toml"
    profile.write_text(
        '[[hooks]]\nphase="post"\nkey="foo%PATH%"\nname="Start"\nvalue=0\n'
    )
    with pytest.raises(ValueError, match="registry key"):
        load_profile(profile)
