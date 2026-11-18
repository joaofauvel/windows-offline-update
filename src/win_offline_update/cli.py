"""Command-line entry point."""

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path

lazy import httpx

from .models import DownloadManifest, Update
lazy from . import bundle, catalog, downloads, feeds, target
lazy from .update import PreparedUpdates, run_update
lazy from .winre_menu import Menu

_DOWNLOADS = Path("Downloads/WindowsOfflineUpdates")


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


async def _update(client: httpx.AsyncClient, args: argparse.Namespace) -> int:
    identity = await asyncio.to_thread(target.read_target, read_loader=False)
    menu = Menu(identity)
    configured = not args.manual and menu.installed
    output = args.output or Path.home() / _DOWNLOADS
    profile = args.profile or (menu.profile if configured else None)

    async def scan(prepared: PreparedUpdates | None = None) -> int:
        return await run_update(
            client,
            identity,
            output,
            only=tuple(args.only),
            dry_run=args.dry_run,
            driver_cache_fallback=args.driver_cache,
            profile=profile,
            extractor=args.extractor,
            prepared=prepared,
        )

    if not configured:
        return await scan()
    if args.dry_run:
        return await scan(PreparedUpdates(menu.root))
    if not await asyncio.to_thread(menu.resume):
        return 0

    with tempfile.TemporaryDirectory(dir=menu.root, prefix="scan-") as temporary:
        prepared = PreparedUpdates(Path(temporary))
        result = await scan(prepared)
        if not prepared.drivers and not prepared.monthly:
            print("No update was staged for WinRE.")
            return result
        category = "drivers" if prepared.drivers else "monthly"
        if prepared.drivers and prepared.monthly:
            if not sys.stdin.isatty():
                print(
                    "Run wuo update with --only drivers or --only monthly to stage one job."
                )
                return result
            reply = await asyncio.to_thread(
                input, "Stage drivers or monthly updates? [drivers/monthly] "
            )
            category = reply.strip().lower()
            if category not in {"drivers", "monthly"}:
                print("No update was staged.")
                return result

        current = await asyncio.to_thread(target.read_target)
        if replace(current, loader_guid=identity.loader_guid) != identity:
            raise ValueError("The running Windows build changed during the scan.")
        menu = Menu(current)
        if category == "drivers":
            source = prepared.directory / "drivers"
            await asyncio.to_thread(
                bundle.prepare_drivers,
                prepared.drivers,
                current,
                source,
                profile=profile,
            )
        else:
            (source,) = prepared.monthly
        await asyncio.to_thread(menu.publish, source)

    print("Update staged.")
    await asyncio.to_thread(menu.offer_restart)
    return result


async def _run(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        if args.command == "update":
            return await _update(client, args)
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
            output = args.output
            if output is None:
                if not downloads.safe_name(selected.id):
                    raise ValueError("The update has an invalid download folder name.")
                output = Path.home() / _DOWNLOADS / selected.id
            result: DownloadManifest = await downloads.download(
                client, selected, output, concurrency=args.jobs
            )
            if args.json:
                print(json.dumps(asdict(result), indent=2))
            else:
                print(f"Saved {len(result.files)} file(s) to {output.resolve()}")
            return 0
    _write_updates(updates, json_output=args.json)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wuo",
        description="Download Windows updates and install supported updates in recovery.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    setup = commands.add_parser(
        "setup", help="Add Windows Offline Update to the recovery menu."
    )
    setup.add_argument("--profile", type=Path, help="Registry-hook profile to save.")
    workflow = commands.add_parser(
        "update",
        help="Find and stage updates for this PC.",
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
        help="Limit updates to a category. Repeat to include several.",
    )
    workflow.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded.",
    )
    workflow.add_argument(
        "--driver-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use Windows Update Agent to export drivers (default: enabled).",
    )
    workflow.add_argument(
        "--output",
        type=Path,
        help="Choose a download folder.",
    )
    workflow.add_argument(
        "--profile",
        type=Path,
        help="Use a registry-hook profile for this run.",
    )
    workflow.add_argument(
        "--extractor", default="7z.exe", help="Path to the 7-Zip CLI."
    )
    workflow.add_argument(
        "--manual",
        action="store_true",
        help="Download or prepare files for manual servicing.",
    )
    status = commands.add_parser(
        "status", help="Check the staged update or verify the result."
    )
    status.add_argument(
        "--discard",
        action="store_true",
        help="Clear the pending job and keep its files and logs.",
    )
    commands.add_parser(
        "remove", help="Remove Windows Offline Update from the recovery menu."
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
    show = commands.add_parser("show", description="Show an update and its files.")
    show.add_argument("update_id")
    download = commands.add_parser(
        "download", help="Download all files for a selected update."
    )
    download.add_argument("update_id")
    download.add_argument("--output", type=Path, help="Choose a download folder.")
    download.add_argument("--jobs", type=int, default=3)
    latest = commands.add_parser(
        "latest", description="Find a cumulative or Defender intelligence update."
    )
    latest.add_argument("--release", help="Windows release, for example 25H2.")
    latest.add_argument("--arch", choices=("x64", "arm64", "x86"), default="x64")
    latest.add_argument("--defender", action="store_true")
    commands.add_parser(
        "media", description="Show the Windows Setup media download page."
    )
    prepare = commands.add_parser(
        "prepare", description="Prepare a verified offline servicing bundle."
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
    drivers = commands.add_parser(
        "prepare-drivers",
        description="Prepare signed driver exports for offline servicing.",
    )
    drivers.add_argument(
        "--export",
        type=Path,
        action="append",
        required=True,
        help="Driver export directory. Repeat for multiple drivers.",
    )
    drivers.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New bundle directory on the target Windows volume.",
    )
    drivers.add_argument(
        "--profile", type=Path, help="Optional local registry-hook TOML."
    )
    winpe = commands.add_parser(
        "winpe", description="Build a WinPE ISO for a prepared recovery bundle."
    )
    winpe.add_argument("--bundle", type=Path, required=True)
    winpe.add_argument("--output", type=Path, required=True, help="New .iso file.")
    winpe.add_argument(
        "--check-only",
        action="store_true",
        help="Verify in WinPE and pause before servicing.",
    )
    winpe.add_argument(
        "--bootex",
        action="store_true",
        help="Use Windows UEFI CA 2023 boot media with a compatible ADK.",
    )
    verify = commands.add_parser(
        "verify", description="Verify a recovery bundle and optional post-boot state."
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
        if args.command in {"setup", "status", "remove"}:
            menu = Menu(target.read_target(read_loader=False))
            if args.command == "setup":
                menu.configure("install", args.profile)
            elif args.command == "remove":
                menu.remove()
            elif args.discard:
                menu.discard()
            else:
                menu.status()
            return 0
        if args.command == "prepare-drivers":
            plan = bundle.prepare_drivers(
                args.export, target.read_target(), args.output, profile=args.profile
            )
            print(
                f"Driver bundle prepared at {args.output} ({len(plan.commands)} INF files)."
            )
            return 0
        if args.command == "winpe":
            from .winpe import build_iso

            build_iso(
                args.bundle, args.output, check_only=args.check_only, bootex=args.bootex
            )
            print(f"Bootable WinPE ISO prepared at {args.output}.")
            return 0
        if args.command == "verify":
            bundle.verify(args.bundle, post_boot=args.post_boot)
            print("Verification passed.")
            return 0
        return asyncio.run(_run(args))
    except (ValueError, OSError, httpx.HTTPError) as error:
        print(f"wuo: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Canceled.", file=sys.stderr)
        return 130
