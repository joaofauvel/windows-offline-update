# One-command update preparation

## Goal

`wuo update` is the usual entry point for finding updates, downloading their packages, and preparing supported updates for the guided recovery workflow. The command targets the current Windows installation by default.

## Selected approach

Search all update categories by default. Download the applicable packages the CLI can resolve from Microsoft, then prepare a recovery bundle for packages whose offline servicing rules are supported. The other approach was a monthly-only default with opt-in categories. That would reduce routine downloads but require extra commands for broader coverage. The user selected the all-category default with filters for individual runs.

## Command behavior

- Add `wuo` to `[project.scripts]` using the existing CLI entry point. The current executable and its subcommands remain available.
- `wuo update` covers monthly Windows cumulative and security updates, rollups, .NET updates, feature updates, drivers, Defender intelligence and platform updates, and Windows Security platform updates. Resolve applicability from the current system and trusted metadata. Where a source cannot establish applicability, report the result as requiring review. For full upgrades that require Windows Setup media, surface the official acquisition and installation route.
- Accept repeatable category filters such as `--only monthly`, `--only features`, and `--only monthly --only defender`. Other filters are `dotnet`, `drivers`, and `security-platform`. A `--dry-run` option shows planned downloads, servicing routes, and source failures before files are fetched.
- Reuse the downloader, verified manifests, and recovery bundle generator. Use an update-specific destination for each package set and carry the selected hook profile into applicable recovery bundles. Report files already verified and recoverable source or download failures in the run summary.
- End with each update's actual outcome and next action. Supported offline packages get the generated recovery command. Drivers, Defender installers, and full feature upgrades keep their package-specific servicing instructions. The initial verified automatic servicing route is KB5132061 on Windows 11 25H2 x64. A newly released update can be downloaded before its offline servicing rule is available.
- Continue using the operator-initiated reboot and stock WinRE runner approved for this project. The command performs discovery, download, and preparation from the normal Windows session.

## Acceptance

The short entry point works via uv. Tests cover default category selection, repeatable filters, dry-run behavior, update applicability, partial download failures, already verified packages, and the handoff for unrecognized servicing rules. Test the supported cumulative route in the disposable VM with its optional hook profile. Select discovery adapters during implementation and document their coverage in the command output.
