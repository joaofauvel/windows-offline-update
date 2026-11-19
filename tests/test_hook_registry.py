from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest


def _git(repo: Path, *args: str, input: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        input=input,
        capture_output=True,
    )
    return result.stdout.decode().strip()


def _profile(value: int = 0) -> bytes:
    return (
        '[[hooks]]\nphase="post"\nkey="Services\\\\Example"\n'
        f'name="Start"\nvalue={value}\n'
    ).encode()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Hook Tests")
    _git(repo, "config", "user.email", "hooks@example.invalid")
    (repo / "hooks").mkdir()
    (repo / "hooks" / "example.toml").write_bytes(_profile())
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial hooks")
    return repo


def _commit(repo: Path, message: str = "Change hooks") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_bundled_official_hooks_are_available_without_registry(tmp_path: Path) -> None:
    from wuo.hook_registry import HookRegistry

    registry = HookRegistry(tmp_path / "registry")
    official = registry.get("official/usb4")

    assert official.origin.name == "official/usb4"
    assert official.origin.source == "official"
    assert official.origin.location == ("https://github.com/joaofauvel/wuo.git")
    assert official.origin.sha256 == hashlib.sha256(official.content).hexdigest()
    assert official.origin.revision
    assert [item.origin.name for item in registry.list_profiles()] == ["official/usb4"]
    assert not (tmp_path / "registry").exists()


def test_add_pins_commit_and_get_does_not_follow_upstream(
    repository: Path, tmp_path: Path
) -> None:
    from wuo.hook_registry import HookRegistry

    registry = HookRegistry(tmp_path / "registry")
    revision = _git(repository, "rev-parse", "HEAD")
    profiles = registry.add(str(repository), "vendor")
    assert [(p.origin.name, p.origin.revision, p.content) for p in profiles] == [
        ("vendor/example", revision, _profile())
    ]

    (repository / "hooks" / "example.toml").write_bytes(_profile(4))
    _commit(repository)

    reloaded = HookRegistry(tmp_path / "registry")
    assert reloaded.get("vendor/example").content == _profile()
    assert reloaded.get("vendor/example").origin.revision == revision
    assert {p.origin.name for p in reloaded.list_profiles()} == {
        "official/usb4",
        "vendor/example",
    }


def test_refresh_publishes_new_profiles_without_mutating_captured_profile(
    repository: Path, tmp_path: Path
) -> None:
    from wuo.hook_registry import HookRegistry

    registry = HookRegistry(tmp_path / "registry")
    selected = registry.add(str(repository), "vendor")[0]
    (repository / "hooks" / "example.toml").write_bytes(_profile(4))
    new_revision = _commit(repository)

    refreshed = registry.refresh("vendor")

    assert refreshed[0].origin.revision == new_revision
    assert registry.get("vendor/example").content == _profile(4)
    assert selected.content == _profile()


def test_freshness_compares_bytes_not_commit(repository: Path, tmp_path: Path) -> None:
    from wuo.hook_registry import HookRegistry

    registry = HookRegistry(tmp_path / "registry")
    selected = registry.add(str(repository), "vendor")[0]
    _commit(repository, "Unrelated commit")

    assert registry.check(selected.origin).state == "current"

    (repository / "hooks" / "example.toml").write_bytes(_profile(4))
    _commit(repository)
    assert HookRegistry(tmp_path / "registry").check(selected.origin).state == "changed"


def test_freshness_reports_removed_profile(repository: Path, tmp_path: Path) -> None:
    from wuo.hook_registry import HookRegistry

    registry = HookRegistry(tmp_path / "registry")
    selected = registry.add(str(repository), "vendor")[0]
    (repository / "hooks" / "example.toml").unlink()
    _commit(repository)

    assert registry.check(selected.origin).state == "removed"
    assert registry.get("vendor/example").content == _profile()


def test_failed_refresh_keeps_last_valid_snapshot(
    repository: Path, tmp_path: Path
) -> None:
    from wuo.hook_registry import HookRegistry

    registry = HookRegistry(tmp_path / "registry")
    selected = registry.add(str(repository), "vendor")[0]
    (repository / "hooks" / "example.toml").write_text("not valid toml!")
    _commit(repository)

    with pytest.raises(ValueError):
        registry.refresh("vendor")

    assert registry.get("vendor/example") == selected


def test_missing_upstream_is_unknown_and_keeps_snapshot(
    repository: Path, tmp_path: Path
) -> None:
    from wuo.hook_registry import HookRegistry

    registry = HookRegistry(tmp_path / "registry")
    selected = registry.add(str(repository), "vendor")[0]
    repository.rename(tmp_path / "moved-upstream")

    assert registry.check(selected.origin).state == "unknown"
    with pytest.raises(ValueError):
        registry.refresh("vendor")
    assert registry.get("vendor/example") == selected


def test_explicit_tag_stays_pinned_after_default_branch_advances(
    repository: Path, tmp_path: Path
) -> None:
    from wuo.hook_registry import HookRegistry

    _git(repository, "tag", "v1")
    registry = HookRegistry(tmp_path / "registry")
    selected = registry.add(str(repository), "vendor", ref="v1")[0]
    (repository / "hooks" / "example.toml").write_bytes(_profile(4))
    _commit(repository)

    assert registry.check(selected.origin).state == "current"
    assert registry.refresh("vendor")[0] == selected


def test_git_symlink_blob_is_rejected(repository: Path, tmp_path: Path) -> None:
    from wuo.hook_registry import HookRegistry

    blob = _git(repository, "hash-object", "-w", "--stdin", input=b"example.toml")
    _git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"120000,{blob},hooks/link.toml",
    )
    _git(repository, "commit", "-m", "Symlink hook")

    with pytest.raises(ValueError, match="regular"):
        HookRegistry(tmp_path / "registry").add(str(repository), "vendor")


@pytest.mark.parametrize("name", ["official", "../outside", "Vendor", "", "a/b"])
def test_unsafe_or_reserved_source_names_are_rejected(
    repository: Path, tmp_path: Path, name: str
) -> None:
    from wuo.hook_registry import HookRegistry

    with pytest.raises(ValueError, match="name|reserved"):
        HookRegistry(tmp_path / "registry").add(str(repository), name)


@pytest.mark.parametrize(
    "repo",
    [
        "ext::touch bad",
        "http://example.invalid/a",
        "git://example.invalid/a",
        "-x",
        "ftp://example.invalid/a",
    ],
)
def test_unsupported_transports_are_rejected_before_git(
    tmp_path: Path, repo: str
) -> None:
    from wuo.hook_registry import HookRegistry

    with pytest.raises(ValueError, match="repository|transport"):
        HookRegistry(tmp_path / "registry").add(repo, "vendor")


def test_cached_content_tampering_is_rejected(repository: Path, tmp_path: Path) -> None:
    from wuo.hook_registry import HookRegistry

    root = tmp_path / "registry"
    registry = HookRegistry(root)
    registry.add(str(repository), "vendor")
    cached = next(root.rglob("example.toml"))
    cached.write_bytes(_profile(4))

    with pytest.raises(ValueError, match="hash|digest|changed"):
        registry.get("vendor/example")


def test_registry_snapshot_path_traversal_is_rejected(
    repository: Path, tmp_path: Path
) -> None:
    from wuo.hook_registry import HookRegistry

    root = tmp_path / "registry"
    registry = HookRegistry(root)
    registry.add(str(repository), "vendor")
    manifest = root / "registry.json"
    data = json.loads(manifest.read_text())
    data["sources"]["vendor"]["snapshot"] = "../../outside"
    manifest.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="snapshot|unsafe"):
        registry.get("vendor/example")


def test_remove_hides_source_and_reports_saved_selection_removed(
    repository: Path, tmp_path: Path
) -> None:
    from wuo.hook_registry import HookRegistry

    registry = HookRegistry(tmp_path / "registry")
    selected = registry.add(str(repository), "vendor")[0]

    registry.remove("vendor")

    assert registry.check(selected.origin).state == "removed"
    assert selected.content == _profile()
    with pytest.raises(ValueError, match="Unknown|unknown|available"):
        registry.get("vendor/example")


def test_freshness_fetch_is_shared_across_profiles_for_one_registry(
    repository: Path, tmp_path: Path
) -> None:
    from wuo.hook_registry import HookRegistry

    (repository / "hooks" / "second.toml").write_bytes(_profile())
    _commit(repository)
    registry = HookRegistry(tmp_path / "registry")
    first, second = registry.add(str(repository), "vendor")
    assert registry.check(first.origin).state == "current"
    (repository / "hooks" / "second.toml").write_bytes(_profile(4))
    _commit(repository)

    assert registry.check(second.origin).state == "current"
    assert HookRegistry(tmp_path / "registry").check(second.origin).state == "changed"


def test_refresh_invalidates_freshness_cache(repository: Path, tmp_path: Path) -> None:
    from wuo.hook_registry import HookRegistry

    registry = HookRegistry(tmp_path / "registry")
    selected = registry.add(str(repository), "vendor")[0]
    assert registry.check(selected.origin).state == "current"
    (repository / "hooks" / "example.toml").write_bytes(_profile(4))
    _commit(repository)

    registry.refresh("vendor")

    assert registry.check(selected.origin).state == "changed"


def test_refspec_injection_and_invalid_names_are_rejected(
    repository: Path, tmp_path: Path
) -> None:
    from wuo.hook_registry import HookRegistry

    registry = HookRegistry(tmp_path / "registry")
    for ref in [
        "--upload-pack=bad",
        "HEAD:refs/heads/bad",
        "+main",
        "../HEAD",
        "main~1",
        "refs/heads/*",
    ]:
        with pytest.raises(ValueError):
            registry.add(str(repository), "vendor", ref)


def test_large_profile_is_rejected_without_publishing(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wuo.hook_registry as module

    monkeypatch.setattr(module, "MAX_PROFILE_BYTES", len(_profile()) - 1)
    root = tmp_path / "registry"

    with pytest.raises(ValueError, match="size limit"):
        module.HookRegistry(root).add(str(repository), "vendor")

    assert not (root / "registry.json").exists()


def test_timeout_leaves_current_snapshot_available(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wuo.hook_registry as module

    registry = module.HookRegistry(tmp_path / "registry")
    selected = registry.add(str(repository), "vendor")[0]
    monkeypatch.setattr(module, "GIT_TIMEOUT", 0.0)

    with pytest.raises(ValueError, match="timed out"):
        registry.refresh("vendor")

    assert registry.get("vendor/example") == selected
    assert registry.check(selected.origin).state == "unknown"


def test_git_submodule_named_as_profile_is_rejected(
    repository: Path, tmp_path: Path
) -> None:
    from wuo.hook_registry import HookRegistry

    commit = _git(repository, "rev-parse", "HEAD")
    _git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{commit},hooks/module.toml",
    )
    _git(repository, "commit", "-m", "Submodule profile")

    with pytest.raises(ValueError, match="regular"):
        HookRegistry(tmp_path / "registry").add(str(repository), "vendor")


def test_only_root_hook_profiles_are_discovered(
    repository: Path, tmp_path: Path
) -> None:
    from wuo.hook_registry import HookRegistry

    nested = repository / "hooks" / "nested"
    nested.mkdir()
    (nested / "ignored.toml").write_text("arbitrary non-profile data")
    (repository / "elsewhere.toml").write_text("also ignored")
    _commit(repository)

    profiles = HookRegistry(tmp_path / "registry").add(str(repository), "vendor")

    assert [profile.origin.name for profile in profiles] == ["vendor/example"]


def test_cached_snapshot_junction_is_rejected(repository: Path, tmp_path: Path) -> None:
    from wuo.hook_registry import HookRegistry

    root = tmp_path / "registry"
    registry = HookRegistry(root)
    registry.add(str(repository), "vendor")
    original = next(root.rglob("example.toml")).parent
    redirected = tmp_path / "redirected"
    original.rename(redirected)
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(original), str(redirected)],
            check=True,
            capture_output=True,
        )
    else:
        original.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(ValueError, match="link|reparse"):
        registry.get("vendor/example")


def test_refresh_all_is_atomic_when_one_source_is_invalid(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wuo.hook_registry as module

    monkeypatch.setattr(module, "OFFICIAL_REPOSITORY", str(repository))
    broken_repo = tmp_path / "broken"
    _git(tmp_path, "clone", str(repository), str(broken_repo))
    _git(broken_repo, "config", "user.name", "Hook Tests")
    _git(broken_repo, "config", "user.email", "hooks@example.invalid")
    registry = module.HookRegistry(tmp_path / "registry")
    first = registry.add(str(repository), "first")[0]
    second = registry.add(str(broken_repo), "second")[0]
    (repository / "hooks" / "example.toml").write_bytes(_profile(4))
    _commit(repository)
    (broken_repo / "hooks" / "example.toml").write_text("invalid toml!")
    _commit(broken_repo)

    with pytest.raises(ValueError):
        registry.refresh()

    assert registry.get("first/example") == first
    assert registry.get("second/example") == second
    assert registry.get("official/usb4").origin.revision


def test_explicit_official_refresh_publishes_fixed_upstream_overlay(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wuo.hook_registry as module

    monkeypatch.setattr(module, "OFFICIAL_REPOSITORY", str(repository))
    registry = module.HookRegistry(tmp_path / "registry")
    bundled = registry.get("official/usb4")
    revision = _git(repository, "rev-parse", "HEAD")

    refreshed = registry.refresh("official")

    assert [(p.origin.name, p.origin.revision) for p in refreshed] == [
        ("official/example", revision)
    ]
    assert registry.get("official/example").content == _profile()
    assert [p.origin.name for p in registry.list_profiles()] == ["official/example"]
    assert bundled.origin.name == "official/usb4"
    assert bundled.content
    assert (
        module.HookRegistry(tmp_path / "registry").get("official/example")
        == refreshed[0]
    )


def test_refresh_all_includes_official_without_a_registered_source(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wuo.hook_registry as module

    monkeypatch.setattr(module, "OFFICIAL_REPOSITORY", str(repository))
    registry = module.HookRegistry(tmp_path / "registry")

    refreshed = registry.refresh()

    assert [p.origin.name for p in refreshed] == ["official/example"]


def test_official_overlay_cannot_redirect_the_fixed_repository(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wuo.hook_registry as module

    monkeypatch.setattr(module, "OFFICIAL_REPOSITORY", str(repository))
    root = tmp_path / "registry"
    registry = module.HookRegistry(root)
    registry.refresh("official")
    manifest = root / "registry.json"
    data = json.loads(manifest.read_text())
    data["sources"]["official"]["repo"] = "https://example.invalid/evil"
    manifest.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="official|reserved"):
        registry.get("official/example")


def test_official_profile_cached_before_repo_rename_remains_usable(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wuo.hook_registry as module

    root = tmp_path / "registry"
    registry = module.HookRegistry(root)
    monkeypatch.setattr(module, "OFFICIAL_REPOSITORY", str(repository))
    original = registry.refresh("official")[0]
    manifest = root / "registry.json"
    data = json.loads(manifest.read_text())
    legacy = "https://github.com/joaofauvel/windows-offline-update.git"
    data["sources"]["official"]["repo"] = legacy
    manifest.write_text(json.dumps(data))
    monkeypatch.setattr(
        module, "OFFICIAL_REPOSITORY", "https://github.com/joaofauvel/wuo.git"
    )

    cached = registry.get("official/example")
    assert cached.origin.location == legacy
    assert cached.origin.revision == original.origin.revision
    assert cached.content == original.content


def test_refresh_can_publish_profile_removal_without_reusing_bundled_data(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wuo.hook_registry as module

    monkeypatch.setattr(module, "OFFICIAL_REPOSITORY", str(repository))
    registry = module.HookRegistry(tmp_path / "registry")
    selected = registry.refresh("official")[0]
    (repository / "hooks" / "example.toml").unlink()
    _commit(repository)

    assert registry.refresh("official") == []
    assert registry.list_profiles() == []
    assert registry.check(selected.origin).state == "removed"
    assert selected.content == _profile()


def test_registry_root_under_junction_is_rejected(
    repository: Path, tmp_path: Path
) -> None:
    from wuo.hook_registry import HookRegistry

    parent = tmp_path / "real"
    root = parent / "registry"
    registry = HookRegistry(root)
    registry.add(str(repository), "vendor")
    moved = tmp_path / "moved"
    parent.rename(moved)
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(parent), str(moved)],
            check=True,
            capture_output=True,
        )
    else:
        parent.symlink_to(moved, target_is_directory=True)

    with pytest.raises(ValueError, match="link|reparse"):
        registry.get("vendor/example")


def test_git_timeout_terminates_owned_helper_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wuo.hook_registry as module

    helper = tmp_path / "fake_git.py"
    helper.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1])\n"
        "if len(sys.argv) > 2:\n"
        "    (root / 'child-started').write_text('ready')\n"
        "    time.sleep(3)\n"
        "    (root / 'child-survived').write_text('bug')\n"
        "else:\n"
        "    subprocess.Popen([sys.executable, __file__, str(root), 'child'])\n"
        "    time.sleep(60)\n"
    )
    real_popen = subprocess.Popen

    def spawn(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        if command[0] == "git":
            command = [sys.executable, str(helper), str(tmp_path)]
        return real_popen(command, **kwargs)

    monkeypatch.setattr(module.subprocess, "Popen", spawn)

    with pytest.raises(ValueError, match="timed out"):
        module._run_git(tmp_path, ["fetch"], time.monotonic() + 2)

    assert (tmp_path / "child-started").exists()
    time.sleep(1.5)
    assert not (tmp_path / "child-survived").exists()


def test_concurrent_registry_mutations_fail_busy_without_losing_sources(
    repository: Path, tmp_path: Path
) -> None:
    from wuo.hook_registry import HookRegistry

    root = tmp_path / "registry"
    registry = HookRegistry(root)
    selected = registry.add(str(repository), "vendor")[0]
    helper = tmp_path / "hold_lock.py"
    helper.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1])\n"
        "with (root / '.registry.lock').open('a+b') as lock:\n"
        "    lock.write(b'0'); lock.flush(); lock.seek(0)\n"
        "    if os.name == 'nt':\n"
        "        import msvcrt\n"
        "        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)\n"
        "    else:\n"
        "        import fcntl\n"
        "        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    (root / 'lock-ready').write_text('ready')\n"
        "    sys.stdin.buffer.read()\n"
    )
    process = subprocess.Popen(
        [sys.executable, str(helper), str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not (root / "lock-ready").exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.01)
        assert (root / "lock-ready").exists()

        with pytest.raises(ValueError, match="busy"):
            registry.add(str(repository), "second")
        with pytest.raises(ValueError, match="busy"):
            registry.remove("vendor")
        with pytest.raises(ValueError, match="busy"):
            registry.refresh("vendor")
        assert registry.get("vendor/example") == selected
    finally:
        # Abrupt process exit must release the lock without stale-lock cleanup.
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)

    registry.add(str(repository), "second")
    registry.remove("vendor")
    assert registry.get("second/example").content == _profile()
