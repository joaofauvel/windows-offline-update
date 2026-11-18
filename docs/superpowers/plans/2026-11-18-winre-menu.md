# WinRE menu implementation

The approved design is in [WinRE recovery menu workflow](../specs/2026-11-18-winre-menu-design.md).

Keep the existing servicing runner and its manual entry points. Add an installed WinRE launcher and a protected pending job under `ProgramData\WinOfflineUpdate\menu` on the target Windows volume. The routine commands are `wuo setup`, `wuo update`, and `wuo status`.

- [x] Implement and exercise one-time setup and removal with built-in Windows tooling. Capture both volume identities, back up WinRE, check the custom menu slot, and recover from a failed registration. Release removal must use the currently registered image.
- [x] Implement and exercise the recovery launcher. Locate only the configured volumes, verify the entire staged inventory before running the existing check and apply commands, and retain the interrupted-job guard.
- [x] Test the Windows-side handoff. Copy the chosen profile into the protected configuration, reuse matching driver exports and cumulative bundles, publish a single pending job atomically, and verify or retire completed and interrupted jobs explicitly. Schedule WinRE only after the proposed job has been staged and the user has accepted the restart.
- [x] Simplify the command names, defaults, and common help text. Keep existing command names working and document routine commands before advanced tasks.
- [x] Run the existing tests, focused handoff tests, Ruff and mypy, then exercise setup, menu apply, verification, and removal in a disposable split-disk VM before claiming hardware readiness.

The split-disk VM passed setup, staging, menu apply, reboot, post-boot verification, removal and a subsequent WinRE boot. The scan fixture reused a real signed Voice Clarity export. A forced failure after registering the customized image restored the original WIM hash, location and menu state.
