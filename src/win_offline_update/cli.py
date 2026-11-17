"""Command-line entry point."""

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="win-offline-update",
        description="Find Windows updates and prepare them for offline servicing.",
    )
    try:
        parser.parse_args(argv)
    except SystemExit as result:
        return int(result.code) if isinstance(result.code, int) else 1
    parser.print_help()
    return 0
