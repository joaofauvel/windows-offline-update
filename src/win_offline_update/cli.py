"""Command-line entry point."""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

lazy import httpx

from .models import DownloadManifest, Update
lazy from . import bundle, catalog, downloads, feeds, target
lazy from .update import run_update


def _write_updates(updates: list[Update], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps([asdict(update) for update in updates], indent=2))
        return
    if not updates:
        print("No matches on the first Microsoft Catalog results page.")
    for update in updates:
        print(f"{update.id}  {update.title}")
        details = [
            update.product,
            update.architecture or "",
            update.classification,
            update.date,
        ]
        print(f"  {' | '.join(filter(None, details))} | {update.source}")
        if update.files:
            print(f"  {update.route}")
            print(f"  Details: {update.details_url}")
            for file in update.files:
                print(f"  {file.name}\n    {file.url}")


async def _resolve(client: httpx.AsyncClient, update_id: str) -> Update:
    if update_id.startswith("defender-"):
        return feeds.defender(update_id.removeprefix("defender-"))
    return await catalog.show(client, update_id)


async def _run(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        if args.command == "update":
            identity = await asyncio.to_thread(target.read_target, read_loader=False)
            return await run_update(
                client,
                identity,
                args.output or Path.home() / "Downloads/WindowsOfflineUpdates",
                only=tuple(args.only),
                dry_run=args.dry_run,
                driver_cache_fallback=args.driver_cache,
                profile=args.profile,
                extractor=args.extractor,
            )
        if args.command == "search":
            updates = await catalog.search(client, args.query)
            updates = [
                item
                for item in updates
                if (args.arch is None or args.arch == item.architecture)
                and (
                    args.product is None
                    or args.product.casefold() in item.product.casefold()
                )
                and (
                    args.kind is None
                    or args.kind.casefold() in item.classification.casefold()
                )
            ]
        elif args.command == "latest":
            if args.defender:
                updates = [feeds.defender(args.arch)]
            elif args.release:
                updates = [await feeds.latest(client, args.release.upper(), args.arch)]
            else:
                raise ValueError(
                    "Supply --release for a cumulative update or use --defender."
                )
        elif args.command == "show":
            updates = [await _resolve(client, args.update_id)]
        elif args.command == "prepare":
            update_id = args.update_id or await asyncio.to_thread(
                bundle.read_update_id, args.download
            )
            selected = await _resolve(client, update_id)
            identity = await asyncio.to_thread(target.read_target)
            plan = await asyncio.to_thread(
                bundle.prepare,
                selected,
                identity,
                args.download,
                args.output,
                profile=args.profile,
                extractor=args.extractor,
            )
            print(
                f"Prepared {selected.kb} for Windows {identity.release} ({identity.build}.{identity.revision})."
            )
            print(f"Package order: {' then '.join(plan.commands)}")
            print(f"Configured registry hooks: {len(plan.hooks)}")
            return 0
        else:
            selected = await _resolve(client, args.update_id)
            print(
                f"Downloading {len(selected.files)} package(s) for {selected.title}",
                file=sys.stderr,
            )
            result: DownloadManifest = await downloads.download(
                client, selected, args.output, concurrency=args.jobs
            )
            if args.json:
                print(json.dumps(asdict(result), indent=2))
            else:
                print(
                    f"Saved {len(result.files)} package(s) and manifest.json to {args.output.resolve()}"
                )
            return 0
    _write_updates(updates, json_output=args.json)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wuo",
        description="Find Windows updates and prepare them for offline servicing.",
    )
    commands = parser.add_subparsers(dest="command")
    workflow = commands.add_parser(
        "update",
        help="Find applicable updates, download them and prepare supported WinRE bundles.",
    )
    workflow.add_argument(
        "--only",
        action="append",
        default=[],
        choices=(
            "monthly",
            "features",
            "dotnet",
            "drivers",
            "defender",
            "security-platform",
            "other",
        ),
        help="Limit the run to this category. Repeat to select multiple categories.",
    )
    workflow.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve offers without downloading packages or writing a bundle.",
    )
    workflow.add_argument(
        "--driver-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use Windows Update Agent's download cache as a fallback for unavailable Catalog drivers (default: enabled).",
    )
    workflow.add_argument(
        "--output",
        type=Path,
        help="Destination (default: ~/Downloads/WindowsOfflineUpdates).",
    )
    workflow.add_argument(
        "--profile",
        type=Path,
        help="Optional local registry-hook TOML for recovery bundles.",
    )
    workflow.add_argument(
        "--extractor", default="7z.exe", help="Path to the 7-Zip CLI."
    )
    search = commands.add_parser(
        "search", help="Find published Microsoft Catalog updates."
    )
    search.add_argument("query", help="KB number, title, or quoted hardware ID.")
    search.add_argument("--arch", choices=("x64", "arm64", "x86"))
    search.add_argument("--product")
    search.add_argument(
        "--kind", help="Match a Catalog classification such as Drivers."
    )
    show = commands.add_parser("show", help="Show details and every standalone file.")
    show.add_argument("update_id")
    download = commands.add_parser(
        "download", help="Download all files for a selected update."
    )
    download.add_argument("update_id")
    download.add_argument("--output", type=Path, required=True)
    download.add_argument("--jobs", type=int, default=3)
    latest = commands.add_parser(
        "latest", help="Find a cumulative or Defender intelligence update."
    )
    latest.add_argument("--release", help="Windows release, for example 25H2.")
    latest.add_argument("--arch", choices=("x64", "arm64", "x86"), default="x64")
    latest.add_argument("--defender", action="store_true")
    commands.add_parser(
        "media", help="Show Microsoft's full Windows Setup media download page."
    )
    prepare = commands.add_parser(
        "prepare", help="Prepare a verified offline servicing bundle."
    )
    prepare.add_argument(
        "--download", type=Path, required=True, help="Folder containing package files."
    )
    prepare.add_argument(
        "--update-id", help="Microsoft Catalog ID when importing existing packages."
    )
    prepare.add_argument(
        "--output", type=Path, required=True, help="New recovery bundle directory."
    )
    prepare.add_argument(
        "--profile", type=Path, help="Optional local registry-hook TOML."
    )
    prepare.add_argument("--extractor", default="7z.exe", help="Path to the 7-Zip CLI.")
    verify = commands.add_parser(
        "verify", help="Verify recovery bundle files and optional post-boot state."
    )
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--post-boot", action="store_true")
    for command in (search, show, download, latest):
        command.add_argument(
            "--json", action="store_true", help="Print machine-readable results."
        )
    try:
        args = parser.parse_args(argv)
    except SystemExit as result:
        return int(result.code) if isinstance(result.code, int) else 1
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "media":
        print(feeds.WINDOWS_MEDIA)
        return 0
    try:
        if args.command == "verify":
            bundle.verify(args.bundle, post_boot=args.post_boot)
            print("Verification passed.")
            return 0
        return asyncio.run(_run(args))
    except (ValueError, OSError, httpx.HTTPError) as error:
        print(f"win-offline-update: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Download or lookup canceled.", file=sys.stderr)
        return 130
