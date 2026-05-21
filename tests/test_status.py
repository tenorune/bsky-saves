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
