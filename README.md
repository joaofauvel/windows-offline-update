# Windows Offline Update

`wuo` finds applicable updates in Windows and installs supported updates from the
Windows Recovery Environment (WinRE).

## Experimental software

**This tool changes Windows recovery configuration and services an offline
Windows installation. Failures can leave Windows or WinRE unable to boot and
may cause data loss.**

Keep a current, restorable backup of your Windows and EFI partitions, separate
recovery media, and any BitLocker recovery keys before using setup or applying
updates. Recovery may require another computer and manual repairs.

The recovery-menu workflow has been tested in disposable Windows 11 VMs,
including a layout with Windows and EFI on separate disks. Hardware-specific
validation of that workflow, including USB4 storage and Limine dual boot,
remains pending.

Hook profiles apply registry changes with system-level privileges. Review the
profile and confirm that its settings fit your installation before selecting
it. The USB4 profile changes boot-critical service settings.

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

Save the USB4 profile as `usb4.toml` if your Windows installation needs those
USB4 and storage service settings to boot. Setup saves a copy for later updates. [USB4 profile](hooks/usb4.toml)

```powershell
wuo setup --profile .\usb4.toml
```

From the checkout, the profile is at `.\hooks\usb4.toml`. Use
`wuo update --profile <path>` to choose a different profile for one run.

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
