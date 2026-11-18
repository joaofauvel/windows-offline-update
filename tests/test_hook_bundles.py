from hashlib import sha256
from pathlib import Path

import pytest

from win_offline_update import bundle
from win_offline_update.hook_registry import HookRegistry
from win_offline_update.hook_selection import read_selection, resolve_selection
from win_offline_update.target import TargetIdentity


def test_bundle_binds_provenance_and_displays_effective_hooks(tmp_path: Path) -> None:
    selection = resolve_selection(
        ["official/usb4"],
        None,
        tmp_path / "selection",
        HookRegistry(tmp_path / "registry"),
    )
    target = TargetIdentity(
        "25H2",
        26200,
        9974,
        "x64",
        tmp_path / "Windows",
        "2ee061c7-f848-4fe4-b5aa-b5e4b1967962",
        "8fd961b5-bced-4d4b-a3ca-5a372c957189",
    )
    staging = tmp_path / "stage"
    package = staging / "packages" / "driver.inf"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"driver")
    relative = "packages/driver.inf"
    plan = bundle.BundlePlan(
        "8895d5cf-d10b-4162-8fc6-6e858c881358",
        None,
        target,
        target.revision,
        (relative,),
        selection.hooks,
        {relative: sha256(package.read_bytes()).hexdigest()},
        {relative: package.stat().st_size},
        kind="drivers",
    )
    output = tmp_path / "bundle"
    bundle._publish_bundle(staging, output, plan, selection.path)
    bundle.verify(output)
    assert read_selection(output / "hooks.toml").origins == selection.origins
    summary = (output / "hooks-summary.txt").read_text(encoding="ascii")
    assert "official/usb4" in summary and "post:" in summary and "USBHUB3" in summary
    runner = (output / "recovery.cmd").read_text(encoding="ascii")
    assert 'type "%BUNDLE%hooks-summary.txt"' in runner
    (output / "hooks.json").write_text("{}", encoding="ascii")
    with pytest.raises(ValueError, match="digest mismatch"):
        bundle.verify(output)
