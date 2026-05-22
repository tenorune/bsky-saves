"""Unit tests for src/bsky_saves/_status.py — the snapshot state machine
behind /status endpoints."""
from __future__ import annotations

import json
import sys
import time

import pytest


@pytest.fixture(autouse=True)
def reset_status_module():
    """Clear all module-level state between tests so they don't leak."""
    from bsky_saves import _status
    _status._reset_for_tests()
    yield
    _status._reset_for_tests()


def _valid_persist_payload(handle="alice.bsky.social", did="did:plc:abc"):
    return {
        "schema_version": 1,
        "updated_at": "2026-05-21T20:00:00Z",
        "current_state": "idle",
        "library": {"handle": handle, "did": did, "total_saves": 100},
        "storage": {"mode": "persist", "session_ttl_seconds": None},
    }


def _valid_session_payload(ttl_seconds=60, did="did:plc:abc"):
    return {
        "schema_version": 1,
        "updated_at": "2026-05-21T20:00:00Z",
        "current_state": "idle",
        "library": {"handle": "alice.bsky.social", "did": did, "total_saves": 100},
        "storage": {"mode": "session", "session_ttl_seconds": ttl_seconds},
    }


def test_receive_persist_push_stores_in_memory(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    # Stub the flush path so we don't actually write to disk in this test.
    monkeypatch.setattr(_status, "_schedule_coalesced_flush", lambda: None)
    payload = _valid_persist_payload()
    _status.receive_push(payload)
    assert _status.read_snapshot() == payload


def test_receive_session_push_stores_in_memory(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    payload = _valid_session_payload(ttl_seconds=60)
    _status.receive_push(payload)
    assert _status.read_snapshot() == payload


def test_read_snapshot_returns_none_when_empty():
    from bsky_saves import _status
    assert _status.read_snapshot() is None


def test_session_mode_ttl_expires_on_read(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    # Use a very short TTL so the test runs quickly.
    payload = _valid_session_payload(ttl_seconds=1)
    _status.receive_push(payload)
    assert _status.read_snapshot() == payload
    time.sleep(1.1)
    # After TTL, the snapshot is dropped on the next read.
    assert _status.read_snapshot() is None


def test_persist_mode_has_no_expiry(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_status, "_schedule_coalesced_flush", lambda: None)
    payload = _valid_persist_payload()
    _status.receive_push(payload)
    # Even after sleep, persist-mode snapshot is still visible (no TTL).
    time.sleep(0.5)
    assert _status.read_snapshot() == payload


def test_delete_snapshot_clears_memory(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_status, "_schedule_coalesced_flush", lambda: None)
    _status.receive_push(_valid_persist_payload())
    assert _status.read_snapshot() is not None
    _status.delete_snapshot()
    assert _status.read_snapshot() is None


def test_last_write_wins_under_sequential_pushes(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_status, "_schedule_coalesced_flush", lambda: None)
    p1 = _valid_persist_payload(handle="alice.bsky.social", did="did:plc:aaa")
    p2 = _valid_persist_payload(handle="bob.bsky.social", did="did:plc:bbb")
    _status.receive_push(p1)
    _status.receive_push(p2)
    assert _status.read_snapshot() == p2


def test_load_disk_on_startup_populates_disk_snapshot(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    # Pre-write a snapshot file.
    path = tmp_path / "status.json"
    payload = _valid_persist_payload()
    path.write_text(json.dumps(payload), encoding="utf-8")
    _status.load_disk_on_startup()
    assert _status.read_snapshot() == payload


def test_load_disk_on_startup_handles_missing_file(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    # No file at tmp_path/status.json.
    _status.load_disk_on_startup()
    assert _status.read_snapshot() is None


def test_load_disk_on_startup_handles_corrupt_file(monkeypatch, tmp_path, capsys):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    path = tmp_path / "status.json"
    path.write_text("not valid json {", encoding="utf-8")
    _status.load_disk_on_startup()
    assert _status.read_snapshot() is None
    captured = capsys.readouterr()
    assert "failed to load" in captured.err.lower()


def test_load_disk_on_startup_handles_non_dict_json(monkeypatch, tmp_path, capsys):
    """A JSON array (or any non-object) is rejected with a distinct warning,
    leaving _disk_snapshot empty. File is not auto-deleted."""
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    path = tmp_path / "status.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    _status.load_disk_on_startup()
    assert _status.read_snapshot() is None
    assert path.exists()  # not auto-deleted
    captured = capsys.readouterr()
    assert "did not parse as a json object" in captured.err.lower()


def test_load_disk_on_startup_handles_non_utf8_bytes(monkeypatch, tmp_path, capsys):
    """A file with invalid UTF-8 bytes is treated as a corrupt file —
    stderr warning, no crash, _disk_snapshot stays empty."""
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    path = tmp_path / "status.json"
    # Lone 0x80 byte is invalid as start of a UTF-8 sequence.
    path.write_bytes(b"\x80\x81\x82 not valid utf-8")
    _status.load_disk_on_startup()
    assert _status.read_snapshot() is None
    captured = capsys.readouterr()
    assert "failed to load" in captured.err.lower()


def test_load_disk_on_startup_is_idempotent(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    path = tmp_path / "status.json"
    payload1 = _valid_persist_payload(handle="alice.bsky.social")
    path.write_text(json.dumps(payload1), encoding="utf-8")
    _status.load_disk_on_startup()
    # Overwrite the file; second load_disk_on_startup should not pick up the change.
    payload2 = _valid_persist_payload(handle="bob.bsky.social")
    path.write_text(json.dumps(payload2), encoding="utf-8")
    _status.load_disk_on_startup()
    # Still the first payload — load is idempotent.
    assert _status.read_snapshot() == payload1


def test_delete_snapshot_removes_disk_file(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    path = tmp_path / "status.json"
    path.write_text(json.dumps(_valid_persist_payload()), encoding="utf-8")
    _status.load_disk_on_startup()
    assert path.exists()
    _status.delete_snapshot()
    assert not path.exists()


def test_delete_during_pending_flush_does_not_resurrect_file(monkeypatch, tmp_path):
    """Pending Timer should not resurrect the file after a concurrent delete.

    Verifies the cancel+join hazard fix: a delete while a flush is mid-flight
    (atomic_write_status running) must leave the disk file gone, not let the
    flush recreate it.

    To exercise the race deterministically, we monkeypatch atomic_write_status
    to block on an event. We fire _do_flush() on a background thread; while it
    is blocked inside atomic_write_status, we call delete_snapshot() on the
    main thread. delete_snapshot must wait for the in-flight flush to drain
    (join the timer/thread) and then unlink the file last, so the final
    on-disk state is "no file."
    """
    import threading as _threading
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)

    payload = _valid_persist_payload()
    _status.receive_push(payload)  # schedules a Timer (~1s out)

    # Capture the real writer so we can call it inside our blocking shim.
    real_write = _status.atomic_write_status

    write_started = _threading.Event()
    release_write = _threading.Event()

    def blocking_write(path, body):
        write_started.set()
        # Hold the flush mid-execution until the main thread releases us.
        release_write.wait(timeout=5.0)
        real_write(path, body)

    monkeypatch.setattr(_status, "atomic_write_status", blocking_write)

    # Cancel the auto-Timer and drive _do_flush ourselves via a controlled
    # Timer so we can deterministically place delete_snapshot in the race
    # window. We use a real threading.Timer (with a tiny delay) rather than a
    # plain Thread so delete_snapshot's .cancel()+.join() calls work — on an
    # already-fired Timer, cancel() is a no-op and join() drains the thread.
    with _status._flush_lock:
        t = _status._flush_timer
    if t is not None:
        t.cancel()
    flush_timer = _threading.Timer(0.0, _status._do_flush)
    flush_timer.daemon = True
    # Register the new Timer as _flush_timer so delete_snapshot drains it.
    with _status._flush_lock:
        _status._flush_timer = flush_timer
        _status._flush_pending = True
    flush_timer.start()
    # Wait until the flush is mid-write (inside atomic_write_status).
    assert write_started.wait(timeout=3.0), "flush never entered atomic_write_status"

    # Now call delete_snapshot. It must:
    #   1. cancel/clear timer state,
    #   2. wait for the in-flight flush to finish writing the file,
    #   3. unlink the file LAST.
    # Release the blocked write *after* a tiny delay, so delete_snapshot's
    # join() is what blocks until the write completes.
    def releaser():
        time.sleep(0.1)
        release_write.set()
    _threading.Thread(target=releaser, daemon=True).start()

    _status.delete_snapshot()

    # Wait for the in-flight flush to fully finish so we can observe the
    # final on-disk state. In the FIXED code, delete_snapshot itself
    # already drained the flush via join() before unlinking, so the file is
    # gone. In the BUGGY code, delete_snapshot returned before the racing
    # write completed, and the write then resurrected the file.
    flush_timer.join(timeout=3.0)
    assert not flush_timer.is_alive(), "flush thread did not finish"

    path = tmp_path / "status.json"
    assert not path.exists(), "delete was resurrected by an in-flight flush"
    assert _status.read_snapshot() is None


def test_persist_push_writes_to_disk(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    payload = _valid_persist_payload()
    _status.receive_push(payload)
    # Wait for the coalesced flush to fire (debounce window ~1s).
    time.sleep(1.3)
    path = tmp_path / "status.json"
    assert path.exists()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == payload


def test_session_push_does_not_write_to_disk(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    payload = _valid_session_payload(ttl_seconds=60)
    _status.receive_push(payload)
    time.sleep(1.3)
    path = tmp_path / "status.json"
    assert not path.exists()


def test_priority_final_flushes_synchronously(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    payload = _valid_persist_payload()
    payload["priority"] = "final"
    _status.receive_push(payload)
    # No sleep — synchronous flush means the file should exist by the time
    # receive_push returns.
    path = tmp_path / "status.json"
    assert path.exists()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == payload


def test_priority_final_cancels_pending_timer(monkeypatch, tmp_path):
    """A priority='final' push must cancel any pending background Timer
    so the spec's '<=1 disk write per second' invariant isn't violated by
    a redundant write firing ~1s after the synchronous write."""
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    # First push schedules a Timer (non-priority).
    p1 = _valid_persist_payload()
    p1["library"]["total_saves"] = 100
    _status.receive_push(p1)
    # Second push uses priority='final' — should cancel the pending Timer.
    p2 = _valid_persist_payload()
    p2["library"]["total_saves"] = 200
    p2["priority"] = "final"
    _status.receive_push(p2)
    path = tmp_path / "status.json"
    assert path.exists()
    # Read the file content after priority=final synchronous write.
    written_first = json.loads(path.read_text(encoding="utf-8"))
    assert written_first["library"]["total_saves"] == 200
    # Snapshot the file mtime, sleep past the debounce window, and re-check.
    # If the Timer wasn't cancelled, _do_flush will fire and rewrite the same
    # snapshot, bumping mtime.
    mtime_before = path.stat().st_mtime_ns
    time.sleep(1.3)
    mtime_after = path.stat().st_mtime_ns
    assert mtime_after == mtime_before, (
        f"Disk file was rewritten by a stale Timer ({mtime_before} -> {mtime_after})"
    )


def test_delete_during_priority_final_flush_does_not_resurrect_file(monkeypatch, tmp_path):
    """A delete racing a priority='final' flush must not resurrect the file.

    The priority='final' path runs the flush synchronously from the HTTP
    request thread, NOT the Timer thread, so the Task 4 cancel+join fix
    doesn't drain it. The fix: _flush_to_disk_synchronously takes
    _flush_lock across the entire write, and delete_snapshot also takes
    _flush_lock around the unlink — so the two serialize.
    """
    import threading
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)

    # Block atomic_write_status to pin the flusher mid-write.
    block_event = threading.Event()
    flush_started_event = threading.Event()
    real_write = _io.atomic_write_status

    def blocking_write(path, body):
        flush_started_event.set()
        block_event.wait(timeout=5.0)
        real_write(path, body)

    monkeypatch.setattr(_status, "atomic_write_status", blocking_write)

    payload = _valid_persist_payload()
    payload["priority"] = "final"

    def push():
        _status.receive_push(payload)

    pusher = threading.Thread(target=push, daemon=True)
    pusher.start()

    # Wait until the flush has entered atomic_write_status (so it's
    # already holding _flush_lock per the Fix 2 contract).
    assert flush_started_event.wait(timeout=5.0), "flush never started"

    # Now try to delete concurrently. delete_snapshot should block on
    # _flush_lock until the flush completes, then unlink.
    def delete():
        _status.delete_snapshot()

    deleter = threading.Thread(target=delete, daemon=True)
    deleter.start()

    # Let the flush proceed (the deleter is blocked on _flush_lock).
    block_event.set()
    pusher.join(timeout=5.0)
    deleter.join(timeout=5.0)

    path = tmp_path / "status.json"
    assert not path.exists(), "delete was resurrected by an in-flight priority=final flush"
    assert _status.read_snapshot() is None


def test_session_mode_ignores_priority_final(monkeypatch, tmp_path):
    """priority='final' must not trigger a disk write in session mode —
    the privacy contract is "session never touches disk regardless"."""
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    payload = _valid_session_payload(ttl_seconds=60)
    payload["priority"] = "final"
    _status.receive_push(payload)
    time.sleep(0.3)
    path = tmp_path / "status.json"
    assert not path.exists()


def test_coalesced_flush_bounds_writes(monkeypatch, tmp_path):
    """Many pushes in quick succession produce at most one disk write per
    1-second window."""
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    # Fire 10 pushes in rapid succession.
    for i in range(10):
        payload = _valid_persist_payload()
        payload["library"]["total_saves"] = 100 + i
        _status.receive_push(payload)
    time.sleep(1.3)
    # Disk got at most one write in that window; final state matches last push.
    path = tmp_path / "status.json"
    assert path.exists()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["library"]["total_saves"] == 109


def test_disk_file_has_0o600_perms_after_flush(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    payload = _valid_persist_payload()
    payload["priority"] = "final"
    _status.receive_push(payload)
    path = tmp_path / "status.json"
    perms = path.stat().st_mode & 0o777
    if sys.platform != "win32":
        assert perms == 0o600, oct(perms)


def test_flush_synchronously_writes_pending_in_memory(monkeypatch, tmp_path):
    """In-memory persist-mode snapshot that hasn't been flushed yet gets
    drained to disk by flush_synchronously."""
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    # Stub the timer to avoid the background flush firing during the test.
    monkeypatch.setattr(_status, "_schedule_coalesced_flush", lambda: None)
    payload = _valid_persist_payload()
    _status.receive_push(payload)
    path = tmp_path / "status.json"
    # Pre-condition: no disk write yet (timer was stubbed).
    assert not path.exists()
    _status.flush_synchronously()
    assert path.exists()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == payload


def test_flush_synchronously_no_op_when_memory_empty(monkeypatch, tmp_path):
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    _status.flush_synchronously()
    assert not (tmp_path / "status.json").exists()


def test_flush_synchronously_skips_session_mode(monkeypatch, tmp_path):
    """Session-mode in-memory snapshot should NOT be drained on shutdown."""
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    payload = _valid_session_payload(ttl_seconds=60)
    _status.receive_push(payload)
    _status.flush_synchronously()
    # No disk write; the privacy contract is preserved on shutdown.
    assert not (tmp_path / "status.json").exists()


def test_post_status_wholesale_replacement_no_field_leakage(monkeypatch, tmp_path):
    """A POST /status fully replaces the prior snapshot — no field leakage.

    Pins the §4.8 Startup-flow-contract invariant from
    bsky-saves-coordination:docs/installer-status-panel.md — "the helper
    REPLACES its on-disk and in-memory snapshot with this push payload;
    it MUST NOT attempt to preserve any portion of its prior snapshot
    during a GUI-startup push." The helper cannot distinguish a startup
    push from any other push, so the invariant applies to every push.

    Catches the regression where a future maintainer adds a field-merge
    step (e.g., preserving prior library.did because a future GUI payload
    omits it, or preserving last_activity.errors[] across pushes because
    a new push has an empty list).
    """
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)

    # Push A — every field has a distinctive value at every nesting level.
    push_a = {
        "schema_version": 1,
        "updated_at": "2026-01-01T00:00:00Z",
        "current_state": "hydrating",
        "library": {
            "handle": "alice.bsky.social",
            "did": "did:plc:aaaaaaaa",
            "total_saves": 1000,
            "by_status": {"synced": 990, "lost": 8, "unsaved": 2},
        },
        "hydration": {
            "articles": {"completed": 700, "total": 1000},
            "threads": {"completed": 400, "total": 1000},
            "images": {"completed": 850, "total": 1000},
        },
        "storage": {
            "mode": "persist",
            "session_ttl_seconds": None,
            "browser_bytes_estimate": 12345,
        },
        "last_activity": {
            "kind": "hydrate_articles",
            "started_at": "2025-12-31T23:00:00Z",
            "finished_at": "2025-12-31T23:30:00Z",
            "added": 5,
            "removed": 0,
            "errors": [{"kind": "thread_fetch_failed", "message": "network", "count": 1}],
        },
        "priority": "final",
    }
    _status.receive_push(push_a)

    # Push B — minimal payload that OMITS several keys push_a had
    # (hydration, last_activity, library.by_status, storage.browser_bytes_estimate).
    # Simulates a GUI-activation push where the GUI restored its state from
    # idb-keyval (per bsky-saves-gui:#85) but has no prior hydration or
    # activity history yet (e.g., a fresh idb after the user wiped browser
    # data — the legitimate scenario the §4.8 contract's overwrite-wins
    # rule is designed to handle correctly). A merge step that preserved
    # any of these omitted keys from push_a would be a contract violation.
    push_b = {
        "schema_version": 1,
        "updated_at": "2026-06-15T12:00:00Z",
        "current_state": "idle",
        "library": {
            "handle": "bob.bsky.social",
            "did": "did:plc:bbbbbbbb",
            "total_saves": 0,
        },
        "storage": {
            "mode": "persist",
            "session_ttl_seconds": None,
        },
        "priority": "final",
    }
    _status.receive_push(push_b)

    # In-memory: deep equality on the snapshot dict catches any field that
    # leaked from push_a (e.g., a merge step preserving push_a's
    # last_activity.errors[0] because push_b's errors is []).
    snap = _status.read_snapshot()
    assert snap == push_b, (
        f"In-memory snapshot is not exactly push_b — field leakage from push_a.\n"
        f"  got:  {snap}\n"
        f"  want: {push_b}"
    )

    # On-disk: priority='final' on push_b forced a synchronous flush, so
    # the file content equals push_b. This pins the disk half of the
    # §4.8 invariant ("REPLACES its on-disk and in-memory snapshot").
    disk_path = tmp_path / "status.json"
    assert disk_path.exists(), "persist-mode push should have produced status.json on disk"
    disk_payload = json.loads(disk_path.read_text(encoding="utf-8"))
    assert disk_payload == push_b, (
        f"On-disk snapshot is not exactly push_b — field leakage from push_a.\n"
        f"  got:  {disk_payload}\n"
        f"  want: {push_b}"
    )
