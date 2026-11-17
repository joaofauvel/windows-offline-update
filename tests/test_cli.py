import importlib

import pytest


@pytest.mark.parametrize(("arguments", "exit_code"), [(["--help"], 0), (["bad"], 2)])
def test_cli_exit_code(arguments: list[str], exit_code: int) -> None:
    cli = importlib.import_module("win_offline_update.cli")
    assert cli.main(arguments) == exit_code
