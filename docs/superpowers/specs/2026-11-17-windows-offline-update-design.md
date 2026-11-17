# Windows offline update CLI

## Goal

Provide a general-purpose CLI for finding Windows updates, downloading complete packages for manual use, and applying supported updates to an installed copy of Windows from stock Windows Recovery Environment (WinRE). A user can enter recovery and start one command. The runner handles package order, offline hooks, and verification. The user decides when to restart.

The initial workflow is designed for normal installations and systems whose Windows and EFI partitions live on different disks. The USB4 registry changes are an opt-in example profile using the same hooks available to other users.

## Approaches

| Approach | Tradeoff |
| --- | --- |
| Stock WinRE with a generated recovery runner | The user enters recovery and selects the target drive letters. The runner uses the Windows tools already present in stock recovery. This is the selected approach. |
| Dedicated WinPE environment with a startup runner | Can automate the boot handoff and start servicing on boot. It requires provisioning a boot image and validating boot configuration, volume access, and device detection on hardware. This is a potential later release after hardware validation. |

## Release scope

- **Search and show.** Search the Microsoft Update Catalog by KB, title, or hardware ID. Show the result's update ID, product or Windows release, architecture when known, revision or date when available, provenance, and package list. Allow selection by stable update ID. A KB with multiple product or architecture matches requires a specific selection. Catalog search reflects published availability. A result line indicates the level of applicability information actually available.
- **Download.** Download all files published for the selected Catalog item, including checkpoint MSUs when listed. Handle security and cumulative updates, rollups, .NET, drivers, feature and enablement updates offered through the Catalog, Defender platform and Windows Security platform updates. Use Microsoft's standalone Defender intelligence release links for definition packages. For full Windows Setup upgrades, v1 directs users to Microsoft's installation-media download page for edition, language, and architecture selection. Download-only use is independent of the installed Windows version.
- **Optional latest lookup.** Use wufeed as a convenience for supported Windows release and architecture lookups. Keep every checkpoint file in its result and resolve the chosen update against official Microsoft metadata or release notes before treating the set as eligible for offline apply. Keep Catalog search and manual imports usable when the feed is unavailable.
- **Prepare and apply.** Support offline DISM servicing for Windows 11 x64 cumulative updates, their servicing stack and checkpoint requirements, and explicitly supported standalone quality packages. The initial test case is Windows 11 25H2 KB5132061. Package type and release metadata determine the install route. A file extension alone is insufficient to classify a CAB or MSU. Drivers, Defender installers, and major upgrades have download and manual-install workflows in this release. Automatic Windows Update controls can be designed separately once their behavior on Home is verified.

## Command workflow

| Command | Behavior |
| --- | --- |
| `search QUERY` | Search published results with product, architecture, and type filters. Offer human-readable output and JSON. |
| `show UPDATE_ID` | Display release details and the full file list, including known prerequisites and the intended servicing route. |
| `download UPDATE_ID --output DIR` | Download the selected update's files and write a manifest usable for manual servicing or preparation. |
| `prepare --download DIR --output DIR [--profile FILE]` | Stage a recovery bundle for the running Windows installation. Support importing predownloaded packages when matched to a supported release and verified against published metadata or an explicitly supplied trusted manifest. Preview the target, package order, hooks, and any prerequisites that need manual resolution. |
| `verify --bundle DIR` | Validate document and file integrity on normal Windows before using the bundle. After the update, check the running build and any declared post-boot assertions against the plan. |

Command spelling and switches are proposed defaults. The generated recovery runner exposes a read-only `check`, an explicit `apply`, and a `repair-hooks` recovery command. Each accepts the actual Windows drive letter and, for split Windows and EFI layouts, the EFI drive letter. The user enters stock WinRE through the normal recovery menu or installation media. A locked target requires the user's normal BitLocker unlock procedure. Recovery credentials remain with the user.

## Package acquisition and trust

The Catalog adapter uses Microsoft's public search and download pages. Treat their response format as changeable. Keep parsing behind the adapter, test representative saved responses, and report a source error when a response can't be interpreted. Download over HTTPS from documented Microsoft distribution hosts and validate the destination after each redirect. Treat wufeed as advisory metadata. Validate Microsoft-published digests when available and record SHA-256 hashes for local bundle integrity. A locally computed hash establishes later file consistency. Windows servicing provides package signature and applicability checks during apply.

Use bounded asyncio concurrency across independent HTTP transfers. Stream to temporary files, verify before rename, and publish a complete manifest only once its required files are ready. Preserve already verified files when a transfer fails. Use timeouts, bounded retry for transient errors, and clear progress or failure output. The manifest records original filenames, sources, digests and their provenance, target metadata, order, and servicing route.

## Recovery bundle and target selection

Use a generated Windows command runner and stock Windows recovery tools such as DISM, BCDEdit, and Reg. The Python CLI, uv, package downloads, and release-specific preprocessing run in normal Windows. The recovery runner works without a network connection. Prepare a native servicing-stack CAB ahead of recovery when the documented release path requires explicitly extracting it, and keep the target cumulative MSU and its checkpoints in their own directory. Unresolved prerequisites block creation of the apply plan.

Preparation records the selected Windows installation identity, build, architecture, and a per-job marker written to its OS volume. Where the EFI volume is separate, record the corresponding Windows loader identity from the active BCD store. The recovery check requires an explicitly supplied target drive, matches the marker and expected Windows identity from the offline installation, and verifies the named loader entry in the specified EFI store when required. Missing, ambiguous, or mismatched identities stop the runner. Check paths, available workspace, package presence, and offline hive readability before servicing. Use invariant identifiers and machine-readable values where practical. A localized or unrecognized result must fail closed. A marker identifies a prepared job on the normal installation and can be duplicated by cloning. The operator also identifies the physical disk in WinRE.

Keep the recovery bundle on trusted local media and verify it after copying and before entering recovery. Document the exact checks available on stock WinRE. A runner launched from media is itself trusted input. Native DISM signature checks cover servicing packages, while prepare and verify cover the runner, hook snapshot, manifest, and package digests at the host-side boundary.

## Apply, hooks, and recovery

1. Repeat target checks during apply and prevent overlapping runs for the same job. Check for existing pending servicing state and report an actionable stop if it prevents this plan from proceeding. Write durable logs outside WinRE's RAM drive.
2. Save the current values affected by the pre-servicing hooks before applying them to the verified offline installation. An unsuccessful hook blocks the next step.
3. Apply the documented package sequence with DISM. Service prerequisites first when required. Let DISM discover checkpoints alongside the target cumulative update where Microsoft documents that workflow. Process one servicing operation at a time, retaining native exit codes, package states, and logs.
4. After successful DISM staging, back up the affected values as they exist at that point, then run and verify the configured post-servicing actions against the offline target. Close all loaded hives. Record the result as ready for a user-initiated reboot. The report distinguishes offline staging from later Windows startup completion.
5. When a run fails or is interrupted, show the last confirmed step and package state and retain the registry backups and DISM logs. An uncertain mutation requires inspection before another apply. If servicing was staged successfully and first boot reset a hook value, `repair-hooks` rechecks the target identity and expected pre- or post-update build, backs up the current affected values, and runs only the configured post-servicing actions. Document independent hook restoration and Windows' quality-update uninstall tools. A registry backup can reverse hook edits. A system backup is the recovery path for broader failures.

The first release supports declarative pre- and post-servicing actions from a local TOML profile. Implement typed DWORD edits to existing values in the offline SYSTEM hive, with expectations and read-back verification. Resolve the next-boot control set through `Select\Default` when a rule targets that control set. Reject unsafe key or value syntax before generating commands, and snapshot the profile in the bundle. The example USB4 profile sets `Start = 0` for the existing `UASPStor`, `USBHUB3`, `USBXHCI`, `Usb4DeviceRouter`, and `Usb4HostRouter` service values after staging. It leaves the choice to use the profile with the operator. The builder can select the internal representation and process adapter after validating stock WinRE behavior.

## Development and release checks

- Use Python 3.15, `uv`, a committed lockfile, Ruff formatting and linting, strict mypy for production code, and pytest. Use explicit lazy imports where they defer command-specific initialization and pass the toolchain. Keep the recovery code isolated from host-only dependencies.
- Initialize a local Git repository with `main`, short-lived feature branches, and reviewable commits. Create a private GitHub remote using `gh`. Configure GitHub Actions for Windows CI with pinned actions, read-only default permissions, locked dependency installs, formatting, linting, typing, tests, and a wheel build. A release tag can build downloadable artifacts. Private visibility and the provisional name `windows-offline-update` are proposed defaults.
- Test Catalog parsing, ambiguous KB selection, checkpoint ordering, interrupted downloads, rejected payloads, wrong or locked volume selection, quoting, DISM exit and package-state handling, failed hooks, hive unloading, interrupted recovery execution, and post-hook repair with a verified target. CI mocks destructive servicing and verifies observable command invocations.
- Validate the actual recovery runner in a disposable Windows 11 VM with snapshots before treating the apply feature as ready. Exercise a current build with a real cumulative update, stock WinRE, changed drive letters, split Windows and EFI disks, the optional registry hook, and a deliberate failure. Verify the target returns to Windows on the expected build and test recovery on the snapshot. Record any gaps in physical USB4 coverage. This plan uses no spare SSD and keeps the daily installation outside development testing.

## References

- [Microsoft Update Catalog search](https://learn.microsoft.com/en-us/windows-server/administration/windows-server-update-services/manage/wsus-and-the-catalog-site)
- [Checkpoint cumulative updates](https://learn.microsoft.com/windows/deployment/update/catalog-checkpoint-cumulative-updates)
- [DISM package servicing and recovery](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/dism-operating-system-package-servicing-command-line-options?view=windows-11)
- [Microsoft's offline SYSTEM hive and control-set procedure](https://learn.microsoft.com/en-us/windows/client-management/troubleshoot-inaccessible-boot-device)
- [Windows Recovery Environment](https://support.microsoft.com/en-us/windows/experience/backup-recovery/windows-recovery-environment)
- [wufeed metadata and package coverage](https://github.com/rybbt/wufeed)
