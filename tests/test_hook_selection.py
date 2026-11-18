import base64
import hashlib
import json
from pathlib import Path

import pytest


def test_no_selection_is_explicit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from win_offline_update.hook_selection import describe_selection, resolve_selection

    selection = resolve_selection([], None, tmp_path / "capture")

    assert selection.path is None
    assert selection.origins == ()
    assert selection.hooks == ()
    describe_selection(selection)
    assert capsys.readouterr().out.strip() == "Hooks: none."


def test_local_selection_keeps_original_bytes_and_detects_source_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from win_offline_update.hook_selection import (
        describe_selection,
        read_selection,
        resolve_selection,
    )

    original = b'# chosen locally\n[[hooks]]\nphase="pre"\nkey="Services\\\\USBXHCI"\nname="Start"\nvalue=0\nexpected=3\n'
    local = tmp_path / "My profile.toml"
    local.write_bytes(original)
    selected = resolve_selection([], local, tmp_path / "capture")
    local.write_bytes(original.replace(b"value=0", b"value=1"))

    assert selected.path is not None
    assert selected.path.read_bytes() == original
    loaded = read_selection(selected.path)
    assert loaded.origins == selected.origins
    assert loaded.hooks[0].value == 0
    assert loaded.origins[0].sha256 == hashlib.sha256(original).hexdigest()
    assert loaded.origins[0].location == str(local.resolve())
    describe_selection(loaded)
    output = capsys.readouterr().out
    assert "My profile.toml" in output
    assert "changed" in output
    assert "wuo setup --profile" in output
    assert r"Services\USBXHCI" in output
    assert "pre" in output and "DWORD" in output and "expected" in output


def test_legacy_snapshot_describes_captured_operations_without_remote_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from win_offline_update.hook_selection import describe_selection, read_selection

    legacy = tmp_path / "profile.toml"
    legacy.write_text(
        '[[hooks]]\nphase="post"\nscope="system"\nkey="Setup"\nname="Start"\nvalue=4294967295\n',
        encoding="utf-8",
    )

    selection = read_selection(legacy)
    describe_selection(selection, check_freshness=False)
    output = capsys.readouterr().out

    assert len(selection.origins) == 1
    assert "snapshot" in output.lower()
    assert "post" in output and "system" in output
    assert "4294967295" in output
    assert "compatib" not in output.lower()


def test_modified_snapshot_is_rejected_even_if_valid_toml(tmp_path: Path) -> None:
    from win_offline_update.hook_selection import read_selection, resolve_selection

    local = tmp_path / "source.toml"
    local.write_text('[[hooks]]\nphase="post"\nkey="Setup"\nname="Start"\nvalue=0\n')
    selection = resolve_selection([], local, tmp_path / "capture")
    assert selection.path is not None
    selection.path.write_bytes(
        selection.path.read_bytes().replace(b"value=0", b"value=1")
    )

    with pytest.raises(ValueError, match="(digest|SHA|hash|snapshot)"):
        read_selection(selection.path)


@pytest.mark.parametrize("field,value", [("schema", 2), ("profile_sha256", "invalid")])
def test_invalid_metadata_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    from win_offline_update.hook_selection import read_selection, resolve_selection

    local = tmp_path / "source.toml"
    local.write_text("hooks=[]\n")
    selection = resolve_selection([], local, tmp_path / "capture")
    metadata_path = tmp_path / "capture" / "hooks.json"
    metadata = json.loads(metadata_path.read_text())
    metadata[field] = value
    metadata_path.write_text(json.dumps(metadata))

    with pytest.raises(ValueError, match="(metadata|digest|SHA|hash|schema)"):
        read_selection(selection.path)


def test_origin_content_and_hash_must_match_selected_operations(tmp_path: Path) -> None:
    from win_offline_update.hook_selection import read_selection, resolve_selection

    local = tmp_path / "source.toml"
    local.write_text('[[hooks]]\nphase="post"\nkey="Setup"\nname="Start"\nvalue=0\n')
    selection = resolve_selection([], local, tmp_path / "capture")
    metadata_path = tmp_path / "capture" / "hooks.json"
    metadata = json.loads(metadata_path.read_text())
    changed = local.read_bytes().replace(b"value=0", b"value=1")
    metadata["origins"][0]["sha256"] = hashlib.sha256(changed).hexdigest()
    metadata["origins"][0]["content_base64"] = base64.b64encode(changed).decode("ascii")
    metadata_path.write_text(json.dumps(metadata))

    with pytest.raises(ValueError, match="(operations|snapshot|profile)"):
        read_selection(selection.path)


@pytest.mark.parametrize(
    "field,value",
    [("sha256", "0" * 64), ("content_base64", "@@@"), ("name", 10)],
)
def test_invalid_origin_metadata_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    from win_offline_update.hook_selection import read_selection, resolve_selection

    local = tmp_path / "source.toml"
    local.write_text("hooks=[]\n")
    selection = resolve_selection([], local, tmp_path / "capture")
    metadata_path = tmp_path / "capture" / "hooks.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["origins"][0][field] = value
    metadata_path.write_text(json.dumps(metadata))

    with pytest.raises(ValueError, match="(metadata|SHA-256)"):
        read_selection(selection.path)


def test_named_profiles_combine_with_valid_toml_and_pinned_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from win_offline_update.hook_registry import Freshness, HookOrigin, HookProfile
    from win_offline_update.hook_selection import (
        describe_selection,
        read_selection,
        resolve_selection,
    )

    first = (
        b'[[hooks]]\nphase="pre"\nkey="Services\\\\USBXHCI"\nname="Start"\nvalue=0\n'
    )
    second = b'[[hooks]]\nphase="post"\nscope="system"\nkey="Setup"\nname="Finish"\nvalue=2\n'

    class Registry:
        def get(self, name: str) -> HookProfile:
            content = {"official/usb4": first, "vendor/finish": second}[name]
            return HookProfile(
                HookOrigin(
                    name,
                    name.split("/")[0],
                    "a" * 40,
                    "hooks/source.toml",
                    hashlib.sha256(content).hexdigest(),
                ),
                content,
            )

        def check(self, origin: HookOrigin) -> Freshness:
            return Freshness("changed", "new content available")

    selected = resolve_selection(
        ["official/usb4", "vendor/finish"], None, tmp_path / "capture", Registry()
    )  # type: ignore[arg-type]
    loaded = read_selection(selected.path)

    assert [origin.name for origin in loaded.origins] == [
        "official/usb4",
        "vendor/finish",
    ]
    assert [hook.key for hook in loaded.hooks] == [r"Services\USBXHCI", "Setup"]
    assert [hook.scope for hook in loaded.hooks] == ["control-set", "system"]
    assert [hook.value for hook in loaded.hooks] == [0, 2]
    describe_selection(loaded, Registry())  # type: ignore[arg-type]
    assert (
        "wuo setup --hook official/usb4 --hook vendor/finish" in capsys.readouterr().out
    )


def test_combined_profiles_reject_case_insensitive_duplicate_targets(
    tmp_path: Path,
) -> None:
    from win_offline_update.hook_registry import HookOrigin, HookProfile
    from win_offline_update.hook_selection import resolve_selection

    class Registry:
        def get(self, name: str) -> HookProfile:
            content = b'[[hooks]]\nphase="post"\nkey="Services\\\\USBXHCI"\nname="Start"\nvalue=0\n'
            if name == "vendor/override":
                content = (
                    content.replace(b"USBXHCI", b"usbxhci")
                    .replace(b"Start", b"START")
                    .replace(b"value=0", b"value=3")
                )
            return HookProfile(
                HookOrigin(
                    name,
                    name.split("/")[0],
                    "a" * 40,
                    "hooks/test.toml",
                    hashlib.sha256(content).hexdigest(),
                ),
                content,
            )

    with pytest.raises(ValueError, match=r"official/usb4.*vendor/override"):
        resolve_selection(
            ["official/usb4", "vendor/override"], None, tmp_path / "capture", Registry()
        )  # type: ignore[arg-type]
    assert not (tmp_path / "capture" / "profile.toml").exists()


@pytest.mark.parametrize("state", ["changed", "removed", "unknown"])
@pytest.mark.parametrize("source", ["vendor", "local"])
def test_stale_named_selection_warns_with_refresh_and_reselection_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], state: str, source: str
) -> None:
    from win_offline_update.hook_registry import Freshness, HookOrigin, HookProfile
    from win_offline_update.hook_selection import describe_selection, resolve_selection

    content = b'[[hooks]]\nphase="post"\nkey="Setup"\nname="Start"\nvalue=0\n'

    class Registry:
        def get(self, name: str) -> HookProfile:
            return HookProfile(
                HookOrigin(
                    name,
                    source,
                    "b" * 40,
                    "hooks/fix.toml",
                    hashlib.sha256(content).hexdigest(),
                ),
                content,
            )

        def check(self, origin: HookOrigin) -> Freshness:
            assert origin.name == f"{source}/fix"
            return Freshness(state, "source check detail")  # type: ignore[arg-type]

    registry = Registry()
    selection = resolve_selection(
        [f"{source}/fix"], None, tmp_path / "capture", registry
    )  # type: ignore[arg-type]
    describe_selection(selection, registry)  # type: ignore[arg-type]
    output = capsys.readouterr().out

    assert state in output
    assert "b" * 40 in output
    assert hashlib.sha256(content).hexdigest() in output
    assert "source check detail" in output
    assert f"wuo hooks refresh {source}" in output
    assert f"wuo setup --hook {source}/fix" in output
    assert "compatib" not in output.lower()


def test_profile_and_named_hooks_cannot_be_selected_together(tmp_path: Path) -> None:
    from win_offline_update.hook_selection import resolve_selection

    with pytest.raises(ValueError, match="(--hook|profile|mutually)"):
        resolve_selection(
            ["official/usb4"], tmp_path / "local.toml", tmp_path / "capture"
        )
