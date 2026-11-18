# WinRE recovery menu workflow

The routine has two user actions. In Windows, run `wuo update --only drivers` to stage a job and accept the restart. In Windows Recovery Environment, choose **Troubleshoot → Windows Offline Update**. The recovery tool checks the staged job, applies it with the configured hooks, and requests a restart on success.

## One-time setup

An elevated `wuo winre setup` command saves the chosen hook profile and captures the Windows and active EFI identities. It backs up the active WinRE image, adds the tool to a working copy using Windows DISM, then registers the image and menu label with REAgentC. If the custom recovery tool slot is already in use, report it and stop setup. Roll back a failed setup using the original image and registration details. For later uninstall, remove the tool from the currently registered image so WinRE can retain newer servicing fixes. Verify that WinRE is enabled and registered after each operation. [Windows RE technical reference](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/windows-recovery-environment--windows-re--technical-reference?view=windows-11)

## Staging and recovery

- Use the existing job directory under `ProgramData\WinOfflineUpdate` on the target Windows volume. Copy the optional profile into the protected configuration directory during setup. Keep a single pending job in a directory writable only by administrators and SYSTEM. Publish the pending job only after verification and a complete copy.
- Let configured installations stage supported jobs from `wuo update`. Reuse Windows Update Agent applicability, verified driver exports, and the existing cumulative package logic where a supported plan exists. Stage one bundle per boot. If both routes are eligible, show them and let the user choose one. Ask before restarting and use `reagentc /boottore` for the handoff.
- Bind each installed launcher to one Windows installation. Resolve and temporarily mount its recorded Windows and EFI volumes by identity inside WinRE. Verify the target marker, the active loader in the Windows BCD, and the bundle hashes before invoking the recovery runner. Stop on a missing, locked, or ambiguous volume or any verification error. A locked volume can be unlocked through Windows recovery before retrying. On reboot, the user returns to Windows through the usual boot entry, including Limine on dual-boot systems.
- Preserve the runner’s interrupted-job guard, pre- and post-hook hive backups, and job logs. On the next Windows invocation, verify a completed job before replacing the pending record. Require explicit acknowledgement to retire an interrupted job, and retain its backups and logs.

## Approaches

- **Installed WinRE menu tool.** Setup adds one custom item using the Windows recovery tools mechanism. Subsequent runs use built-in recovery and the fixed staging location. This is the selected approach.
- **WinRE command prompt launcher.** A short command can use the same staging and target discovery. It keeps the built-in Command Prompt as a fallback when the custom tool slot is in use.
- **Dedicated WinPE media.** This provides an independent environment for testing storage access and running jobs when built-in recovery is unavailable. It requires separate boot media.

## Verification

The disposable split-disk VM prototype showed the menu item in WinRE and ran the existing check successfully after finding Windows and mounting the EFI volume by its recorded volume name. The installed Windows copies of `certutil.exe` and `findstr.exe` also ran inside WinRE and matched the plan SHA-256 to the prepared record. Include the needed binaries and message resources at setup.

Before release, exercise the installer, setup rollback, uninstall after WinRE servicing, staged-job integrity checks, interrupted jobs, and a full apply and post-boot verification from the menu on disposable images. Include a same-disk case, a second EFI partition, and a locked Windows volume.
