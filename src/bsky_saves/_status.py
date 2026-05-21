"""bsky-saves v0.6.7 status-snapshot state machine.

Owns the snapshot that backs the /status endpoints. State is in-memory
with a lazy TTL for session-mode pushes, mirrored to disk for persist-mode
via a coalesced background flush (added in Task 4).

Spec: docs/superpowers/specs/2026-05-21-bsky-saves-v0.6.7-status-endpoints.md
Cross-repo contract: bsky-saves-coordination:docs/installer-status-panel.md
"""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import _io
from ._io import atomic_write_status


_FLUSH_DEBOUNCE_SECONDS = 1.0


@dataclass
class Snapshot:
    """A single status snapshot.

    payload: the raw JSON dict received from POST /status.
    received_at: time.monotonic() when the helper received the push.
    """
    payload: dict
    received_at: float


_lock = threading.Lock()
_memory_snapshot: Snapshot | None = None
_memory_expires_at: float = 0.0  # monotonic; 0 == no expiry pending

_disk_loaded: bool = False
_disk_snapshot: Snapshot | None = None


def _status_path() -> Path:
    return _io.config_dir() / "status.json"


def receive_push(body: dict) -> None:
    """Apply a validated POST /status body.

    Updates the in-memory snapshot. Session mode: sets the TTL.
    Persist mode: schedules a coalesced flush (added in Task 4).
    """
    global _memory_snapshot, _memory_expires_at
    now = time.monotonic()
    storage = body.get("storage", {})
    mode = storage.get("mode")
    snap = Snapshot(payload=body, received_at=now)

    with _lock:
        _memory_snapshot = snap
        if mode == "session":
            ttl = int(storage.get("session_ttl_seconds") or 0)
            _memory_expires_at = now + ttl
            return  # No disk writes for session mode.
        # Persist mode.
        _memory_expires_at = 0.0  # No expiry in persist mode.

    # Persist-mode flush logic added in Task 4.
    _schedule_coalesced_flush()


def read_snapshot() -> dict | None:
    """Return the current visible snapshot's payload, or None for 404.

    Performs lazy session-mode TTL expiry as a side effect.
    """
    global _memory_snapshot, _memory_expires_at
    with _lock:
        if _memory_snapshot is not None and _memory_expires_at != 0.0:
            if time.monotonic() >= _memory_expires_at:
                _memory_snapshot = None
                _memory_expires_at = 0.0
        if _memory_snapshot is not None:
            return _memory_snapshot.payload
    # Fall back to disk if loaded (added in Task 3).
    if _disk_snapshot is not None:
        return _disk_snapshot.payload
    return None


def delete_snapshot() -> None:
    """Drop the in-memory snapshot and (Task 3+) the on-disk mirror.

    Idempotent.
    """
    global _memory_snapshot, _memory_expires_at, _disk_snapshot
    with _lock:
        _memory_snapshot = None
        _memory_expires_at = 0.0
    # Disk-side cleanup added in Task 3.
    _disk_snapshot = None
    path = _status_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def load_disk_on_startup() -> None:
    """Read <config_dir>/bsky-saves/status.json into _disk_snapshot if present.

    Called once by run_serve at helper startup. Idempotent — subsequent
    calls are no-ops (we don't want to clobber an in-memory snapshot that
    was pushed AFTER the helper started but BEFORE someone called this
    function a second time).

    Malformed disk file (missing, non-JSON, non-dict, etc.) is logged as a
    warning to stderr and treated as "no disk snapshot." The file is NOT
    auto-deleted — operator can inspect.
    """
    global _disk_snapshot, _disk_loaded
    if _disk_loaded:
        return
    _disk_loaded = True
    path = _status_path()
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
        if not isinstance(payload, dict):
            print(
                f"bsky-saves: warning: {path} did not parse as a JSON object; ignoring",
                file=sys.stderr,
            )
            return
        _disk_snapshot = Snapshot(payload=payload, received_at=0.0)
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"bsky-saves: warning: failed to load {path}: {e}",
            file=sys.stderr,
        )


def _schedule_coalesced_flush() -> None:
    """Placeholder; the real implementation lands in Task 4."""
    pass


def _reset_for_tests() -> None:
    """Test-only: clear all module state."""
    global _memory_snapshot, _memory_expires_at
    global _disk_snapshot, _disk_loaded
    with _lock:
        _memory_snapshot = None
        _memory_expires_at = 0.0
    _disk_snapshot = None
    _disk_loaded = False
