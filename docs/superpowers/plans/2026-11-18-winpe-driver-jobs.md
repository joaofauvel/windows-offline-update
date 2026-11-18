# WinPE Driver Jobs Implementation Plan

**Goal** — Prepare a driver job in Windows and run it automatically on a single boot into dedicated WinPE media.

**Architecture** — Driver jobs reuse the existing target marker, EFI loader verification, DISM logging, and pre- and post-hook runner. A job-specific WinPE ISO records the target and EFI partition GUIDs and bundle hashes. Startup checks them before invoking the runner and reboots after success.

**Tech stack** — Python, PowerShell, DISM, and the Microsoft ADK and WinPE add-on.

**Scope** — Feature and quality update policy stays untouched. Driver jobs use explicitly selected Windows Update Agent exports. Every valid pre- and post-hook in the supplied profile runs through the existing hook mechanism. The first release assumes a manual one-time boot selection. Defender and Windows Security platform packages continue through their existing download paths.

1. Validate and assemble driver payloads from exported manifests. Test modified payloads, unsafe paths, unmatched metadata, and multiple exports.
2. Add driver bundles and run only their listed INF files. Keep cumulative job compatibility and test both hook phases with a driver job.
3. Create job-specific WinPE ISO media and validate the target Windows and EFI GUIDs, hashes, and job UUID during startup. Support check-only media and stop on any failure. Test across changed letters and incomplete jobs.
4. Exercise the runner in the split-disk test fixtures and boot a lab ISO if the required Windows tooling is available. Confirm the operator can verify the post-boot result.
