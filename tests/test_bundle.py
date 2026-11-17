from dataclasses import replace
from pathlib import Path

import pytest

from win_offline_update.catalog import parse_files, parse_search
from win_offline_update.models import Update

FIXTURES = Path(__file__).with_name("fixtures")


@pytest.fixture
def november_update() -> Update:
    selected = parse_search((FIXTURES / "catalog-search.html").read_text())[1]
    return replace(
        selected, files=parse_files((FIXTURES / "catalog-download.html").read_text())
    )


def test_kb5132061_orders_ssu_before_target(november_update: Update) -> None:
    from win_offline_update.bundle import resolve_plan
    from win_offline_update.target import TargetIdentity

    target = TargetIdentity(
        "25H2",
        26200,
        9457,
        "x64",
        Path("C:/Windows"),
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    )
    plan = resolve_plan(november_update, target)
    assert plan.commands == (
        "SSU-26100.9971-x64.cab",
        "packages/windows11.0-kb5132061-x64_aedc878e9e078df2ec7e2cfb98dd5b21831cdbcc.msu",
    )
    assert plan.target_revision == 9974


def test_missing_checkpoint_blocks_apply_plan(november_update: Update) -> None:
    from win_offline_update.bundle import resolve_plan
    from win_offline_update.target import TargetIdentity

    target = TargetIdentity(
        "25H2",
        26200,
        9457,
        "x64",
        Path("C:/Windows"),
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    )
    with pytest.raises(ValueError, match="checkpoint"):
        resolve_plan(replace(november_update, files=november_update.files[1:]), target)


def test_unknown_package_stays_manual(november_update: Update) -> None:
    from win_offline_update.bundle import resolve_plan
    from win_offline_update.target import TargetIdentity

    target = TargetIdentity(
        "25H2",
        26200,
        9457,
        "x64",
        Path("C:/Windows"),
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    )
    with pytest.raises(ValueError, match="manual servicing"):
        resolve_plan(
            replace(november_update, id="unknown", classification="Drivers"), target
        )
