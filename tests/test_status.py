"""Unit tests for src/bsky_saves/_status.py — the snapshot state machine
behind /status endpoints."""
from __future__ import annotations

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
