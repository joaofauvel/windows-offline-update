# Windows offline update CLI

A Python CLI for finding Windows update packages and preparing them for guided offline Windows servicing.

## Find and download packages

```powershell
uv run --locked win-offline-update search KB5132061 --arch x64
uv run --locked win-offline-update show 950b2422-28ad-4493-a96c-03725fd0d8c2
uv run --locked win-offline-update download 950b2422-28ad-4493-a96c-03725fd0d8c2 --output .\packages\KB5132061
```

Search accepts a title or a quoted hardware ID as well as a KB number. Add
`--kind Drivers` to filter the first Catalog results page, or narrow the search
with a more specific query. Use the update ID to select the correct release and
architecture. Add `--json` for machine-readable results.

`latest --release 25H2 --arch x64` uses wufeed to find a candidate KB and gets
the actual package list from Microsoft. `latest --defender --arch x64` reports
the Defender intelligence package. Use `download defender-x64` with a fresh
output directory for the current definitions. `media` opens the workflow by
printing Microsoft's Windows Setup media download link.

Each download records the complete package list and the observed SHA-256
digests in `manifest.json`. The downloader verifies Microsoft digest metadata
when present. Use the release notes or the package-specific installation method
for manual servicing.

## Development

Use Python 3.15 and uv.

```powershell
uv sync --locked
uv run --locked win-offline-update --help
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy src
uv run --locked pytest -q
uv build --no-sources
```

Use a [disposable Windows VM](docs/winre-test-vm.md) for recovery integration tests.
