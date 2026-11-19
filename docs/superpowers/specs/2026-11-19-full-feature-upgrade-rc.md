# Full feature upgrades from the wuo recovery menu

Users run `wuo update` in Windows, approve the restart once a job is ready,
then select **Troubleshoot → wuo** in WinRE. The menu continues the full upgrade
and reports a recovery error if it cannot safely proceed. The same entry serves
CBS packages and drivers. Setup's own subsequent restarts remain automatic.

## Release scope

The main deliverable of 0.4.0-rc1 is a full keep-apps-and-data upgrade from an
older Windows release under `wuo update`, behind an explicit RC opt-in.
Set the project version to `0.4.0-rc1` for the CI tag check against
`v0.4.0-rc1`. The wheel and PyPI normalize the version to `0.4.0rc1`.
The stable 0.4.0 release can offer the route through the
normal update flow once validation passes. Full upgrades still require a
separate confirmation with target and hook details. Enablement packages keep
using the existing CBS route.

The RC takes a trusted Microsoft ISO or mounted media selected by the user.
It checks the chosen image's build, edition, architecture, and default UI
language against the live installation, authenticates Setup, and requires
Setup's keep-apps-and-data compatibility result. Missing media and blocked
migrations are reported as incomplete. A dry run previews the route without
committing Setup. Windows Update offer discovery and policy handling follow
the existing flow. A version-specific upgrade or USB4 profile is never required.
The ISO supplies payload for an eligible full feature offer. It cannot start an
upgrade by itself when the configured update service offers none. Match media
to the offer's target release and base build, allowing a different cumulative
revision for that target when native Setup accepts the migration.
Finish pending jobs and required restarts before planning a full upgrade.
Process other supported updates for the running build before offering an
eligible full upgrade. Unavailable feature media or a failed feature-only
compatibility check leaves the feature offer incomplete and does not prevent
unrelated updates. Once a full-upgrade job is accepted, it runs alone. Discard
the previous scan's offers and rescan against the replacement build.

Full feature upgrades default to wuo-managed rollback and its backups.
`wuo update --no-rollback` explicitly skips those backups and automatic
restoration for that upgrade. It does not disable Windows Setup's own recovery
or change the existing recovery safeguards for CBS packages and drivers. The
job preview and confirmation show which mode will be used. With rollback
disabled, a rejected hook still needs a persistent recovery path with a way to
repair and retry the hook. A failed check must never authorize the new OS to
boot. Both modes need a cold-restart test before either can be armed.

## Approaches

- **Menu handoff to Setup's temporary SafeOS.** Selected for consistency with
  the existing experience. Keep the installed WinRE menu as the explicit
  entry point and let Setup run its own offline phase. The handoff has to
  preserve Setup's target and rollback boot entries.
- **Direct restart into Setup's temporary SafeOS.** Has fewer transitions
  and may be useful as a lab control. It bypasses the entry the user wants.
- **External boot media.** Useful for recovery and testing. It adds a manual
  boot and media preparation step to every upgrade.

## Lifecycle and ownership

1. From the running installation, verify Setup compatibility and capture the
   target identity, media, effective hooks, and native boot configuration in
   one job. Serialize with the existing update workflow. Capture only the
   declarative registry hooks already accepted by wuo. A pending job keeps
   the same captured hooks through retry.
2. Before invoking any committing Setup command, persist recovery state and
   apply and verify the captured pre hooks. With rollback enabled, save the
   source SYSTEM hive before the hooks and restore the source values if a
   failure occurs before Setup starts. With rollback disabled, require a safe
   way to apply the hooks without retaining a backup. Once a Setup
   command that may commit has been invoked, treat the job as potentially
   committed until native state proves otherwise. Test `/Auto Upgrade /NoReboot`
   as the point to prepare Setup's own transient
   SafeOS image while Windows is still running. Record its identity before
   modification and retain the stock copy. Stage the temporary image with a
   launcher that runs Setup's original SafeOS process and checks its resulting
   state before calling the hook runner. Keep the stock installed WinRE
   available. A job cannot be ready unless the real rehearsal proves that a
   restart into WinRE and selection of **wuo** can reach the exact prepared
   SafeOS without bypassing the launcher. The native method for preserving
   Setup's one-shot boot target is a lab decision, not an assumed contract.
3. After successful SafeOS and before the replacement kernel starts, locate
   the exact target with its volume and machine identity. Load SYSTEM, resolve
   its default control set, apply captured post hooks, read
   them back, and unload the hive. Only the verified path requests a boot of
   the replacement OS. Empty profiles still require target and Setup checks.
   A failed hook and a power interruption must leave the next normal boot
   on a verified recovery or rollback path until the job authorizes the new
   installation. An error prompt in PE alone does not satisfy this condition.
4. With rollback enabled, a rejected post hook after commit must initiate a
   verified rollback to the previous bootable Windows installation. Prefer Setup's own rollback if the
   lab proves it is available at this precise phase. A saved hook hive alone
   cannot undo the filesystem migration or boot configuration changes.
   With rollback enabled, before accepting an upgrade, require a recoverable prior state and a
   tested path to restore the Windows installation and its Windows boot files.
   The preboot failure test passes only after the original Windows build
   starts and the source identity and original boot-critical values verify.
   Persist the rollback outcome through an interruption and avoid recovery
   reboot loops. If the automatic rollback itself fails, retain an independent
   recovery route and explain how to restore the saved state.
5. Show the durable job state and recovery instructions when an operation
   fails. An in-progress Windows Setup cannot be discarded with the simple
   CBS job cleanup. Prove cancellation before commit, rollback after commit,
   and a deliberate retry without modifying other boot entries. After Windows
   starts, `wuo status` verifies the actual build, Setup completion and the
   applicable hook or rollback receipt.

Identify the OS volume, active Windows boot store, and recovery partition
independently. Both common single-disk UEFI layouts and split Windows/EFI
layouts use the same code path. Ambiguous loaders, inaccessible volumes,
unrecognized SafeOS startup, and an unavailable rollback route stop the
operation. Do not change Windows Update policy or a third-party boot manager.
Retain the old menu label as an ownership identity so users can upgrade or
remove an existing registration.

## Release evidence

The current VM experiment used a same-build 26H2 repair and a manually prepared
temporary SafeOS. SafeOS returned to its shell, the hook runner applied all
five optional USB4 settings and the values persisted at the desktop. A
mismatched hook held in PE in the current session. A cold restart from that
rejected state reached the replacement kernel and completed native Pre and
Post SysPrep. The temporary SafeOS hook wrapper alone therefore cannot enforce
recovery or rollback after a restart. These results establish the hook boundary
and disqualify the wrapper alone as the install path.

In a second replay, native BCDEdit selected the Setup-generated **Windows
Rollback** entry. It booted SafeOS and ran the recovery handler. Setup logged
that it would retry the replacement boot, with no evidence that the source
installation was restored. Do not use the presence or label of this entry as
proof of rollback. The builder must demonstrate an explicit rollback mechanism
or a restorable snapshot under the relevant live failure and cold-restart tests.

A separate split-EFI Pro trial backed up just the Windows volume to a virtual
USB disk with Windows Backup. Standalone recovery media restored the volume
while the guest's EFI disk was detached. After reattaching EFI, Windows
cold-booted with the original marker, machine GUID, and build. This establishes
a candidate Windows-volume restore. The next rehearsal needs recovery after
Setup changes the boot state, with the relevant EFI and WinRE volumes present.

A recovery-media test redirected one BCDEdit write to a scratch EFI store
using `/sysstore` on fixed virtual disks. The same command failed with the
EFI copies attached over USB. Offline inspection found the test edit in the
scratch boot entries and metadata changes in the primary BCD. Treat this as
a candidate for redirecting writes inside a single recovery session. The
actual Setup process still needs to demonstrate that the primary boot path
and firmware routing stay under recovery's control, including on cold restart.

The follow-up started native SafeOS from the primary EFI with two fixed disks.
`/sysstore` failed before the handler ran. A separate fork started SafeOS from
scratch through a one-shot firmware entry. Setup finished SafeOS and wrote
the replacement boot sequence into scratch. The primary BCD's default,
display order, and boot sequence kept their previous values. Setup also
moved scratch to the front of the persistent UEFI boot order. After a
deliberately rejected USB4 hook, a fresh cold boot followed that firmware
order and ran the replacement kernel. A separate scratch EFI store alone
therefore fails the release gate. A usable boot gate must prevent the
replacement kernel from starting even if Setup changes firmware boot order.

Before publishing an install-capable RC, run the workflow through the actual
WinRE menu without a hypervisor timing dependency. Test a real keep-apps version
upgrade, single-disk and split EFI, an interrupted preparation, a failed hook,
fallback when SafeOS cannot start, and rollback. Check that the experimental
launcher never propagates to the installed recovery image. Any real keep-apps
version upgrade can supply the generic RC evidence. A genuine 25H2 Pro source
is still needed for the requested 25H2 to 26H2 validation. Physical USB4 and
Limine use need a recoverable test before being presented as supported.

Microsoft documents [Setup command options](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/windows-setup-command-line-options)
and [WinPE launch ordering](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/winpeshlini-reference-launching-an-app-when-winpe-starts).
Microsoft documents [OS uninstall](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/dism-uninstallos-command-line-options)
against an online Windows image. That command does not establish how to
initiate rollback from SafeOS before first boot.
Changing Setup's temporary SafeOS image is experimental. Do not treat the
ordinary WinPE documentation as approval for the Setup-specific handoff.
