import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from wuo.target import TargetIdentity
from wuo.update import Offer

TARGET_ID = "891b0bf2-a1b7-4c3d-8620-14db3a5542fd"


@pytest.fixture(autouse=True)
def no_host_restart_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wuo.windows_updates.current_reboot_required", lambda _: False)


def offers() -> list[Offer]:
    return [
        Offer(
            "7b5d074f-0430-4464-96ac-bdb9fab4ff56",
            "Security Intelligence KB2267602 (1.463.360.0)",
            "defender",
            revision=2,
        ),
        Offer(
            "912b0dba-70dd-41b6-bf3f-ec451138431d",
            "Windows Security platform KB5007651",
            "security-platform",
            revision=3,
        ),
        Offer(
            "e8583d93-7030-4d7e-900a-60926e665b53",
            "Microsoft Defender Antivirus platform KB4052623",
            "defender",
            revision=4,
        ),
    ]


def test_security_order_and_exact_scope() -> None:
    from wuo.windows_updates import eligible, ordered

    assert [item.id for item in ordered(offers())] == [
        offers()[2].id,
        offers()[1].id,
        offers()[0].id,
    ]
    assert all(eligible(offer) for offer in offers())
    assert not eligible(
        Offer(
            "e8583d93-7030-4d7e-900a-60926e665b53",
            "Security KB9999999",
            "monthly",
            revision=1,
        )
    )
    assert not eligible(
        Offer(
            "e8583d93-7030-4d7e-900a-60926e665b53", "KB2267602", "drivers", revision=1
        )
    )


def test_result_requires_matching_request_and_each_update_verified(
    tmp_path: Path,
) -> None:
    from wuo.windows_updates import read_result

    target = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", TARGET_ID, ""
    )
    request_id = "0acb024c-eea6-440c-93f6-47513e18c074"
    request = {
        "request_id": request_id,
        "target": {"machine_guid": TARGET_ID},
        "updates": [{"id": offers()[0].id, "revision": 2}],
    }
    journal = tmp_path / "windows-updates.json"
    record: dict[str, Any] = {
        "schema_version": 1,
        "request_id": request_id,
        "target": {"machine_guid": TARGET_ID},
        "boot_id": "one",
        "reboot_required": False,
        "updates": [
            {
                "id": offers()[0].id,
                "revision": 2,
                "title": offers()[0].title,
                "state": "installed",
                "verified": True,
                "installed_proof": True,
                "reboot_required": False,
                "result_code": 2,
                "hresult": 0,
                "error": None,
            }
        ],
        "observed": {},
    }
    journal.write_text(json.dumps(record), encoding="ascii")
    result = read_result(journal, target, request)
    assert result.complete and not result.reboot_required
    record["updates"][0]["verified"] = False
    journal.write_text(json.dumps(record), encoding="ascii")
    assert not read_result(journal, target, request).complete
    record["request_id"] = "stale"
    journal.write_text(json.dumps(record), encoding="ascii")
    with pytest.raises(ValueError, match="request"):
        read_result(journal, target, request)


def test_python_process_boundary_and_captured_hooks_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wuo import windows_updates

    identity = TargetIdentity(
        "25H2", 26200, 9974, "x64", tmp_path / "Windows", TARGET_ID, ""
    )
    monkeypatch.setattr(windows_updates, "read_target", lambda **_: identity)
    child = tmp_path / "native_result.py"
    child.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "request=json.loads(Path(sys.argv[1]).read_text())\n"
        "request.update(boot_id='one', reboot_required=False)\n"
        "for row in request['updates']:\n"
        " row.update(state='installed', verified=True, installed_proof=True, reboot_required=False, result_code=2)\n"
        "Path(sys.argv[2]).write_text(json.dumps(request))\n",
        encoding="utf-8",
    )
    original = subprocess.Popen

    def launch(
        args: list[str | Path],
        *,
        stdout: int,
        stderr: int,
        text: bool,
        errors: str,
        creationflags: int,
    ) -> subprocess.Popen[str]:
        return original(
            [sys.executable, child, args[-3], args[-1]],
            stdout=stdout,
            stderr=stderr,
            text=True,
            errors=errors,
        )

    monkeypatch.setattr(windows_updates.subprocess, "Popen", launch)
    result = windows_updates.install([offers()[0]], identity, tmp_path)
    assert result.complete
    receipt = json.loads((tmp_path / "windows-hooks.json").read_text())
    journal = json.loads(result.journal.read_text())
    assert receipt["state"] == "post-applied"
    assert receipt["job_id"] == journal["hooks_job_id"]


def test_stale_target_fails_before_hooks_or_process_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from wuo import windows_updates

    identity = TargetIdentity("25H2", 26200, 9974, "x64", tmp_path, TARGET_ID, "")
    monkeypatch.setattr(
        windows_updates, "read_target", lambda **_: replace(identity, revision=9975)
    )

    def forbidden(*_: object, **__: object) -> None:
        pytest.fail(
            "A stale Windows baseline must not apply hooks or launch an installer."
        )

    monkeypatch.setattr(windows_updates.windows_hooks, "capture", forbidden)
    monkeypatch.setattr(windows_updates.subprocess, "Popen", forbidden)
    with pytest.raises(ValueError, match="identity changed"):
        windows_updates.install([offers()[0]], identity, tmp_path)


def test_subset_cannot_replace_unresolved_native_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wuo import windows_updates

    identity = TargetIdentity("25H2", 26200, 9974, "x64", tmp_path, TARGET_ID, "")
    rows = tuple(
        {
            "id": item.id,
            "revision": item.revision,
            "verified": False,
            "state": "unavailable",
        }
        for item in offers()[:2]
    )
    prior = windows_updates.WindowsResult(
        False, False, rows, tmp_path / windows_updates.JOURNAL
    )
    prior.journal.write_text("prior journal retained", encoding="ascii")
    monkeypatch.setattr(windows_updates, "verify", lambda *_: prior)
    monkeypatch.setattr(
        windows_updates,
        "_invoke",
        lambda *_, **__: pytest.fail(
            "A subset must not replace unresolved native rows."
        ),
    )
    with pytest.raises(ValueError, match="unresolved|previous|subset"):
        windows_updates.install([offers()[0]], identity, tmp_path)
    assert prior.journal.read_text() == "prior journal retained"


def test_terminal_native_completion_does_not_rescan_retired_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wuo import windows_updates

    identity = TargetIdentity("25H2", 26200, 9974, "x64", tmp_path, TARGET_ID, "")
    record = {
        "schema_version": 1,
        "request_id": "44444444-4444-4444-4444-444444444444",
        "target": windows_updates._target(identity),
        "boot_id": "boot-one",
        "reboot_required": False,
        "phase_complete": True,
        "updates": [
            {
                "id": offers()[0].id,
                "revision": 2,
                "title": offers()[0].title,
                "kb": "KB2267602",
                "category": "defender",
                "state": "installed",
                "verified": True,
                "installed_proof": True,
                "reboot_required": False,
            }
        ],
    }
    windows_updates._record_completion(tmp_path / windows_updates.JOURNAL, record)

    def hooks(*_: object, **options: object) -> None:
        assert options.get("check_values") is False, (
            "A completed phase must not reapply its old live hook policy."
        )

    monkeypatch.setattr(windows_updates.windows_hooks, "verify", hooks)
    monkeypatch.setattr(
        windows_updates,
        "_invoke",
        lambda *_, **__: pytest.fail(
            "A durable completed transaction must not reopen retired WUA metadata."
        ),
    )
    result = windows_updates.verify(identity, tmp_path)
    assert result is not None and result.complete


def test_terminal_completion_still_blocks_a_current_global_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wuo import windows_updates

    target = TargetIdentity("25H2", 26200, 9974, "x64", tmp_path, TARGET_ID, "")
    data = {
        "schema_version": 1,
        "request_id": "44444444-4444-4444-4444-444444444444",
        "target": windows_updates._target(target),
        "boot_id": "one",
        "reboot_required": False,
        "updates": [
            {
                "id": offers()[0].id,
                "revision": 2,
                "title": offers()[0].title,
                "state": "installed",
                "verified": True,
                "installed_proof": True,
                "reboot_required": False,
            }
        ],
    }
    windows_updates._record_completion(tmp_path / windows_updates.JOURNAL, data)
    monkeypatch.setattr(windows_updates, "current_reboot_required", lambda _: True)
    monkeypatch.setattr(
        windows_updates,
        "_invoke",
        lambda *_, **__: pytest.fail("Completed metadata must stay closed."),
    )
    result = windows_updates.verify(target, tmp_path)
    assert result is not None and result.reboot_required and not result.complete
    assert (
        json.loads((tmp_path / windows_updates.JOURNAL).read_text())["phase_complete"]
        is True
    )


@pytest.mark.parametrize("boundary", ["stage", "receipt", "commit"])
def test_completion_publication_failure_keeps_nonterminal_journal_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from wuo import windows_updates

    target = TargetIdentity("25H2", 26200, 9974, "x64", tmp_path, TARGET_ID, "")
    data = {
        "schema_version": 1,
        "request_id": "44444444-4444-4444-4444-444444444444",
        "target": windows_updates._target(target),
        "boot_id": "one",
        "reboot_required": False,
        "updates": [
            {
                "id": offers()[0].id,
                "revision": 2,
                "title": offers()[0].title,
                "state": "installed",
                "verified": True,
                "installed_proof": True,
                "reboot_required": False,
            }
        ],
    }
    journal = tmp_path / windows_updates.JOURNAL
    original = json.dumps(data).encode("utf-8")
    journal.write_bytes(original)
    atomic = windows_updates.atomic_json
    replace = Path.replace

    def write(path: Path, value: object) -> None:
        if (boundary == "stage" and path.name.endswith(".terminal")) or (
            boundary == "receipt" and path.parent.name == "windows-completions"
        ):
            raise OSError("injected publication failure")
        atomic(path, value)

    def commit(path: Path, destination: Path | str) -> Path:
        if boundary == "commit" and Path(destination) == journal:
            receipt = tmp_path / "windows-completions" / (data["request_id"] + ".json")
            assert receipt.is_file(), (
                "The receipt must exist before terminal journal publication."
            )
            raise OSError("injected commit failure")
        return replace(path, destination)

    monkeypatch.setattr(windows_updates, "atomic_json", write)
    monkeypatch.setattr(Path, "replace", commit)
    with pytest.raises(OSError, match="injected"):
        windows_updates._record_completion(journal, data)
    assert journal.read_bytes() == original
    sentinel = windows_updates.WindowsResult(True, False, (), journal)
    monkeypatch.setattr(windows_updates, "_invoke", lambda *_, **__: sentinel)
    assert windows_updates.verify(target, tmp_path) is sentinel


@pytest.mark.parametrize("future_changed", [False, True])
@pytest.mark.parametrize("include_new", [False, True])
def test_unfinished_retry_reuses_original_one_run_hook_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    future_changed: bool,
    include_new: bool,
) -> None:
    from wuo import windows_hooks, windows_updates
    from wuo.hook_selection import resolve_selection

    target = TargetIdentity("25H2", 26200, 9974, "x64", tmp_path, TARGET_ID, "")
    selected = resolve_selection(("official/usb4",), None, tmp_path / "one-run")
    monkeypatch.setattr(windows_hooks, "_backup", lambda *_: None)
    monkeypatch.setattr(windows_hooks, "apply", lambda *_: None)
    monkeypatch.setattr(windows_hooks, "_read", lambda *_: 0)
    monkeypatch.setattr(windows_hooks, "_path", lambda hook: hook.key)
    job_id = "55555555-5555-5555-5555-555555555555"
    with windows_hooks.capture(
        selected.path, tmp_path, TARGET_ID, target.windows, job_id=job_id
    ):
        pass
    item = offers()[0]
    row = {
        "id": item.id,
        "revision": item.revision,
        "kb": "KB2267602",
        "title": item.title,
        "category": item.category,
        "verified": False,
        "state": "failed",
    }
    journal = tmp_path / windows_updates.JOURNAL
    journal.write_text(json.dumps({"hooks_job_id": job_id, "updates": [row]}))
    previous = windows_updates.WindowsResult(False, False, (row,), journal)
    monkeypatch.setattr(windows_updates, "verify", lambda *_: previous)
    future = tmp_path / "changed.toml" if future_changed else None
    if future:
        future.write_text("hooks = []\n")

    def install(*_: object, **options: object) -> windows_updates.WindowsResult:
        assert _[0]["updates"] == [
            {key: row[key] for key in ("id", "revision", "kb", "title", "category")}
        ]
        assert (
            options["profile"] == tmp_path / "windows-hook-jobs" / job_id / "hooks.toml"
        )
        assert (
            Path(options["profile"]).with_name("hooks.json").read_bytes()
            == (tmp_path / "one-run/hooks.json").read_bytes()
        )
        return windows_updates.WindowsResult(True, False, (), journal)

    monkeypatch.setattr(windows_updates, "_invoke", install)
    requested = [item, offers()[1]] if include_new else [item]
    assert windows_updates.install(
        requested, target, tmp_path, profile=future
    ).complete is (not include_new)


def test_incomplete_native_retry_rejects_explicit_hook_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wuo import windows_updates

    target = TargetIdentity("25H2", 26200, 9974, "x64", tmp_path, TARGET_ID, "")
    previous = windows_updates.WindowsResult(
        False,
        False,
        ({"id": offers()[0].id, "revision": offers()[0].revision, "verified": False},),
        tmp_path / windows_updates.JOURNAL,
    )
    monkeypatch.setattr(windows_updates, "verify", lambda *_: previous)
    monkeypatch.setattr(
        windows_updates,
        "_invoke",
        lambda *_, **__: pytest.fail(
            "Explicit --no-hooks must not discard an unfinished capture."
        ),
    )
    with pytest.raises(ValueError, match="captured hooks"):
        windows_updates.install(
            [offers()[0]], target, tmp_path, profile=None, hooks_explicit=True
        )
