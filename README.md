# Windows Offline Update

`wuo` finds applicable updates in Windows and installs supported updates from the
Windows Recovery Environment (WinRE).

> [!WARNING]
> **Experimental software.** **This tool changes Windows recovery configuration
> and services an offline Windows installation. Failures can leave Windows or
> WinRE unable to boot and may cause data loss.**
> 
> Keep a current, restorable backup of your Windows and EFI partitions, separate
> recovery media, and any BitLocker recovery keys before using setup or applying
> updates. Recovery may require another computer and manual repairs.
> The recovery-menu workflow has been tested in disposable Windows 11 VMs,
> including a layout with Windows and EFI on separate disks. Hardware-specific
> validation of that workflow, including USB4 storage and Limine dual boot,
> remains pending.
> 
> Hook profiles apply registry changes with system-level privileges. Review the
> profile and confirm that its settings fit your installation before selecting
> it. The USB4 profile changes boot-critical service settings.

## Set up once

Install with `uv tool install wuo`. If `uv`
asks you to update your PATH, run `uv tool update-shell` and reopen PowerShell.
In an administrator PowerShell, run the setup command.

```powershell
wuo setup
```

For one-off commands, use `uvx wuo` in place of `wuo`. From a source checkout,
use `uv run --locked wuo`.

Setup backs up the installed recovery image and adds **Windows Offline Update**
to a copy of it. If setup fails, it attempts to restore the original image and
registration. If restoration fails, it reports the backup location for manual
recovery.

If your Windows installation needs the USB4 and storage service settings to
boot, select the bundled [USB4 profile](hooks/usb4.toml). Setup saves a copy for
later updates.

```powershell
wuo setup --hook official/usb4
```

Repeat `--hook` to combine profiles. Local TOML files also work:
`wuo setup --profile .\usb4.toml` saves a local profile, and
`wuo update --profile <path>` selects one for a single update.

## Hook profiles and sources

The `official` profiles are bundled with `wuo` and available without fetching a
repository. List profiles, saved selections, source revisions and freshness:

```powershell
wuo hooks list
```

Register a Git repository to make its profiles available. Only regular TOML
files directly under its root `hooks/` directory are read. Registration does
not enable a profile.

```powershell
wuo hooks add <repo> --name vendor
wuo setup --hook vendor/name
```

Add `--ref <branch-or-tag>` when registering a source to track a particular ref.
Repositories can use HTTPS, SSH or a local path. Git is required to register or
refresh sources and to check source freshness. Bundled and saved profiles still
work without Git; freshness checks report when their result is unknown.

Use `wuo update --hook vendor/name` to select a profile for one run, repeating
`--hook` to combine profiles. Without an override, updates use the saved setup
selection. Use `wuo update --no-hooks` to explicitly disable hooks for one run.

Setup, update and status show the effective profiles and their registry changes,
including when no hooks apply. They show a pending job's captured selection
separately from the saved setup selection. Recovery prints the captured registry
changes before applying the job.

```powershell
wuo hooks refresh
wuo hooks refresh vendor
wuo hooks remove vendor
```

Refresh updates available profiles. Saved selections never change automatically;
rerun setup with `--hook` to save a refreshed selection. Removing a source keeps
the saved profile copy. Setup, update and status warn when a selected profile has
changed, was removed, or cannot be checked. A prepared job keeps its captured
profile until it completes or is explicitly discarded.

To change hooks for a pending job, finish it or clear it with
`wuo status --discard` before staging a new job. Hook selection flags stop at
an existing job so that a one-run override cannot restart it with different
settings.

## Install updates

1. In an administrator PowerShell, run `wuo update`. Use `wuo update --only drivers`
   to scan for drivers only or `wuo update --dry-run` to preview the scan. When
   `wuo` prepares a recovery job, accept the prompt to restart into WinRE.
2. In WinRE, choose **Troubleshoot → Windows Offline Update**. The tool checks
   and installs the prepared job, then restarts Windows on success.

On a dual-boot PC, select your usual Windows entry after the restart. Run
`wuo status` in Windows to check the result.

Downloads go to `Downloads\WindowsOfflineUpdates` in your user folder.

To remove the recovery menu entry, run `wuo remove` in an administrator PowerShell.

## Supported updates

- **Drivers.** Recovery jobs use signed drivers exported from Windows Update
  Agent. `wuo` checks the offered revision, hardware, architecture, version, and
  signature. Windows Plug and Play selects the active driver after restart.
- **Cumulative updates.** The recovery workflow supports KB5132061 on eligible
  Windows 11 25H2 x64 builds `26200.6584` through `26200.9973`. Preparing this
  update requires `7z.exe` on your PATH or its path passed with `--extractor`.
- **Other offers.** `wuo update` can also find and download standalone .NET,
  Defender, Windows Security platform, and other available packages for separate
  installation. For feature upgrades that require full Windows Setup media, use
  `wuo media` to get Microsoft's download link.

## Download manually

Search by KB number, title, or hardware ID. Use the update ID for your Windows
release and architecture to show or download the package.

```powershell
wuo search KB5132061 --arch x64
wuo show 950b2422-28ad-4493-a96c-03725fd0d8c2
wuo download 950b2422-28ad-4493-a96c-03725fd0d8c2
```

Run `wuo update --manual` to download and prepare updates for manual servicing.
See `wuo prepare --help` for the supported cumulative update or
`wuo prepare-drivers --help` for a verified driver export. To archive a staged
or interrupted recovery job, run `wuo status --discard` in an administrator
PowerShell.

If Windows Update offers a driver without an export or standalone package, look
up a signed driver for the reported hardware ID and version from the PC or
device manufacturer. [Recovery validation notes](docs/validation/first-winre-update.md)

Run `wuo --help` to see the main commands.

## License

[GNU General Public License, version 3 or later](LICENSE).
