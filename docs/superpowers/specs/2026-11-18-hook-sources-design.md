# Hook sources for 0.3.0

Hook profiles can be selected by name from the bundled official collection or from registered Git repositories. Adding a source makes its profiles available. The user selects the profiles for setup or for a particular update. Local TOML paths continue to work.

## Commands

- `wuo hooks list` shows available profiles, saved selections, source revisions and freshness.
- `wuo hooks add <repo> --name vendor [--ref <branch-or-tag>]` reads `hooks/*.toml` at a pinned Git commit.
- `wuo hooks refresh [source]` checks the tracked ref and updates available profiles. Saved setup selections and pending jobs keep their captured bytes.
- `wuo hooks remove vendor` removes the source from discovery. Existing saved selections retain their bytes and identify the removed source.
- `wuo setup --hook official/usb4` saves a named profile. Repeat `--hook` to combine profiles. `--profile <path>` selects a local file.
- `wuo update --hook vendor/profile` selects profiles for that run. Without an override, the saved setup profile applies. `--no-hooks` explicitly selects none for a run.

## Visibility

Setup, update and status print the effective profiles and their pre/post registry operations. Explicitly report when no hooks apply. Show saved setup selections separately from a pending job's captured selections, with the pending selections governing that job. Print the captured registry operations in recovery before applying the job.

The provenance record captures each selected profile name, source, commit or local path, and SHA-256. Save the metadata with the protected setup snapshot and within the hashed bundle inventory so it is available after restart. Support 0.2.0 saved profiles and existing prepared jobs without metadata by describing their local snapshot.

## Freshness

Compare a selected profile's bytes with the current source version. A change or removal produces a warning with an explicit refresh/reselection command. Source checks have a bounded timeout and report an unknown result on failure. Ordinary Windows setup/update/status checks can read sources, while recovery uses the captured bytes. A new commit with the same profile bytes counts as current. Local files are compared with the saved snapshot where the source path is still available.

Refresh updates discovery only. Re-run setup with the named hook to save a refreshed selection. A pending job keeps its verified hook snapshot until completed or explicitly discarded.

## Sources and validation

Package the official `hooks/` directory in the wheel and source archive. It is available without fetching a repository. External sources use Git over HTTPS or SSH and require the Git executable. Support local Git repositories for offline use and tests. Read regular root-level TOML blobs from a commit without executing repository code, checking out files, or initializing submodules.

Validate profiles with the existing registry-hook schema. Reject duplicate registry operations across combined profiles and display which profiles conflict. Restrict names and paths, limit content sizes, and atomically publish a validated source snapshot under the user's local application data directory. Resolve source refs explicitly and preserve recorded commits on failures.

## Implementation choices

The official alias is reserved. Its bundled revision is the installed package version, and its upstream is this project's default branch for freshness checks. External source registration accepts a user-chosen alias and optional ref. Use the user's existing Git authentication for supported transports.

The existing offline servicing runner and WinRE install/remove transactions remain the servicing path. This release adds profile discovery, selection, visibility and provenance around that path.

## Validation

Use local repositories to test changed, unchanged and removed hooks, invalid TOML, rejected object modes, offline failures, and source refresh without implicit activation. Test explicit CLI selections, persistent setup snapshots, override behavior, metadata-free legacy snapshots and pending jobs with different saved selections. Inspect the built wheel and test the complete hook inventory with the WinRE runner fixtures.
