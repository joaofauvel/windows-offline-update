# One-Command Offline Update Preparation Plan

> **For agentic workers:** Use the execution and test-driven development skills to implement these steps.

**Goal** — Package the `wuo` entry point and let one command discover, download, and prepare available Windows updates.

**Architecture** — A read-only Windows Update Agent adapter identifies offered updates. The command resolves standalone files through Microsoft's Catalog, reuses verified downloads, and delegates eligible preparation to the existing bundle generator.

**Tech Stack** — Python 3.15, uv, pytest, Windows PowerShell, Windows Update Agent.

**Spec** — `docs/superpowers/specs/2026-11-17-wuo-update-design.md`.

- [x] Test and add a bounded Windows Update Agent scan adapter and update category classification. Treat partial scans as partial coverage.
- [x] Test default and repeatable category selection, dry runs, failures alongside successes, and verified package reuse. Resolve exact Catalog IDs and report feature-media or manual steps where packages cannot be resolved.
- [x] Add `wuo` to the wheel, delegate validated monthly preparation to the existing bundle machinery, and document profile selection and the recovery handoff.
- [x] Verify Ruff, mypy, pytest, the built wheel's aliases, and a host dry run. Exercise package reuse and preparation against an isolated target.

The live scan resolved Windows 11 25H2 x64 packages for the monthly update, .NET,
Windows Security, and Defender intelligence. Five driver offers lacked public
entries for the offered IDs. The isolated preparation test used the genuine
KB5132061 and checkpoint packages and verified the bundle and lab hook. The
[earlier VM test](../../validation/first-winre-update.md) verified the recovery
runner and reboot on a disposable virtual disk.
