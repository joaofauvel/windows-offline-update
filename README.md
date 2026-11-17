# Windows offline update CLI

A Python CLI for finding Windows update packages and preparing them for guided offline Windows servicing.

## Find and download packages

```powershell
uv run --locked win-offline-update search KB5132061 --arch x64
uv run --locked win-offline-update show 950b2422-28ad-4493-a96c-03725fd0d8c2
uv run --locked win-offline-update download 950b2422-28ad-4493-a96c-03725fd0d8c2 --output .\packages\KB5132061
```

Search accepts a title or a quoted hardware ID as well as a KB number. Add
`--kind Drivers` to filter the first Catalog results page, or narrow the search
with a more specific query. Use the update ID to select the correct release and
architecture. Add `--json` for machine-readable results.

`latest --release 25H2 --arch x64` uses wufeed to find a candidate KB and gets
the actual package list from Microsoft. `latest --defender --arch x64` reports
the Defender intelligence package. Use `download defender-x64` with a fresh
output directory for the current definitions. `media` opens the workflow by
printing Microsoft's Windows Setup media download link.

Each download records the complete package list and the observed SHA-256
digests in `manifest.json`. The downloader verifies Microsoft digest metadata
when present. Use the release notes or the package-specific installation method
for manual servicing.

## Windows Recovery Environment lab

The WinRE runner is awaiting real servicing validation in a
[disposable Windows VM](docs/winre-test-vm.md). The bundled servicing rule
currently covers KB5132061 on a supported 25H2 x64 baseline. Prepare the
bundle from an elevated terminal inside the target VM. Install the 7-Zip CLI
in that guest or supply its executable with `--extractor`.

```powershell
uv run --locked win-offline-update prepare --update-id 950b2422-28ad-4493-a96c-03725fd0d8c2 --download .\packages\KB5132061 --output C:\WinRE-Bundle
uv run --locked win-offline-update verify --bundle C:\WinRE-Bundle
```

Add `--profile examples/usb4.toml` to opt into the example registry hooks.
Use a disposable lab profile for a normal VM when testing hook behavior.

Enter stock WinRE. Identify the prepared Windows volume and its EFI system
partition in DiskPart. Assign an unused letter to the verified EFI partition
if needed. The examples use `W:` for Windows and `S:` for EFI. The runner
checks the prepared job marker, Windows identity and build, and the EFI store's
Windows loader. It expects standard English command output and verifies file
sizes in recovery. Run the full hash verification in Windows after copying the
bundle.

```cmd
W:\WinRE-Bundle\recovery.cmd check W: S:
W:\WinRE-Bundle\recovery.cmd apply W: S:
```

After a successful run, review the log path printed by the runner and restart
when ready. After Windows finishes startup, run `verify --bundle C:\WinRE-Bundle
--post-boot` to verify the build and configured post-hook values.

The job folder under `ProgramData\WinOfflineUpdate\jobs` contains the DISM
logs, phase-specific registry backups, and the captured DWORD values. An
interrupted apply keeps its start record and requires inspection. To reverse a
hook edit, use its recorded control-set path and previous value to restore
only that DWORD in the offline SYSTEM hive. For a failed update, use WinRE's
**Uninstall latest quality update** or restore the VM snapshot.

If a successful staging run is followed by a boot failure caused by a changed
post-hook value, return to WinRE and run
`W:\WinRE-Bundle\recovery.cmd repair-hooks W: S:`. This rechecks the prepared
target and reapplies the post-servicing hooks without running DISM.

## Development

Use Python 3.15 and uv.

```powershell
uv sync --locked
uv run --locked win-offline-update --help
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy src
uv run --locked pytest -q
uv build --no-sources
```

Use a [disposable Windows VM](docs/winre-test-vm.md) for recovery integration tests.
