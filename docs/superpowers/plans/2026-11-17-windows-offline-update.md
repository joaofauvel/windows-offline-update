# Windows Offline Update CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Track steps with checkboxes.

**Goal** — Find and download Windows updates, then prepare and apply supported cumulative updates with optional offline registry hooks from stock WinRE.

**Architecture** — A Python host CLI handles search, downloads, metadata validation, and recovery bundle preparation. The generated CMD runner uses stock recovery tools and the prepared bundle. The installation and every recovery operation target an explicitly selected offline Windows instance.

**Tech stack** — Python 3.15, uv, Ruff, mypy, pytest, GitHub Actions, Windows CMD, DISM, BCDEdit, Reg.

**Spec** — `docs/superpowers/specs/2026-11-17-windows-offline-update-design.md`.

## Global constraints

- Use Python 3.15 with explicit lazy imports for optional CLI paths where the pinned tooling supports the syntax. Windows 11 x64 is the first supported apply target.
- Validate the entire download and report its provenance. Keep optional feed metadata advisory. Cumulative and checkpoint packages share a dedicated directory. An extracted SSU, when the release requires one, lives separately.
- Run DISM sequentially. Use stock WinRE, explicit target selection, and a user-initiated reboot. Release readiness depends on an actual run in a disposable Windows VM. The developer's daily installation is outside the test environment.
- Apply only local, declarative, validated registry hooks. Keep the USB4 settings in an example profile.
- Use topic branches off `main`. Run `uv sync --locked`, formatting, lint, typing, pytest, and wheel construction on Windows before merging.

## Review focus

- A transfer redirects away from the approved Microsoft host or sends a short body. The manifest cannot mark it complete. Task 2.
- The checkpoint is absent or the target release has an unresolved servicing-stack requirement. The tool produces a download report and refuses to emit an apply plan. Task 3.
- WinRE letters change or a second disk carries a copied BCD entry. Require an explicit disk selection and fail if the prepared identity checks cannot distinguish the target. Task 4.
- DISM fails after doing work or a power loss leaves an apply in progress. Preserve the journal and refuse a blind rerun. Task 4.
- First boot resets a USB4 DWORD. Repair rechecks the same job and runs only its post-servicing hook. Task 4.

## Files and responsibilities

| File or directory | Responsibility |
| --- | --- |
| `pyproject.toml`, `uv.lock`, `.python-version`, `.github/workflows/` | Build, pinned tools, checks, and tag artifacts |
| `src/win_offline_update/cli.py` | Parsing, human-readable and JSON output, and exit codes |
| `src/win_offline_update/{models,catalog,feeds}.py` | Update types, Microsoft Catalog lookup, optional feed and official Defender links |
| `src/win_offline_update/downloads.py` | Verified streaming downloads and manifests |
| `src/win_offline_update/{target,bundle,hooks}.py` | Windows inventory, servicing rules, bundle verification, and declarative hooks |
| `src/win_offline_update/recovery/apply.cmd.in` | Stock WinRE entry point and durable servicing journal |
| `examples/usb4.toml`, `docs/winre-test-vm.md` | Optional local hook example and isolated validation |
| `tests/`, `tests/fixtures/` | Recorded provider data, Windows command fixtures, failure injection, and bundle tests |

### Task 1 — Runnable CLI and Windows CI

**Files:** Create `pyproject.toml`, `.python-version`, `uv.lock`, `.gitignore`, `.gitattributes`, `src/win_offline_update/{__init__,cli}.py`, `tests/test_cli.py`, `.github/workflows/{ci,release}.yml`, and `README.md`.

**Produces:** `cli.main(argv: Sequence[str] | None = None) -> int` and the console entry point `win-offline-update`.

- [ ] Check the latest tooling's Python 3.15 support, and pin compatible versions before locking. Install uv using its official distribution if needed. Confirm `uv run python --version` selects 3.15.
- [ ] Write CLI smoke tests against `main(["--help"])` and an unknown command. Assert help returns 0 and the invalid command returns 2. Run `uv run pytest tests/test_cli.py -q` and verify failure before implementing the entry point.
- [ ] Configure the package and Windows CI. Use a public entry point backed by argparse, a default read-only GitHub token, and pinned action SHAs. Build tag artifacts and require the same checks on a release tag.
- [ ] Run `uv sync --locked`, `uv run --locked ruff format --check .`, `uv run --locked ruff check .`, `uv run --locked mypy src`, `uv run --locked pytest -q`, and `uv build`. Confirm all succeed, then commit `build: establish Python 3.15 CLI and Windows CI`.

### Task 2 — Search, show, and verified downloads

**Files:** Create `src/win_offline_update/{models,catalog,feeds,downloads}.py`, `tests/test_catalog.py`, `tests/test_downloads.py`, and saved provider fixtures. Modify `cli.py` and `README.md`.

**Produces:** `Update`, `UpdateFile`, `DownloadManifest`, `search(query: str) -> list[Update]`, `show(update_id: str) -> Update`, and `download(update: Update, destination: Path, *, concurrency: int) -> DownloadManifest`. The provider and downloader operations are async. The CLI runs each at its command boundary.

- [ ] Save representative Microsoft responses for a multi-product KB, a checkpoint cumulative update, an INF driver selected by hardware ID, a rollup, a Defender platform item, and a feature enablement item. Add fixtures for official Defender intelligence links and wufeed's complete file list. Confirm the exact protocol and canonical download hosts while collecting fixtures.
- [ ] Write tests `test_search_preserves_distinct_update_ids_for_same_kb`, `test_show_returns_target_and_checkpoint`, `test_unrecognized_catalog_response_is_source_error`, and `test_feed_outage_preserves_catalog_lookup`. Assert product, architecture, and provenance are reported when available. Run the tests and verify they fail.
- [ ] Implement Catalog adapters, optional latest lookup, and official Defender intelligence links. Surface full Setup media as an official Microsoft link. Add `search`, `show`, and optional JSON output. Pass the provider tests.
- [ ] Write tests `test_off_host_redirect_is_rejected`, `test_short_transfer_does_not_publish_manifest`, `test_digest_mismatch_keeps_existing_artifact`, and `test_checkpoint_files_all_reach_manifest`. Verify failures before implementing asynchronous streaming, bounded concurrency, retries, digest checking, and atomic finalization. Reject unsafe filenames and duplicate output paths.
- [ ] Run provider and downloader tests, then perform a read-only production smoke test of Catalog search and file metadata for KB5132061. Commit `feat: search and download Windows update packages`.

### Task 3 — Bundle preparation and hook profile validation

**Files:** Create `src/win_offline_update/{target,bundle,hooks}.py`, `examples/usb4.toml`, `tests/test_bundle.py`, and `tests/test_hooks.py`. Modify `cli.py` and package fixtures.

**Consumes:** `DownloadManifest` and verified local files from Task 2. **Produces:** `read_target() -> TargetIdentity`, `load_profile(path: Path) -> HookProfile`, `prepare(manifest: DownloadManifest, target: TargetIdentity, destination: Path, *, profile: HookProfile | None) -> BundlePlan`, and `verify(bundle: Path) -> VerificationResult`.

- [ ] Write tests `test_kb5132061_orders_ssu_before_target`, `test_missing_checkpoint_blocks_apply_plan`, `test_unresolved_ssu_blocks_apply_plan`, `test_driver_cab_cannot_become_quality_package`, `test_manifest_import_verifies_official_digests`, and `test_invalid_hook_key_cannot_inject_cmd`. Verify failures before implementation.
- [ ] Validate rules by exact release, architecture, and package role. For the first real fixture, model x64 KB5132061 with checkpoint KB5043080 and SSU 26100.9971. Inspect/extract the documented SSU from the intact Microsoft package before preparing the working directory. Validate its identity and leave DISM signature enforcement enabled.
- [ ] Implement host inventory, explicit bundle target identity, per-job marker, separate EFI BCD identity where needed, and host-side bundle verification. Preflight requires the captured baseline build. Post-boot verification requires the expected updated build and declared assertions. A changed bundle digest invalidates either check. Keep package download provenance and a local SHA-256 manifest.
- [ ] Define v1 hook entries with phase, SYSTEM key path rooted in the selected control set when applicable, DWORD value name, expected type or previous value if provided, and desired value. Validate user-supplied strings before emission into CMD. Use `examples/usb4.toml` for the five approved service names with desired `Start = 0`.
- [ ] Add `prepare` and `verify` commands. Confirm an unsupported package still downloads and gets a manual-servicing report. Run the bundle and hook suites, then commit `feat: prepare verified offline servicing plans`.

### Task 4 — Recovery runner, journal, and hook repair

**Files:** Create `src/win_offline_update/recovery/apply.cmd.in`, `tests/test_recovery_runner.py`, and recovery command fixtures. Modify `bundle.py`, `README.md`, and package-data configuration.

**Consumes:** `BundlePlan`, `TargetIdentity`, and validated hooks. **Produces:** the generated `recovery.cmd check|apply|repair-hooks W: [S:]` and a durable result log.

- [ ] Capture stock WinRE output samples from the VM for DISM, Reg, and BCDEdit. Confirm the generated script can run without Python, networking, or PowerShell. Require failure on unsupported or ambiguous command output.
- [ ] Write failing tests `test_wrong_identity_runs_no_mutation`, `test_ambiguous_efi_match_stops`, `test_read_only_check_does_not_call_apply`, and `test_locked_volume_explains_manual_unlock`. Implement argument, Windows marker and identity, offline hive readability, and EFI selection checks. Pass the tests.
- [ ] Write failing tests `test_failed_dism_preserves_exit_and_journal`, `test_interrupted_apply_refuses_automatic_retry`, `test_hook_failure_stops_next_step_and_unloads_hive`, and `test_success_records_staged_without_rebooting`. Implement an exclusive job lock, durable stage journal, package-state inspection, and sequential execution. Journal the start before each mutation. Record DISM's documented success and restart-required statuses distinctly and retain native errors. Place scratch and logs on the validated writable volume.
- [ ] Write failing test `test_repair_hooks_revalidates_target_and_never_launches_dism`. Implement the repair entry point for an eligible, previously staged job. Read current DWORDs, save the phase-specific backups, apply and verify the values, unload the hive, and report the result. Retain the post-DISM and first-boot distinction in the report.
- [ ] Execute Windows runner tests with fake command tools, using malformed and localized output fixtures to exercise fail-closed behavior. Run Task 1's full check commands, including the wheel build with the runner template, then commit `feat: service and repair updates from stock WinRE`.

### Task 5 — Disposable VM validation and release

**Files:** Update `docs/winre-test-vm.md` and add `docs/validation/first-winre-update.md`. Modify `README.md` with the verified runbook.

**Consumes:** A Windows 11 guest with a restorable snapshot, real Microsoft update files, and the built package. **Produces:** VM evidence, recovery instructions, and release-ready artifacts.

- [ ] Use the VM guide to create a Windows 11 25H2 x64 guest on file-backed disks. Record the guest build and EFI layout. Confirm snapshot restore works before attempting servicing.
- [ ] Run `check` with the wrong target and wrong EFI inputs. Retain the log demonstrating no servicing or hook writes. Repeat with a known-good target and verify its WinRE-assigned letters.
- [ ] From the snapshot, perform the real offline update on the guest and record DISM logs, hook verification, hive unload, and resulting build after reboot. Use KB5132061 if newer than the guest baseline, or a later documented quality update. Test the split-EFI layout on a VM clone.
- [ ] Test a deliberate hook failure, a recovery-only post-hook repair, and snapshot restore. Record the observed behavior, any variant the test did not cover, and a recovery path for each failure. Distinguish physical USB4 coverage from VM validation.
- [ ] Run CI against the final branch. Commit the validation and runbook before creating a release tag. Tag only an explicitly selected release after the documented VM acceptance checks pass.
