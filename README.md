# Windows offline update CLI

A Python CLI for finding Windows update packages and preparing them for guided offline Windows servicing.

`wuo` is licensed under the GNU General Public License, version 3 or, at your
option, any later version. See the [license text](LICENSE).

## Prepare updates for this PC

From PyPI, use `uvx wuo update --dry-run`. From a source checkout, use the
commands below. For offline preparation, `--profile` accepts a path to a local
TOML file.

```powershell
uv run --locked wuo update --dry-run
uv run --locked wuo update
uv run --locked wuo update --only monthly --profile .\examples\usb4.toml
```

`wuo update` queries Windows Update Agent for offers applicable to the running
installation. It checks the native Windows architecture and uses the current
release and build to select standalone monthly packages. `--dry-run` queries
update metadata without downloading package files or preparing a bundle.

The default covers all offered categories. Repeat `--only` to combine
`monthly`, `features`, `dotnet`, `drivers`, `defender`, `security-platform`, or
`other`. Driver downloads require an exact offered update ID in the Catalog and
a matching hardware or compatible ID on a present device. Windows Update Agent
can offer drivers that have no public standalone download. The command reports
those offers for review. Driver packages are for manual servicing of a signed,
matching INF.

Packages are saved under `~/Downloads/WindowsOfflineUpdates` by default. Use
`--output` to select another directory. Repeated runs verify cached files and
report packages that need manual action. An exit status of `1` means the scan
or one of the selected offers requires review. Other packages can still be saved.

Run from an elevated terminal with 7-Zip available to prepare a supported WinRE
bundle in the same pass. `--profile` specifies the hook configuration for that
bundle. The current WinRE rule covers KB5132061 on supported Windows 11 25H2 x64
builds. Follow the recovery steps below for installation. Feature upgrades
that need full Windows Setup media use Microsoft's download page.

## Stage a driver offline

In an elevated Windows terminal, use `uvx wuo update --only drivers`.
Windows Update Agent's download cache is the default fallback for drivers whose
exact offer has no public Catalog package. Use `--no-driver-cache` for a
Catalog-only run or add `--dry-run` to preview the route. The export
checks the offered revision, present hardware, and the INF's architecture,
version, and signature. The `manifest.json` in the export directory lists the
matching INF under `payload/`. Use the export with the same PC and Windows
installation. An export failure can list same-version Catalog candidates for
review, with identical packages shown once.

If the offered driver has no standalone package, use the reported hardware ID
and driver version to look up a signed Windows driver from the PC or device
manufacturer. Extract the driver and keep its INF, catalog, and supporting
files together. In WinRE, identify the offline Windows volume. The example
uses `W:` for that volume and `D:` for the drive holding the extracted files.

```cmd
dism /Image:W:\ /Get-DriverInfo /Driver:D:\drivers\example.inf
dism /Image:W:\ /Add-Driver /Driver:D:\drivers\example.inf
```

Compare the driver information with the offered hardware ID, architecture,
and version before staging it. Check the driver version after Windows restarts.

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

The runner passed a [split-EFI VM test](docs/validation/first-winre-update.md)
using the Command Prompt from Microsoft installation media. The bundled
servicing rule covers KB5132061 on a supported 25H2 x64 baseline. Prepare the
bundle from an elevated terminal inside the target VM. Install the 7-Zip CLI
in that guest or supply its executable with `--extractor`.

```powershell
uv run --locked win-offline-update prepare --update-id 950b2422-28ad-4493-a96c-03725fd0d8c2 --download .\packages\KB5132061 --output C:\WinRE-Bundle
uv run --locked win-offline-update verify --bundle C:\WinRE-Bundle
```

Add `--profile examples/usb4.toml` to opt into the example registry hooks.
Use a disposable lab profile for a normal VM when testing hook behavior.

With `uvx`, save the [USB4 example](https://github.com/joaofauvel/windows-offline-update/blob/main/examples/usb4.toml)
as `usb4.toml`, or pass the path to an existing profile.

```powershell
uvx wuo update --only monthly --profile .\usb4.toml
```

For example, `key = 'Services\UASPStor'`, `name = 'Start'`, and `value = 0`
sets a DWORD in the default control set for the next boot. From WinRE,
the bundle runner loads `W:\Windows\System32\Config\SYSTEM` with `reg load`,
backs up the mounted hive, and reads its `Select\Default` value. If that value
is `1`, the relative key resolves to `ControlSet001\Services\UASPStor` in the
mounted hive. The runner unloads the hive after the configured pre- or
post-servicing edits. The example's five hooks use the `post` phase.
Set `scope = "system"` for a key relative to the root of the SYSTEM hive.
Profiles support `pre` and `post` edits to existing DWORD values.

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
