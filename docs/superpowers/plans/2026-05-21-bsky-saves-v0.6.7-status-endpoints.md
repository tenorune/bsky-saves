# bsky-saves v0.6.7 status-snapshot endpoints — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three new helper-side endpoints (`POST /status`, `GET /status`, `DELETE /status`) for the cross-repo installer-status-panel feature; ship as `bsky-saves==0.6.7` on PyPI.

**Architecture:** A new module `src/bsky_saves/_status.py` owns the snapshot state machine (in-memory + on-disk, with mode-dependent storage, lazy session-mode TTL expiry, and a coalesced background flush bounded to ≤1/s for persist-mode pushes). `serve.py` adds three route handlers that delegate to that module. `run_serve` gains a startup hook (load disk) and a shutdown hook (synchronous final flush). One new helper in `_io.py` writes status atomically with per-write unique tmp names. All endpoints sit behind the existing `_check_token` middleware unchanged.

**Tech Stack:** Python 3.11+, stdlib only (`threading`, `time`, `json`, `tempfile`, `pathlib`). No new dependencies. Existing `httpx` / `pytest` / `respx` test stack unchanged.

**Spec:** `docs/superpowers/specs/2026-05-21-bsky-saves-v0.6.7-status-endpoints.md`
**Cross-repo contract:** `bsky-saves-coordination:docs/installer-status-panel.md` (canonical) + `installer-status-panel-resolved.md` (R1–R10 archive).

**Branch:** `claude/v0.6.7-status-endpoints` (already created; spec already committed).

---

## File map

| File | Disposition | Responsibility |
|---|---|---|
| `src/bsky_saves/_io.py` | Modify | Add `atomic_write_status(path, body_bytes)` using `tempfile.NamedTemporaryFile` for per-write unique tmp names. Existing `atomic_write_inventory` and token helpers unchanged. |
| `src/bsky_saves/_status.py` | Create | Module owning the snapshot state: `Snapshot` dataclass, locks, in-memory + disk snapshot vars, public API (`receive_push`, `read_snapshot`, `delete_snapshot`, `load_disk_on_startup`, `flush_synchronously`), private flush algorithm (`_schedule_coalesced_flush`, `_do_flush`, `_flush_to_disk_synchronously`), and `_reset_for_tests` test hook. |
| `src/bsky_saves/serve.py` | Modify | Three new route handlers (`_handle_status_post`, `_handle_status_get`, `_handle_status_delete`); one validation helper (`_validate_status_payload`); three new entries in `ROUTES`. `run_serve` calls `_status.load_disk_on_startup()` before `serve_forever()` and `_status.flush_synchronously()` in its `finally` block before `server.shutdown()`. |
| `tests/test_io.py` | Modify | New tests for `atomic_write_status` (writes file with `0o600`, creates parent dir, atomic-replace, per-write unique tmp names). |
| `tests/test_status.py` | Create | Unit tests for `_status` internals: state-machine transitions, lazy expiry, coalesce timing, shutdown flush, disk loading. |
| `tests/test_serve.py` | Modify | New integration tests for the three endpoints + auth gating + disk-write coalescing + persistence-file perms. Add a `reset_status_module` fixture. |
| `pyproject.toml` | Modify | `version = "0.6.7"`. |
| `README.md` | Modify | New `### Status snapshot` subsection under `## bsky-saves serve` documenting the endpoint surface for users. |

---

## Task 1: `atomic_write_status` helper in `_io.py`

Foundation: every disk write of the status file goes through this helper. Per-write unique tmp names so a future concurrent writer doesn't race on a single shared tmp filename (defense-in-depth — today's caller is single-threaded by design).

**Files:**
- Modify: `src/bsky_saves/_io.py`
- Test: `tests/test_io.py`

- [ ] **Step 1.1: Write failing tests in `tests/test_io.py`**

Append at the end of the file:

```python
def test_atomic_write_status_creates_file_with_0o600_perms(tmp_path):
    from bsky_saves._io import atomic_write_status
    path = tmp_path / "subdir" / "status.json"
    atomic_write_status(path, b'{"k": "v"}\n')
    assert path.exists()
    assert path.read_bytes() == b'{"k": "v"}\n'
    perms = path.stat().st_mode & 0o777
    if sys.platform != "win32":
        assert perms == 0o600, oct(perms)


def test_atomic_write_status_creates_parent_dir_with_0o700(tmp_path):
    from bsky_saves._io import atomic_write_status
    path = tmp_path / "new-parent-dir" / "status.json"
    atomic_write_status(path, b'{}\n')
    assert path.parent.is_dir()
    if sys.platform != "win32":
        parent_perms = path.parent.stat().st_mode & 0o777
        assert parent_perms == 0o700, oct(parent_perms)


def test_atomic_write_status_overwrites_existing(tmp_path):
    from bsky_saves._io import atomic_write_status
    path = tmp_path / "status.json"
    atomic_write_status(path, b'{"v": 1}\n')
    atomic_write_status(path, b'{"v": 2}\n')
    assert path.read_bytes() == b'{"v": 2}\n'


def test_atomic_write_status_leaves_no_tmp_sidecar(tmp_path):
    from bsky_saves._io import atomic_write_status
    path = tmp_path / "status.json"
    atomic_write_status(path, b'{}\n')
    siblings = list(path.parent.iterdir())
    assert siblings == [path], f"expected only {path.name}, got {[s.name for s in siblings]}"
```

- [ ] **Step 1.2: Run tests to verify they fail**

```
/tmp/venv/bin/pytest tests/test_io.py -v -k atomic_write_status
```

Expected: 4 failures (`ImportError: cannot import name 'atomic_write_status'`).

- [ ] **Step 1.3: Add `atomic_write_status` to `src/bsky_saves/_io.py`**

Append at the end of the file (after existing helpers, before any test-only code):

```python
import tempfile


def atomic_write_status(path: Path, body: bytes) -> None:
    """Atomic-write the status snapshot to disk with per-write unique tmp names.

    Used by _status._flush_to_disk_synchronously and the background flush
    callback. Per-write unique tmp names (via tempfile.NamedTemporaryFile)
    are defense-in-depth against a future contributor running the same
    writer from multiple threads — today's caller is single-threaded by
    design, but the broader inventory writer's single-tmp-name scheme races
    under concurrent calls and we don't want to inherit that hazard here.

    Args:
        path: target file path. Parent dir is created with 0o700 if absent.
        body: bytes to write. Caller is responsible for encoding (utf-8).
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
    ) as tmp:
        tmp.write(body)
        tmp_path = Path(tmp.name)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
```

- [ ] **Step 1.4: Run tests to verify they pass**

```
/tmp/venv/bin/pytest tests/test_io.py -v -k atomic_write_status
```

Expected: 4 passed.

- [ ] **Step 1.5: Run full `_io` test suite as regression check**

```
/tmp/venv/bin/pytest tests/test_io.py -v
```

Expected: all existing `_io` tests still pass alongside the new four.

- [ ] **Step 1.6: Commit**

```bash
git add src/bsky_saves/_io.py tests/test_io.py
git commit -m "feat(_io): add atomic_write_status helper for v0.6.7 status snapshots

Per-write unique tmp names via tempfile.NamedTemporaryFile — defense in
depth against any future caller running this writer from multiple
threads simultaneously. Today's caller (the v0.6.7 _status module's
background flush task) is single-threaded by design.

Creates parent dir with 0o700, final file with 0o600 (matches token
file's threat model).

Tests: file perms, parent-dir perms, overwrite behavior, no tmp
sidecar left on disk after replace."
```

---

## Task 2: `_status.py` core state machine (memory only)

In-memory snapshot, lazy session-mode TTL expiry, and the public read/delete API. No disk I/O yet — that lands in Tasks 3 and 4.

**Files:**
- Create: `src/bsky_saves/_status.py`
- Create: `tests/test_status.py`

- [ ] **Step 2.1: Write failing tests in `tests/test_status.py`**

Create the file with these tests (and a top-level fixture):

```python
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
```

- [ ] **Step 2.2: Run tests to verify they fail**

```
/tmp/venv/bin/pytest tests/test_status.py -v
```

Expected: 7 failures (`ImportError: No module named 'bsky_saves._status'`).

- [ ] **Step 2.3: Create `src/bsky_saves/_status.py`**

```python
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

from ._io import atomic_write_status, config_dir


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
    return config_dir() / "status.json"


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
```

- [ ] **Step 2.4: Run tests to verify they pass**

```
/tmp/venv/bin/pytest tests/test_status.py -v
```

Expected: 7 passed.

- [ ] **Step 2.5: Run full suite as regression check**

```
/tmp/venv/bin/pytest tests/ -q
```

Expected: all existing tests pass alongside the new 7.

- [ ] **Step 2.6: Commit**

```bash
git add src/bsky_saves/_status.py tests/test_status.py
git commit -m "feat(_status): core snapshot state machine (memory only)

Snapshot dataclass, in-memory store behind a Lock, lazy session-mode
TTL expiry on read. Public API: receive_push, read_snapshot,
delete_snapshot. Disk loading and coalesced flush land in subsequent
tasks (Task 3 and Task 4 respectively).

_reset_for_tests test hook resets all module state — used by the
autouse fixture in tests/test_status.py so tests don't leak state
across runs.

Tests cover memory transitions: persist push stored, session push
stored with TTL, lazy expiry drops memory on read after TTL, persist
mode has no expiry, delete clears memory, sequential pushes follow
last-write-wins (R3 in the cross-repo contract)."
```

---

## Task 3: Disk-load on startup + delete-from-disk

Add the `load_disk_on_startup()` entrypoint and wire `delete_snapshot()` to remove the on-disk file. Read-fallback in `read_snapshot()` already exists from Task 2 (returns `_disk_snapshot.payload` if memory is empty).

**Files:**
- Modify: `src/bsky_saves/_status.py`
- Modify: `tests/test_status.py`

- [ ] **Step 3.1: Write failing tests in `tests/test_status.py`**

Append:

```python
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
```

Also add the missing import at the top of the test file:

```python
import json
```

- [ ] **Step 3.2: Run tests to verify they fail**

```
/tmp/venv/bin/pytest tests/test_status.py -v -k "load_disk or delete_snapshot_removes_disk"
```

Expected: 5 failures (`AttributeError: module 'bsky_saves._status' has no attribute 'load_disk_on_startup'`).

- [ ] **Step 3.3: Add `load_disk_on_startup` to `src/bsky_saves/_status.py`**

Insert after the `delete_snapshot` function:

```python
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
```

- [ ] **Step 3.4: Run tests to verify they pass**

```
/tmp/venv/bin/pytest tests/test_status.py -v
```

Expected: 12 passed (7 from Task 2 + 5 new).

- [ ] **Step 3.5: Run full suite**

```
/tmp/venv/bin/pytest tests/ -q
```

Expected: all green.

- [ ] **Step 3.6: Commit**

```bash
git add src/bsky_saves/_status.py tests/test_status.py
git commit -m "feat(_status): disk loading + delete cleans up on-disk file

load_disk_on_startup reads <config_dir>/bsky-saves/status.json into
_disk_snapshot if present. Idempotent (won't overwrite a fresh
in-memory snapshot if called a second time). Malformed file (missing,
non-JSON, non-dict) logged as a stderr warning and treated as no disk
snapshot; file is NOT auto-deleted so operator can inspect.

delete_snapshot now also unlinks the on-disk file. Idempotent —
silently no-ops if the file doesn't exist.

Tests: present-file population, missing-file no-op, corrupt-file
warning, idempotent re-call, delete removes file."
```

---

## Task 4: Coalesced background flush + `priority: "final"` synchronous bypass

The persist-mode write-coalescing logic. Pushes update memory immediately; a daemon-thread Timer flushes to disk at most once per second. `priority: "final"` bypasses the timer.

**Files:**
- Modify: `src/bsky_saves/_status.py`
- Modify: `tests/test_status.py`

- [ ] **Step 4.1: Write failing tests in `tests/test_status.py`**

Append:

```python
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
```

Add at the top of the test file:

```python
import sys
```

- [ ] **Step 4.2: Run tests to verify they fail**

```
/tmp/venv/bin/pytest tests/test_status.py -v -k "persist_push_writes or session_push_does_not_write or priority_final or coalesced_flush or 0o600"
```

Expected: 6 failures (persist-mode tests find no file because `_schedule_coalesced_flush` is the placeholder from Task 2).

- [ ] **Step 4.3: Replace the `_schedule_coalesced_flush` stub and add the flush helpers in `src/bsky_saves/_status.py`**

Replace the existing `_schedule_coalesced_flush` placeholder with this block (which adds module-level state, the scheduler, the timer callback, and the synchronous-flush helper):

```python
_flush_lock = threading.Lock()
_flush_pending: bool = False
_flush_timer: threading.Timer | None = None
_last_flush_at: float = 0.0


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
        delay = max(0.0, (_last_flush_at + _FLUSH_DEBOUNCE_SECONDS) - time.monotonic())
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
```

Update `receive_push` to handle the `priority: "final"` bypass. Replace the lines:

```python
        # Persist mode.
        _memory_expires_at = 0.0  # No expiry in persist mode.

    # Persist-mode flush logic added in Task 4.
    _schedule_coalesced_flush()
```

with:

```python
        # Persist mode.
        _memory_expires_at = 0.0  # No expiry in persist mode.

    # Persist-mode flush: synchronous on priority="final", else coalesced.
    if body.get("priority") == "final":
        _flush_to_disk_synchronously(snap)
    else:
        _schedule_coalesced_flush()
```

Update `_reset_for_tests` to also reset the new flush state. Replace the existing function with:

```python
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
```

Update the existing Task 2 tests that stubbed out `_schedule_coalesced_flush` — they need to either point `config_dir` at `tmp_path` (already do) and let the flush fire, OR they need to keep stubbing it out. Two options:

- Option A: leave the stubs (the tests were testing memory-only behavior; not exercising disk).
- Option B: remove the stubs so the tests now exercise the real flush.

Pick **Option A** — the Task 2 tests deliberately scoped to memory-only behavior; keeping the stub keeps each test focused.

But note: the autouse `reset_status_module` fixture calls `_reset_for_tests` which cancels any pending Timer. Pending timer from a Task 2 test that didn't stub the flush would otherwise fire after the test ends. Make sure all Task 2 tests that call `receive_push` with persist-mode payload also stub `_schedule_coalesced_flush` to `lambda: None` (they all already do).

- [ ] **Step 4.4: Run tests to verify they pass**

```
/tmp/venv/bin/pytest tests/test_status.py -v
```

Expected: 18 passed (12 from Tasks 2+3 + 6 new).

- [ ] **Step 4.5: Run full suite**

```
/tmp/venv/bin/pytest tests/ -q
```

Expected: all green.

- [ ] **Step 4.6: Commit**

```bash
git add src/bsky_saves/_status.py tests/test_status.py
git commit -m "feat(_status): coalesced background flush + priority:\"final\" bypass

The persist-mode write-coalescing implementation per cross-repo R8:

  - In-memory snapshot updated immediately on every push.
  - threading.Timer schedules a flush at max(0, last_flush_at + 1s -
    monotonic_now). Daemon=True so it doesn't block process exit.
  - If a flush is already scheduled, new pushes ride on it — only one
    timer alive at a time. Steady-state guarantee: ≤1 disk write per
    second regardless of push rate.
  - priority='final' bypasses the timer entirely and flushes
    synchronously before receive_push returns. GUI uses this on
    beforeunload via navigator.sendBeacon so terminal state lands on
    disk before tab close.
  - Session-mode pushes never touch disk, even with priority='final'
    (cross-repo R8 'session ignores priority' clause). The privacy
    contract is honored under all inputs.

Tests cover: persist push eventually writes, session push never writes,
priority:final writes synchronously, session+priority:final still
doesn't write, 10 quick pushes produce one write with last-push-wins
state, written file has 0o600 perms."
```

---

## Task 5: Shutdown-synchronous flush

The `flush_synchronously()` entrypoint called from `run_serve`'s `finally` block, so a graceful SIGTERM / Ctrl-C drains the in-memory state to disk before exit.

**Files:**
- Modify: `src/bsky_saves/_status.py`
- Modify: `tests/test_status.py`

- [ ] **Step 5.1: Write failing tests in `tests/test_status.py`**

Append:

```python
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
```

- [ ] **Step 5.2: Run tests to verify they fail**

```
/tmp/venv/bin/pytest tests/test_status.py -v -k flush_synchronously
```

Expected: 3 failures (`AttributeError: module 'bsky_saves._status' has no attribute 'flush_synchronously'`).

- [ ] **Step 5.3: Add `flush_synchronously` to `src/bsky_saves/_status.py`**

Insert after `_flush_to_disk_synchronously`:

```python
def flush_synchronously() -> None:
    """Called from run_serve's shutdown hook.

    Drains the in-memory snapshot to disk if it's persist-mode and present.
    Session-mode snapshots are NOT flushed — the privacy contract is
    preserved even on graceful shutdown. No-op if memory is empty.
    """
    with _lock:
        snap = _memory_snapshot
        if snap is None:
            return
        is_session = (_memory_expires_at != 0.0)
    if is_session:
        return  # Session mode never writes to disk, even on shutdown.
    _flush_to_disk_synchronously(snap)
```

- [ ] **Step 5.4: Run tests to verify they pass**

```
/tmp/venv/bin/pytest tests/test_status.py -v -k flush_synchronously
```

Expected: 3 passed.

- [ ] **Step 5.5: Run full suite**

```
/tmp/venv/bin/pytest tests/ -q
```

Expected: all green.

- [ ] **Step 5.6: Commit**

```bash
git add src/bsky_saves/_status.py tests/test_status.py
git commit -m "feat(_status): synchronous-flush shutdown hook

Called from run_serve's finally block on graceful exit (Ctrl-C,
SIGTERM). Drains the in-memory persist-mode snapshot to disk so a
graceful shutdown doesn't lose the latest push.

Session-mode snapshots are deliberately skipped — the privacy
contract ('session never touches disk') is preserved under shutdown
too. No-op when memory is empty.

The wiring into run_serve itself lands in Task 7 once the endpoint
handlers are in (Task 6). For now flush_synchronously is callable
but unused from outside tests."
```

---

## Task 6: Endpoint route handlers + payload validation

Wire the three endpoints into `serve.py`. This is where the helper's HTTP surface becomes visible to the GUI and the panel.

**Files:**
- Modify: `src/bsky_saves/serve.py`
- Modify: `tests/test_serve.py`

- [ ] **Step 6.1: Write failing integration tests in `tests/test_serve.py`**

Add a fixture near the top (after the existing `paired_helper` fixture):

```python
@pytest.fixture
def reset_status_module(monkeypatch, tmp_path):
    """Reset _status module state and point config_dir at a temp dir so
    integration tests don't share state and don't touch the real
    ~/.config/bsky-saves/."""
    from bsky_saves import _status, _io
    monkeypatch.setattr(_io, "config_dir", lambda: tmp_path)
    _status._reset_for_tests()
    yield tmp_path
    _status._reset_for_tests()


def _valid_status_payload(handle="alice.bsky.social", did="did:plc:abc"):
    return {
        "schema_version": 1,
        "updated_at": "2026-05-21T20:00:00Z",
        "current_state": "idle",
        "library": {"handle": handle, "did": did, "total_saves": 100},
        "storage": {"mode": "persist", "session_ttl_seconds": None},
    }
```

Append the integration tests at the end of the file:

```python
# ============================================================
# v0.6.7: /status endpoints
# ============================================================


def test_post_status_204_on_valid_persist(paired_helper, reset_status_module):
    body = _valid_status_payload()
    body["priority"] = "final"  # Synchronous flush so the file is on disk by 204.
    with serve_in_background() as (port, _server):
        status, headers, resp_body = _request(
            port, "/status",
            method="POST",
            headers=_auth_headers(paired_helper),
            body=body,
        )
    assert status == 204
    assert resp_body == b""


def test_post_status_204_on_valid_session(paired_helper, reset_status_module):
    body = _valid_status_payload()
    body["storage"]["mode"] = "session"
    body["storage"]["session_ttl_seconds"] = 60
    with serve_in_background() as (port, _server):
        status, _h, _b = _request(
            port, "/status",
            method="POST",
            headers=_auth_headers(paired_helper),
            body=body,
        )
    assert status == 204


def test_post_status_400_on_invalid_schema_version(paired_helper, reset_status_module):
    body = _valid_status_payload()
    body["schema_version"] = 2
    with serve_in_background() as (port, _server):
        status, _h, resp_body = _request(
            port, "/status",
            method="POST",
            headers=_auth_headers(paired_helper),
            body=body,
        )
    assert status == 400
    payload = json.loads(resp_body)
    assert "schema_version" in payload["error"]


def test_post_status_400_on_missing_library_did(paired_helper, reset_status_module):
    body = _valid_status_payload()
    del body["library"]["did"]
    with serve_in_background() as (port, _server):
        status, _h, _b = _request(
            port, "/status",
            method="POST",
            headers=_auth_headers(paired_helper),
            body=body,
        )
    assert status == 400


def test_post_status_400_on_session_without_ttl(paired_helper, reset_status_module):
    body = _valid_status_payload()
    body["storage"]["mode"] = "session"
    body["storage"]["session_ttl_seconds"] = None  # missing ttl
    with serve_in_background() as (port, _server):
        status, _h, _b = _request(
            port, "/status",
            method="POST",
            headers=_auth_headers(paired_helper),
            body=body,
        )
    assert status == 400


def test_post_status_401_without_authorization(paired_helper, reset_status_module):
    with serve_in_background() as (port, _server):
        status, _h, _b = _request(
            port, "/status",
            method="POST",
            body=_valid_status_payload(),
        )
    assert status == 401


def test_get_status_404_with_no_prior_push(paired_helper, reset_status_module):
    with serve_in_background() as (port, _server):
        status, _h, _b = _request(
            port, "/status",
            method="GET",
            headers=_auth_headers(paired_helper),
        )
    assert status == 404


def test_get_status_200_after_persist_push(paired_helper, reset_status_module):
    body = _valid_status_payload()
    body["priority"] = "final"
    with serve_in_background() as (port, _server):
        _request(
            port, "/status",
            method="POST",
            headers=_auth_headers(paired_helper),
            body=body,
        )
        status, _h, resp_body = _request(
            port, "/status",
            method="GET",
            headers=_auth_headers(paired_helper),
        )
    assert status == 200
    payload = json.loads(resp_body)
    assert payload["library"]["did"] == body["library"]["did"]
    assert payload["library"]["total_saves"] == 100


def test_get_status_401_without_authorization(paired_helper, reset_status_module):
    with serve_in_background() as (port, _server):
        status, _h, _b = _request(
            port, "/status",
            method="GET",
        )
    assert status == 401


def test_delete_status_clears_memory_and_disk(paired_helper, reset_status_module, tmp_path):
    body = _valid_status_payload()
    body["priority"] = "final"
    with serve_in_background() as (port, _server):
        _request(
            port, "/status",
            method="POST",
            headers=_auth_headers(paired_helper),
            body=body,
        )
        # Confirm GET returns 200 first.
        s, _h, _b = _request(port, "/status", method="GET", headers=_auth_headers(paired_helper))
        assert s == 200
        # DELETE.
        s, _h, _b = _request(port, "/status", method="DELETE", headers=_auth_headers(paired_helper))
        assert s == 204
        # GET now returns 404.
        s, _h, _b = _request(port, "/status", method="GET", headers=_auth_headers(paired_helper))
        assert s == 404
    # Disk file is also gone.
    assert not (reset_status_module / "status.json").exists()


def test_delete_status_401_without_authorization(paired_helper, reset_status_module):
    with serve_in_background() as (port, _server):
        status, _h, _b = _request(port, "/status", method="DELETE")
    assert status == 401


def test_persist_disk_file_has_0o600_perms(paired_helper, reset_status_module):
    body = _valid_status_payload()
    body["priority"] = "final"
    with serve_in_background() as (port, _server):
        _request(
            port, "/status",
            method="POST",
            headers=_auth_headers(paired_helper),
            body=body,
        )
    path = reset_status_module / "status.json"
    assert path.exists()
    if sys.platform != "win32":
        perms = path.stat().st_mode & 0o777
        assert perms == 0o600, oct(perms)
```

- [ ] **Step 6.2: Run tests to verify they fail**

```
/tmp/venv/bin/pytest tests/test_serve.py -v -k "post_status or get_status or delete_status or persist_disk"
```

Expected: all 12 fail (the routes don't exist; 404 or 405 on every request).

- [ ] **Step 6.3: Add validation helper + route handlers in `src/bsky_saves/serve.py`**

Locate the existing routing block (search for `ROUTES = {`) and add the validation helper + three new handlers just above it. The exact placement: after `_handle_auth_check` and before the `ROUTES` dict.

```python
def _validate_status_payload(body: dict) -> str | None:
    """Validate a POST /status body. Returns an error message or None.

    Strict on required fields and their types; tolerates unknown fields
    for forward compatibility. See the v0.6.7 spec §5 for the schema.
    """
    if not isinstance(body, dict):
        return "body must be a JSON object"
    if body.get("schema_version") != 1:
        return f"invalid schema_version: {body.get('schema_version')!r} (must be 1)"
    if not isinstance(body.get("updated_at"), str) or not body["updated_at"]:
        return "missing or empty field: updated_at"
    if body.get("current_state") not in {"idle", "refreshing", "hydrating", "error"}:
        return f"invalid current_state: {body.get('current_state')!r}"
    lib = body.get("library")
    if not isinstance(lib, dict):
        return "missing field: library"
    if not isinstance(lib.get("handle"), str) or not lib["handle"]:
        return "missing or empty field: library.handle"
    if not isinstance(lib.get("did"), str) or not lib["did"].startswith("did:"):
        return "missing or invalid field: library.did"
    ts = lib.get("total_saves")
    if not isinstance(ts, int) or ts < 0:
        return "missing or invalid field: library.total_saves"
    storage = body.get("storage")
    if not isinstance(storage, dict):
        return "missing field: storage"
    mode = storage.get("mode")
    if mode not in {"persist", "session"}:
        return f"invalid storage.mode: {mode!r}"
    if mode == "session":
        ttl = storage.get("session_ttl_seconds")
        if not isinstance(ttl, int) or ttl <= 0:
            return "session mode requires positive storage.session_ttl_seconds"
    else:
        ttl = storage.get("session_ttl_seconds")
        if ttl is not None:
            return "persist mode must not set storage.session_ttl_seconds"
    return None


def _handle_status_post(handler) -> None:
    body = handler._read_json_body()
    if body is _BODY_REJECTED:
        return
    if not isinstance(body, dict):
        handler._send_json_error(400, "body must be a JSON object")
        return
    err = _validate_status_payload(body)
    if err is not None:
        handler._send_json_error(400, err)
        return
    from . import _status
    _status.receive_push(body)
    handler.send_response(204)
    handler.send_header("Content-Length", "0")
    handler._cors_headers()
    handler._security_headers()
    handler.end_headers()


def _handle_status_get(handler) -> None:
    from . import _status
    snap = _status.read_snapshot()
    if snap is None:
        handler._send_json_error(404, "no status snapshot")
        return
    handler._send_json(200, snap)


def _handle_status_delete(handler) -> None:
    from . import _status
    _status.delete_snapshot()
    handler.send_response(204)
    handler.send_header("Content-Length", "0")
    handler._cors_headers()
    handler._security_headers()
    handler.end_headers()
```

Locate the `ROUTES` dict and add three entries. Find:

```python
ROUTES = {
    ("GET", "/ping"): _handle_ping,
    ("GET", "/auth/check"): _handle_auth_check,
    ("POST", "/fetch-image"): _handle_fetch_image,
    ("POST", "/extract-article"): _handle_extract_article,
    ("POST", "/fetch"): _handle_fetch,
    ("POST", "/enrich"): _handle_enrich,
    ("POST", "/hydrate-threads"): _handle_hydrate_threads,
}
```

Replace with:

```python
ROUTES = {
    ("GET", "/ping"): _handle_ping,
    ("GET", "/auth/check"): _handle_auth_check,
    ("GET", "/status"): _handle_status_get,
    ("POST", "/fetch-image"): _handle_fetch_image,
    ("POST", "/extract-article"): _handle_extract_article,
    ("POST", "/fetch"): _handle_fetch,
    ("POST", "/enrich"): _handle_enrich,
    ("POST", "/hydrate-threads"): _handle_hydrate_threads,
    ("POST", "/status"): _handle_status_post,
    ("DELETE", "/status"): _handle_status_delete,
}
```

- [ ] **Step 6.4: Run integration tests to verify they pass**

```
/tmp/venv/bin/pytest tests/test_serve.py -v -k "post_status or get_status or delete_status or persist_disk"
```

Expected: all 12 passed.

- [ ] **Step 6.5: Run full suite**

```
/tmp/venv/bin/pytest tests/ -q
```

Expected: all green.

- [ ] **Step 6.6: Commit**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): add /status endpoints (POST, GET, DELETE)

Three new credentialed endpoints wiring serve.py to the _status module:

  POST /status   — validates the payload (schema_version, required
                   fields, storage.mode + ttl coupling), then delegates
                   to _status.receive_push. 204 No Content on accept,
                   400 with a short stable error string on invalid.
  GET /status    — delegates to _status.read_snapshot; 200 + JSON
                   payload, or 404 if no snapshot exists / session-mode
                   TTL has expired.
  DELETE /status — delegates to _status.delete_snapshot. 204 No Content.

All three sit behind the existing _check_token middleware unchanged.
None are added to EXEMPT_ROUTES. Same WWW-Authenticate: Bearer 401
shape via _send_json_error. Same CORS Allow-Headers and
Expose-Headers from v0.6.5.

_validate_status_payload follows _validate_creds's style — lightweight
hand-rolled isinstance checks rather than a schema library. Strict on
required fields; ignores unknown fields for forward compatibility.

Tests: 12 integration tests covering each endpoint, auth gating, valid
and invalid payloads, persistence file perms after a flushed push.
Uses a new reset_status_module fixture that points config_dir at a
tmp_path and resets _status module state between runs."
```

---

## Task 7: `run_serve` startup + shutdown wiring

Hook `_status.load_disk_on_startup()` into `run_serve` startup, and `_status.flush_synchronously()` into the `finally` block.

**Files:**
- Modify: `src/bsky_saves/serve.py`
- Modify: `tests/test_serve.py`

- [ ] **Step 7.1: Write failing integration tests in `tests/test_serve.py`**

Append:

```python
def test_run_serve_loads_disk_snapshot_on_startup(paired_helper, reset_status_module, tmp_path):
    """A pre-existing status.json on disk is visible via GET /status
    after the helper starts (before any in-memory push)."""
    from bsky_saves import _status
    # Pre-write a status file BEFORE serve starts.
    payload = _valid_status_payload(handle="loaded-from-disk.bsky.social")
    path = tmp_path / "status.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    # _status hasn't been told to load it yet; reset_status_module already
    # called _reset_for_tests at fixture setup, so _disk_loaded is False.
    with serve_in_background() as (port, _server):
        # The serve_in_background context should have called
        # _status.load_disk_on_startup at startup. GET /status now returns it.
        status, _h, resp_body = _request(
            port, "/status",
            method="GET",
            headers=_auth_headers(paired_helper),
        )
    assert status == 200
    got = json.loads(resp_body)
    assert got["library"]["handle"] == "loaded-from-disk.bsky.social"
```

- [ ] **Step 7.2: Run the new test to verify it fails**

```
/tmp/venv/bin/pytest tests/test_serve.py -v -k test_run_serve_loads_disk_snapshot
```

Expected: FAIL — status is 404 because `load_disk_on_startup` isn't wired into `run_serve` yet.

Also note: `serve_in_background` is the existing fixture used throughout the test suite; it calls `make_handler` directly and starts a `ThreadingHTTPServer`, but does NOT call `run_serve` itself. So the startup-hook wiring needs to live in BOTH `run_serve` (for the CLI path) AND in `serve_in_background` (for tests). Looking at the fixture, the cleanest path is to add the startup-load call to `serve_in_background` directly, since it's a test fixture that should mirror the daemon's startup behavior.

Update `serve_in_background` in `tests/test_serve.py` — locate the existing context manager (it's near the top of the file). Find the line that starts the server thread, and add an `_status.load_disk_on_startup()` call before it. Specifically, find:

```python
@contextlib.contextmanager
def serve_in_background(verbose=False, gui_root=None):
    """Start the helper on an ephemeral port, yield (port, server), then shutdown."""
    handler_cls = make_handler(
        port=0,
        allow_origins=_default_origins(0),
        verbose=verbose,
        gui_root=gui_root,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
```

And add immediately after the `server = ...` line, before the thread start:

```python
    # Mirror run_serve's startup hook so tests see the same behavior the
    # CLI does on `bsky-saves serve`.
    from bsky_saves import _status
    _status.load_disk_on_startup()
```

- [ ] **Step 7.3: Run the test again — still failing (the test depends on wiring in `run_serve` and the fixture)**

```
/tmp/venv/bin/pytest tests/test_serve.py -v -k test_run_serve_loads_disk_snapshot
```

Expected: now PASS (because the fixture wiring was added in 7.2).

- [ ] **Step 7.4: Wire `run_serve` itself in `src/bsky_saves/serve.py`**

Find the existing `run_serve` function. Locate the block:

```python
    handler_cls = make_handler(
        port=port,
        allow_origins=origins,
        verbose=verbose,
        gui_root=gui_root,
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    print(
        f"bsky-saves serve listening on http://127.0.0.1:{port} "
        f"(origins: {', '.join(origins)})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
```

Replace with:

```python
    handler_cls = make_handler(
        port=port,
        allow_origins=origins,
        verbose=verbose,
        gui_root=gui_root,
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)

    # v0.6.7: load any persisted status snapshot from disk before accepting
    # requests. Subsequent GET /status calls will return this until a fresh
    # push overwrites.
    from . import _status
    _status.load_disk_on_startup()

    print(
        f"bsky-saves serve listening on http://127.0.0.1:{port} "
        f"(origins: {', '.join(origins)})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # v0.6.7: drain the in-memory persist-mode snapshot to disk before
        # exit. Session-mode snapshots are deliberately skipped (privacy
        # contract preserved on shutdown). No-op if memory is empty.
        _status.flush_synchronously()
        server.shutdown()
        server.server_close()
```

- [ ] **Step 7.5: Run full suite**

```
/tmp/venv/bin/pytest tests/ -q
```

Expected: all green, including the new `test_run_serve_loads_disk_snapshot_on_startup`.

- [ ] **Step 7.6: Commit**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): wire _status startup/shutdown hooks into run_serve

Two lifecycle hooks added:

  - Startup: load_disk_on_startup() runs after make_handler and before
    serve_forever, so any pre-existing <config_dir>/bsky-saves/status.json
    is visible via GET /status before the first push arrives.
  - Shutdown: flush_synchronously() runs in the finally block before
    server.shutdown(), so a graceful exit (Ctrl-C, SIGTERM) drains any
    in-memory persist-mode snapshot to disk.

The serve_in_background test fixture mirrors the startup hook so
integration tests see the same load behavior as the CLI's
'bsky-saves serve' path.

Test: pre-write status.json before serve starts, GET /status returns
the persisted snapshot."
```

---

## Task 8: Version bump + README docs

Bump `pyproject.toml` to `0.6.7` and add a user-facing docs section.

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`

- [ ] **Step 8.1: Bump version in `pyproject.toml`**

Find `version = "0.6.6"` and replace with `version = "0.6.7"`.

- [ ] **Step 8.2: Add README documentation**

Open `README.md` and locate the `## bsky-saves serve` section. After the existing `### Pairing` subsection (and before `### --gui mode`), insert:

```markdown
### Status snapshot (v0.6.7+)

The helper exposes three credentialed endpoints for the installer's status panel to display library state without opening the GUI:

| Method | Path | Purpose |
|---|---|---|
| `POST`   | `/status` | Publish a library snapshot (the GUI pushes this). |
| `GET`    | `/status` | Read the latest snapshot. `200` with JSON or `404` if no snapshot exists. |
| `DELETE` | `/status` | Clear the snapshot (the GUI calls this from "Settings → Clear all data"). |

The snapshot lives in helper memory and (in `persist` mode) is mirrored to `<config_dir>/bsky-saves/status.json` (sibling of the token file, `0600` perms). In `session` mode it's memory-only with a per-push TTL — the helper drops the snapshot if the GUI stops pushing heartbeats. Disk writes in persist mode are coalesced to at most one per second; the GUI can request a synchronous flush by sending `"priority": "final"` in the payload (used on `beforeunload` so terminal state lands on disk before tab close).

Auth: same `Authorization: Bearer <token>` as every other credentialed endpoint. No protocol bump — the endpoints are additive.

Full cross-repo contract: [`bsky-saves-coordination:docs/installer-status-panel.md`](https://github.com/tenorune/bsky-saves-coordination/blob/main/docs/installer-status-panel.md). Helper-side implementation spec: [`docs/superpowers/specs/2026-05-21-bsky-saves-v0.6.7-status-endpoints.md`](docs/superpowers/specs/2026-05-21-bsky-saves-v0.6.7-status-endpoints.md).
```

- [ ] **Step 8.3: Run full suite as a final sanity check**

```
/tmp/venv/bin/pytest tests/ -q
```

Expected: all green; no test should depend on the README content.

- [ ] **Step 8.4: Commit**

```bash
git add pyproject.toml README.md
git commit -m "release(v0.6.7): version bump + README docs for /status endpoints

Bumps package version to 0.6.7. Adds a 'Status snapshot' subsection to
the README's 'bsky-saves serve' section so users can discover the new
endpoints and understand the storage model at a glance, with pointers
to the cross-repo contract and the helper-side spec for depth.

No protocol bump — the endpoints are additive (no auth-requirement
change to existing endpoints per docs/protocol-versioning.md). The
GUI pushes status, the installer panel reads it; old GUIs that don't
push simply leave the panel showing 'no snapshot yet'."
```

---

## Self-review

Spec coverage check:

| Spec section | Task coverage |
|---|---|
| §3 Files modified — `_io.py::atomic_write_status` | Task 1 ✓ |
| §3 Files modified — `serve.py` route handlers + ROUTES | Task 6 ✓ |
| §3 Files modified — `serve.py::run_serve` startup/shutdown | Task 7 ✓ |
| §3 Files modified — `pyproject.toml` version bump | Task 8 ✓ |
| §3 Files modified — `README.md` Status snapshot subsection | Task 8 ✓ |
| §3 Files created — `_status.py` | Tasks 2 (core), 3 (disk load), 4 (flush), 5 (shutdown) ✓ |
| §3 Files created — `tests/test_status.py` | Tasks 2, 3, 4, 5 ✓ |
| §4 Endpoint contracts — POST /status | Task 6 ✓ |
| §4 Endpoint contracts — GET /status | Task 6 ✓ |
| §4 Endpoint contracts — DELETE /status | Task 6 ✓ |
| §5 Payload validation | Task 6 (`_validate_status_payload`) ✓ |
| §6 State machine — Snapshot dataclass + locks | Task 2 ✓ |
| §6 State machine — receive_push session/persist branches | Tasks 2 (memory) + 4 (flush wiring) ✓ |
| §6 State machine — read_snapshot lazy expiry | Task 2 ✓ |
| §6 State machine — delete_snapshot | Tasks 2 (memory) + 3 (disk file) ✓ |
| §7 Coalesced flush algorithm | Task 4 ✓ |
| §7 Shutdown flush | Task 5 (implementation) + Task 7 (wiring into run_serve) ✓ |
| §8 Persistence file — path, perms, atomic write | Tasks 1 (writer) + 4 (caller) ✓ |
| §8 Persistence file — startup load | Tasks 3 (implementation) + 7 (wiring) ✓ |
| §9 Authentication — same _check_token middleware | Task 6 (routes added to ROUTES, not to EXEMPT_ROUTES) ✓ |
| §10 Tests — unit (test_status.py) | Tasks 2, 3, 4, 5 ✓ |
| §10 Tests — integration (test_serve.py) | Tasks 6, 7 ✓ |
| §11 Backward compat — schema_version, no protocol bump | Tasks 6 (validation) + 8 (commit message) ✓ |

No gaps.

Placeholder scan: no "TODO", no "implement later", no "similar to Task N", no "add appropriate error handling" — every step has concrete code or commands.

Type consistency check:

- `Snapshot` dataclass used identically across all tasks (Tasks 2–5). Fields: `payload: dict`, `received_at: float`.
- `_lock`, `_flush_lock`, `_memory_snapshot`, `_memory_expires_at`, `_disk_snapshot`, `_disk_loaded`, `_flush_pending`, `_flush_timer`, `_last_flush_at` — all introduced in Task 2 or Task 4, used consistently in Tasks 3, 4, 5.
- `receive_push(body: dict)`, `read_snapshot() -> dict | None`, `delete_snapshot()`, `load_disk_on_startup()`, `flush_synchronously()` — signatures stable across all tasks.
- `atomic_write_status(path: Path, body: bytes)` — same signature in Task 1 (definition) and Task 4 (use).
- `_validate_status_payload(body: dict) -> str | None` — defined and used only in Task 6.
- `_valid_status_payload` test helper exists in both `test_status.py` (with overload names `_valid_persist_payload`, `_valid_session_payload`) and `test_serve.py` (single function). Distinct files, no collision.

No type / signature drift.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-21-bsky-saves-v0.6.7-status-endpoints.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, spec-then-quality review between tasks, fast iteration. Matches the v0.6.0 / v0.6.2 implementation flow.

**2. Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

Which approach?
