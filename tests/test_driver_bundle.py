import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from win_offline_update import driver_bundle
from win_offline_update.target import TargetIdentity

TARGET = TargetIdentity(
    "25H2", 26200, 9974, "x64", Path("C:/Windows"), "machine", "", "Client"
)
UPDATE_ID = "d7a01c2d-9d66-4c6b-bd48-9b1cbcbd0c1e"
HARDWARE_ID = r"PCI\VEN_1ABC&DEV_2002"
FILES = {
    "native/offer.inf": b"matching inf",
    "native/other.inf": b"unlisted inf",
    "native/offer.cat": b"catalog",
    "native/bin/driver.sys": b"driver binary",
}


def _export(path: Path, *, update_id: str = UPDATE_ID, **overrides: object) -> Path:
    for relative, content in FILES.items():
        file = path / "payload" / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    manifest = {
        "schema_version": 1,
        "source": "Windows Update Agent",
        "update_id": update_id,
        "update_revision": 42,
        "hardware_id": HARDWARE_ID,
        "title": "Vendor driver 1.2.3.4",
        "architecture": "x64",
        "windows_release": "25H2",
        "windows_build": 26200,
        "infs": ["native/offer.inf"],
        "files": {
            name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in FILES.items()
        },
        **overrides,
    }
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _link_directory(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture
def windows_checks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    verified = []

    def verify_signature(inf: Path, payload: Path) -> None:
        verified.append(inf.relative_to(payload).as_posix())

    monkeypatch.setattr(driver_bundle, "_verify_inf_signature", verify_signature)
    monkeypatch.setattr(driver_bundle, "_verify_windows_matches", lambda *_: None)
    return verified


def test_stages_entire_payload_but_selects_only_listed_infs(
    tmp_path: Path, windows_checks: list[str]
) -> None:
    first = _export(tmp_path / "one")
    second = _export(tmp_path / "two", update_id="339c8a5c-60d9-4eca-9ea3-a8160cc00c78")
    (first / "local-notes.txt").write_bytes(b"outside payload")
    bundle = tmp_path / "bundle"
    (bundle / "packages").mkdir(parents=True)

    staged = driver_bundle.stage_driver_exports([first, second], TARGET, bundle)

    assert staged.commands == (
        "packages/0001/native/offer.inf",
        "packages/0002/native/offer.inf",
    )
    assert windows_checks == ["native/offer.inf", "native/offer.inf"]
    expected_files = {
        f"packages/{index}/{name}": data
        for index in ("0001", "0002")
        for name, data in FILES.items()
    }
    assert {
        path.relative_to(bundle).as_posix(): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
    } == expected_files
    assert staged.paths == {
        name: hashlib.sha256(data).hexdigest() for name, data in expected_files.items()
    }
    assert staged.size == {name: len(data) for name, data in expected_files.items()}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("source", "Unknown"),
        ("update_id", "not-a-uuid"),
        ("update_revision", True),
        ("update_revision", 0),
        ("architecture", "arm64"),
        ("windows_release", "24H2"),
        ("windows_build", 26100),
        ("hardware_id", ""),
        ("infs", []),
        ("infs", ["native/offer.cat"]),
        ("infs", ["native/missing.inf"]),
        ("infs", ["native/offer.inf", "native/offer.inf"]),
        ("infs", ["native/../native/offer.inf"]),
        ("files", {"native//offer.inf": {"size": 12, "sha256": "a" * 64}}),
    ],
)
def test_rejects_invalid_export_metadata(
    tmp_path: Path, windows_checks: list[str], field: str, value: object
) -> None:
    export = _export(tmp_path / "source")
    manifest = export / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data[field] = value
    manifest.write_text(json.dumps(data), encoding="utf-8")
    bundle = tmp_path / "bundle"

    with pytest.raises(ValueError):
        driver_bundle.stage_driver_exports([export], TARGET, bundle)

    assert not (bundle / "packages").exists()


@pytest.mark.parametrize("change", ["changed", "missing", "extra"])
def test_rejects_inventory_changes(
    tmp_path: Path, windows_checks: list[str], change: str
) -> None:
    export = _export(tmp_path / "source")
    file = export / "payload/native/bin/driver.sys"
    if change == "changed":
        file.write_bytes(b"badmer binary")
    elif change == "missing":
        file.unlink()
    else:
        file.with_name("extra.sys").write_bytes(b"unlisted")

    with pytest.raises(ValueError, match="inventory"):
        driver_bundle.stage_driver_exports([export], TARGET, tmp_path / "bundle")


def test_rejects_duplicate_export(tmp_path: Path, windows_checks: list[str]) -> None:
    first = _export(tmp_path / "source")
    second = _export(tmp_path / "source-copy")

    with pytest.raises(ValueError, match="Duplicate"):
        driver_bundle.stage_driver_exports([first, second], TARGET, tmp_path / "bundle")


def test_rejects_linked_payload_directory(
    tmp_path: Path, windows_checks: list[str]
) -> None:
    export = _export(tmp_path / "source")
    (export / "payload").rename(tmp_path / "elsewhere")
    _link_directory(export / "payload", tmp_path / "elsewhere")

    with pytest.raises(ValueError, match="[Uu]nsafe"):
        driver_bundle.stage_driver_exports([export], TARGET, tmp_path / "bundle")


def test_refuses_to_stage_inside_linked_destination_parent(
    tmp_path: Path, windows_checks: list[str]
) -> None:
    export = _export(tmp_path / "source")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    alias = tmp_path / "alias"
    _link_directory(alias, elsewhere)

    with pytest.raises(ValueError, match="Unsafe reparse point"):
        driver_bundle.stage_driver_exports([export], TARGET, alias / "bundle")

    assert list(elsewhere.iterdir()) == []


def test_refuses_partial_bundle_when_copy_changes_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_checks: list[str]
) -> None:
    export = _export(tmp_path / "source")
    bundle = tmp_path / "bundle"
    original_copyfile = shutil.copyfile

    def corrupt_copy(source: Path, destination: Path) -> Path:
        result = original_copyfile(source, destination)
        if destination.suffix == ".sys":
            destination.write_bytes(b"corrupted")
        return result

    monkeypatch.setattr(shutil, "copyfile", corrupt_copy)

    with pytest.raises(ValueError, match="changed during staging"):
        driver_bundle.stage_driver_exports([export], TARGET, bundle)

    assert not (bundle / "packages").exists()


@pytest.mark.parametrize("check", ["_verify_windows_matches", "_verify_inf_signature"])
def test_refuses_payload_if_windows_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_checks: list[str],
    check: str,
) -> None:
    export = _export(tmp_path / "source")
    bundle = tmp_path / "bundle"

    def fail(*_args: object) -> None:
        raise ValueError("Windows validation failed")

    monkeypatch.setattr(driver_bundle, check, fail)

    with pytest.raises(ValueError, match="Windows validation failed"):
        driver_bundle.stage_driver_exports([export], TARGET, bundle)

    assert not (bundle / "packages").exists()
