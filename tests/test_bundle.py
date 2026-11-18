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


def test_known_cumulative_uses_generic_package_eligibility(
    november_update: Update,
) -> None:
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
    assert plan.commands == tuple(
        f"packages/{file.name}" for file in november_update.files
    )
    assert plan.target_revision == target.revision
    assert plan.kind == "packages"


def test_eligibility_does_not_guess_checkpoint_dependencies(
    november_update: Update,
) -> None:
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
    plan = resolve_plan(
        replace(november_update, files=november_update.files[1:]), target
    )
    assert plan.kind == "packages"


def test_driver_package_requires_driver_servicing(november_update: Update) -> None:
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
    with pytest.raises(ValueError, match="driver servicing"):
        resolve_plan(
            replace(november_update, id="unknown", classification="Drivers"), target
        )
