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

_flush_lock = threading.Lock()
_flush_pending: bool = False
_flush_timer: threading.Timer | None = None
_last_flush_at: float = 0.0
# Locking note: `_disk_snapshot` is read in `read_snapshot()` and reassigned
# in `delete_snapshot()` without `_flush_lock`. CPython's GIL serializes the
# single-pointer load/store, so observers see either the old or the new
# Snapshot object — never a torn read. The flush path writes `_disk_snapshot`
# under `_flush_lock` to pair with `_flush_pending`/`_flush_timer` updates,
# which need atomicity *together*.


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

    # Persist-mode flush: synchronous on priority="final", else coalesced.
    if body.get("priority") == "final":
        _flush_to_disk_synchronously(snap)
    else:
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
        body = path.read_bytes()
        payload = json.loads(body)
        if not isinstance(payload, dict):
            print(
                f"bsky-saves: warning: {path} did not parse as a JSON object; ignoring",
                file=sys.stderr,
            )
            return
        _disk_snapshot = Snapshot(payload=payload, received_at=0.0)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        print(
            f"bsky-saves: warning: failed to load {path}: {e}",
            file=sys.stderr,
        )


def _schedule_coalesced_flush() -> None:
    """Schedule a deferred flush; coalesce with any pending one.

    Guarantees at most one disk write per _FLUSH_DEBOUNCE_SECONDS in steady
    state. If a flush is already scheduled, the new push will be picked up
    by the existing timer when it fires — no second timer is started.
    """
    global _flush_pending, _flush_timer
    with _flush_lock:
        if _flush_pending:
            return  # A flush is already scheduled; it'll see the latest memory.
        _flush_pending = True
        # Always defer by the full debounce window. This gives subsequent
        # pushes a coalescing window AND naturally caps disk writes at one
        # per _FLUSH_DEBOUNCE_SECONDS. (Earlier draft used
        # `_last_flush_at + DEBOUNCE - now`, but on the first-ever push
        # `_last_flush_at == 0.0` made delay collapse to 0, so the timer
        # would fire before subsequent pushes could land in memory.)
        delay = _FLUSH_DEBOUNCE_SECONDS
        _flush_timer = threading.Timer(delay, _do_flush)
        _flush_timer.daemon = True
        _flush_timer.start()


def _do_flush() -> None:
    """Background-timer callback: write the latest persist-mode snapshot."""
    global _flush_pending, _flush_timer
    with _lock:
        snap = _memory_snapshot
        # Re-check mode: if the latest push was session-mode, don't write.
        is_session = (_memory_expires_at != 0.0)
    if snap is None or is_session:
        with _flush_lock:
            _flush_pending = False
            _flush_timer = None
        return
    _flush_to_disk_synchronously(snap)


def _flush_to_disk_synchronously(snap: Snapshot) -> None:
    """Write the snapshot's payload to disk now.

    Updates _last_flush_at and _disk_snapshot. Called from the Timer callback
    AND from the `priority: "final"` path AND from shutdown (Task 5).
    """
    global _last_flush_at, _disk_snapshot, _flush_pending, _flush_timer
    body = (json.dumps(snap.payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_status(_status_path(), body)
    with _flush_lock:
        _last_flush_at = time.monotonic()
        _disk_snapshot = snap
        _flush_pending = False
        _flush_timer = None


def _reset_for_tests() -> None:
    """Test-only: clear all module state."""
    global _memory_snapshot, _memory_expires_at
    global _disk_snapshot, _disk_loaded
    global _flush_pending, _flush_timer, _last_flush_at
    with _lock:
        _memory_snapshot = None
        _memory_expires_at = 0.0
    with _flush_lock:
        _disk_snapshot = None
        _disk_loaded = False
        _flush_pending = False
        if _flush_timer is not None:
            _flush_timer.cancel()
        _flush_timer = None
        _last_flush_at = 0.0
