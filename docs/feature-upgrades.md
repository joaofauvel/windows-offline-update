# Hooks before the first boot of a feature upgrade

A full Windows Setup upgrade is not automated by `wuo`. Installations that need
registry changes to access their boot disk require a stronger guarantee than a
script that runs after the upgrade succeeds.

## Compatibility preview

The development branch can check a full Windows 11 upgrade offer with the
matching ISO. Use an administrator PowerShell and get the SHA-256 for the exact
ISO from the **Verify your download** section of Microsoft's download page.
The preview compares the entire file against the digest you supply and checks
the Setup signature. Use the published digest independently of the copy you
want to verify. [Microsoft Windows 11 downloads](https://www.microsoft.com/en-us/software-download/windows11)

```powershell
wuo update --check-feature 'C:\Downloads\Windows11.iso' --iso-sha256 '<Microsoft SHA-256>' --accept-setup-eula
```

Windows Update Agent must offer the full upgrade to this PC. The ISO needs a
matching build, edition, architecture and default system UI language. Setup
runs a compatibility scan and can write Panther logs. The preview stages no
recovery job and requests no restart. `--accept-setup-eula` accepts the
applicable Microsoft software license terms for the scan.

The preview displays the planned rollback policy. Full feature upgrades will
default to rollback and its backups. `--no-rollback` previews skipping them
for that upgrade. It does not change Windows Setup's own recovery.

## Preboot hooks

The required sequence is:

1. Setup applies the replacement Windows installation and finishes the registry
   changes that could overwrite the selected hook values.
2. While that installation is offline, identify and back up its SYSTEM hive,
   resolve its default control set, apply the captured hooks, and verify them.
3. Allow the replacement installation to boot only after those checks succeed.

The original installation, Setup's recovery environment, and the replacement
installation can have different SYSTEM hives. Modifying one does not establish
that the others contain the same values.

## Documented Setup boundaries

Microsoft documents pre-install and pre-commit custom actions before application
and reboot, plus completion actions after Setup finishes. These descriptions do
not establish a callback after the replacement hive's final changes and before
its first boot. A completion action cannot fix a boot dependency that has already
prevented Windows from starting.
[Setup custom actions](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/windows-setup-enable-custom-actions)

`/NoReboot` suppresses the restart after Setup's down-level phase. `/SkipFinalize`
prepares an upgrade without committing; `/Finalize` completes that preparation
and immediately restarts into the offline phase. These options do not by
themselves provide the required handoff after the offline phase.
[Setup command-line options](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/windows-setup-command-line-options)

Registry edits to a staged image, polling for hive files, or changing boot order
at an assumed time are not sufficient evidence of that guarantee. Setup can
perform more work after the edit or replace the boot configuration.

## Validation required before implementation

Use a disposable VM with a full replacement upgrade, rather than an enablement
package, to record when each new hive is created, loaded, migrated and finalized.
Probe the documented custom actions without changing boot-critical values first.

A proposed handoff must then demonstrate that it stops before the first boot of
the replacement installation, applies the captured profile to the correct hive,
and preserves Setup's ability to continue or roll back. Test failed hooks,
interrupted servicing, unexpected partitions, and Windows/EFI on separate disks.
Failure must leave an accessible recovery route without automatically attempting
the unverified boot.

An enablement package follows the CBS servicing route instead. Its build number
can change without a full replacement upgrade; `wuo` verifies the actual CBS
package identities and applies recovery hooks before restarting Windows.
