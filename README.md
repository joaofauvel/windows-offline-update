# Windows offline update CLI

A Python CLI for finding Windows update packages and preparing them for guided offline Windows servicing.

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
