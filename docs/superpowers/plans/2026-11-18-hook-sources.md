# Hook sources implementation plan

Implement the approved hook-source commands and the requested effective-hook visibility and stale-content warnings. The specification is in [Hook sources for 0.3.0](../specs/2026-11-18-hook-sources-design.md).

- [x] Fetch, pin, validate and refresh repository profile inventories, with bundled official profiles.
- [x] Resolve explicit selections and combined profiles, and write provenance snapshots.
- [x] Add the hooks commands and named selection options to the CLI, print effective operations and freshness, and support saved and legacy profiles.
- [x] Include selected metadata in the verified bundle and the recovery display.
- [x] Exercise local-repository freshness and CLI regressions, run repository checks, and verify 0.3.0 artifacts.

Validation passed with 186 tests, Ruff and mypy. The built 0.3.0 wheel contains the official USB4 profile and recovery resources. A disposable split-disk Windows 11 VM saved a local profile, switched to official/usb4, staged the exact Voice Clarity driver, then disabled future hooks while preserving the captured pending selection. WinRE applied that job, post-boot verification confirmed all five Start DWORDs were zero, and removal cleared the saved metadata and selection pointer. Git was absent in the VM and freshness was reported as unknown.
