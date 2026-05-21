# bsky-saves v0.6.7 — Status-snapshot endpoints

> **Status:** drafting (2026-05-21). Implementation pending.
> **Branch:** `claude/v0.6.7-status-endpoints` in `tenorune/bsky-saves`.
> **Releases as:** PyPI `bsky-saves==0.6.7`. Consumers: `bsky-saves-gui` (publishes via `POST /status`) and `bsky-saves-install`'s tray panel (consumes via `GET /status`).
> **External contract:** `tenorune/bsky-saves-coordination:docs/installer-status-panel.md` and its companion `installer-status-panel-resolved.md`. That document is canonical for the **cross-repo design**; this spec is canonical for the **bsky-saves implementation**. The two MUST agree on the payload shape (cross-repo §4.4), the endpoint surface (cross-repo §4.2), the mode-dependent storage rules (cross-repo §4.2 + R8), and the auth model (cross-repo §4.6) — that agreement is the anti-drift contract for v0.6.7.

---

## 1. Context

The cross-repo coordination doc (`bsky-saves-coordination:docs/installer-status-panel.md`) locks the contract for an installer status panel that the user sees in `bsky-saves-install`'s menu-bar tray. The panel needs to display summary library state — total saves, hydration completion, last-activity summary — even when the GUI tab isn't open. That state lives in browser-resident storage owned by `bsky-saves-gui`, so the GUI is the source of truth; the helper sits in the middle as a state-cache proxy; the panel polls the helper. None of the three teams can implement their slice until the helper exposes the agreed endpoints.

v0.6.7 ships the helper-side slice: three new HTTP endpoints (`POST /status`, `GET /status`, `DELETE /status`), the mode-dependent in-memory + on-disk state machine that backs them, the coalesced background flush task that bounds disk writes, and the shutdown hook that guarantees no data loss on graceful exit.

The cross-repo doc captures the *why* for each design decision (R1 through R10 in the resolved-questions archive); this spec captures the *how* on the bsky-saves side.

## 2. Scope

`bsky-saves` remains a helper daemon + CLI package. v0.6.7 adds three HTTP endpoints to `serve.py`, a small state-machine module for the snapshot lifecycle, and a background flush thread. No CLI surface changes, no protocol bump.

### In scope

- **Endpoints:** `POST /status`, `GET /status`, `DELETE /status` — §4.
- **Payload validation:** strict on required fields, forward-compatible on unknown ones — §5.
- **State machine:** `{ memory_snapshot, memory_expires_at, disk_snapshot }`, behind a single lock — §6.
- **Session-mode TTL:** lazy expiry on `GET /status`, value supplied per-push in `storage.session_ttl_seconds` — §6.
- **Persist-mode coalesced flush:** at most one disk write per second, with `priority: "final"` synchronous-bypass and shutdown-synchronous flush — §7.
- **Persistence file:** `<config_dir>/bsky-saves/status.json`, `0o600`, atomic-write via per-write tmp names — §8.
- **Auth:** all three endpoints behind the existing `_check_token` middleware introduced in v0.6.2 — §9.
- **Tests:** comprehensive integration tests for each endpoint + state-machine + flush behavior + auth — §10.

### Out of scope (explicitly deferred)

- **`schema_version` bumping.** This is the first shipped version; ships as `1`. Future schema changes follow the bump rules in cross-repo §4.4 and the security gate in cross-repo §4.7.
- **Per-DID indexing of snapshots.** Phase 1 is single-slot last-write-wins (cross-repo R3). Phase 3 (CLI inventories) revisits this; the payload's `library.did` is the forward-compat hook.
- **`/auth/check` parity for `/status` endpoints.** Out of scope; the `/status` family is credentialed like `/fetch` etc., and uses the existing `_check_token` gate. No equivalent of `/auth/check` for status because the panel can probe `/auth/check` directly to test the pairing.
- **Phase 2 commands (panel → GUI).** Cross-repo §5 sketch only; out of phase 1.
- **Phase 3 CLI inventories on disk.** Cross-repo §6 sketch only.
- **Schema validation library.** Lightweight hand-written `isinstance`-style checks (matching `serve._validate_creds`'s style). No `jsonschema` / `pydantic` dependency.

## 3. Architecture and module layout

### Files modified

| File | Change |
|---|---|
| `src/bsky_saves/serve.py` | Three new route handlers (`_handle_status_post`, `_handle_status_get`, `_handle_status_delete`); registered in `ROUTES`. Each handler delegates to the state machine. Shutdown hook in `run_serve` calls the state machine's `flush_synchronously` before `server.shutdown()`. |
| `src/bsky_saves/_io.py` | One small helper: `atomic_write_status(path, body_bytes)` — writes via `tempfile.NamedTemporaryFile(delete=False, dir=path.parent, suffix=".tmp")` + `os.replace`. Per-write unique tmp names. Defense-in-depth against any future caller running it from multiple threads simultaneously. |
| `tests/test_serve.py` | New integration tests for the three endpoints + state-machine + flush behavior. |
| `tests/test_io.py` | New tests for `atomic_write_status`. |
| `pyproject.toml` | `version = "0.6.7"`. |
| `README.md` | New `### Status snapshot` subsection under `## bsky-saves serve` documenting the endpoint surface at user-facing granularity. |

### Files created

| File | Responsibility |
|---|---|
| `src/bsky_saves/_status.py` | Module owning the snapshot state machine: `Snapshot` dataclass, the in-memory store + lock, the background flush thread, the disk read/write, the shutdown-flush entrypoint. Single source of truth for what's in memory vs. on disk. Importable from `serve.py` for route handlers and from `run_serve` for the shutdown hook. |
| `tests/test_status.py` | Unit tests for `_status` internals — state-machine transitions, lazy expiry, coalesce timing, shutdown flush. Complements the integration tests in `tests/test_serve.py`. |

## 4. Endpoint contracts

Endpoints sit behind the existing `_security_gate` middleware (Host → Origin → Bearer token, in that order). All three are credentialed; none are exempt.

### `POST /status`

**Purpose:** GUI publishes the latest library state.

**Request:**

- Method: `POST`
- Path: `/status`
- Headers: `Authorization: Bearer <token>`, `Content-Type: application/json`
- Body: JSON conforming to the payload shape in cross-repo §4.4. See §5 below for the validation rules.
- Max body size: 10 MB (inherited from `_MAX_BODY_BYTES` in `serve.py`; status payloads are small but the existing cap covers us).

**Response:**

- `204 No Content` on accept (no body).
- `400 Bad Request` on malformed JSON, missing required fields, or wrong-type fields. Body: `{"error": "<short reason>"}`.
- `401 Unauthorized` on missing / invalid Bearer token (handled by `_check_token`; not endpoint-specific).
- `413 Payload Too Large` on bodies over 10 MB (handled by `_read_json_body`).

**Side effects (on `204`):**

- The in-memory snapshot is replaced with the parsed payload (last-write-wins, single-slot per cross-repo R3).
- If `storage.mode === "session"`: `memory_expires_at = now() + storage.session_ttl_seconds`. No disk write.
- If `storage.mode === "persist"`:
  - If `body.priority === "final"`: synchronous flush to disk before returning `204`.
  - Else: enqueue/schedule a coalesced flush (§7). Return `204` immediately.

### `GET /status`

**Purpose:** Panel reads the latest unexpired snapshot.

**Request:**

- Method: `GET`
- Path: `/status`
- Headers: `Authorization: Bearer <token>`
- No body, no query params.

**Response:**

- `200 OK` with the latest unexpired snapshot as JSON. The `updated_at` field reflects when the GUI published it (not when the helper served it).
- `404 Not Found` if no snapshot exists (never pushed) or the in-memory session-mode snapshot has expired and no persist-mode disk snapshot exists.
- `401 Unauthorized` as above.

**Read priority:**

1. Check `memory_expires_at`. If set and `< now()`, drop `memory_snapshot` and `memory_expires_at` (lazy expiry).
2. If `memory_snapshot` is set, return it.
3. Else if `disk_snapshot` is loaded (from helper-startup or a prior persist-mode push), return it.
4. Else `404`.

### `DELETE /status`

**Purpose:** GUI explicit "Clear all data" path (cross-repo R5).

**Request:**

- Method: `DELETE`
- Path: `/status`
- Headers: `Authorization: Bearer <token>`
- No body.

**Response:**

- `204 No Content` on success.
- `401 Unauthorized` as above.

**Side effects:**

- `memory_snapshot = None`, `memory_expires_at = 0`.
- `disk_snapshot = None`.
- Unlink `<config_dir>/bsky-saves/status.json` if it exists.

## 5. Payload validation

Light hand-rolled validation matching `serve.py::_validate_creds`'s style. Goal: reject malformed required fields with `400`; ignore unknown fields for forward compatibility.

**Required fields and their types:**

| Field | Type | Constraint |
|---|---|---|
| `schema_version` | int | Must equal `1`. Future bumps reject; the panel-side handles older versions gracefully per cross-repo §4.4. |
| `updated_at` | string | Non-empty. Format is ISO-8601 by convention, not parsed/validated structurally — the helper treats it as an opaque string. |
| `current_state` | string | One of `{"idle", "refreshing", "hydrating", "error"}`. |
| `library` | object | Required. See sub-fields. |
| `library.handle` | string | Non-empty. |
| `library.did` | string | Non-empty, starts with `did:`. |
| `library.total_saves` | int | `≥ 0`. |
| `storage` | object | Required. See sub-fields. |
| `storage.mode` | string | One of `{"persist", "session"}`. |
| `storage.session_ttl_seconds` | int or null | Required when `mode === "session"`; must be positive. When `mode === "persist"`, must be null or absent. |

**Optional fields validated for type if present:**

| Field | Type |
|---|---|
| `priority` | string. Currently only `"final"` triggers special behavior; other values logged and treated as default. |
| `library.by_status` | object with string keys, int values |
| `hydration.{articles,threads,images}` | object with `completed` (int ≥ 0), `total` (int ≥ 0) |
| `storage.browser_bytes_estimate` | int or null |
| `last_activity` | object; sub-fields not strictly validated beyond type |
| `last_activity.errors` | array of `{kind: string, message: string, count: int}` |

**Unknown fields at any level:** ignored, not rejected. Preserved on disk only if the helper round-trips the payload as-is (which it does — the helper stores the full body as received).

**Forward compatibility:**

- `schema_version` is checked strictly to keep the helper from accepting future incompatible payloads it can't reason about. Future schema bumps will require a coordinated helper release.
- The `priority` field tolerates unknown values (treated as default) so the GUI can add new priority hints without a `schema_version` bump.

**Error response shape:** `400` body is `{"error": "<short reason>"}`, matching the existing `_send_json_error` style. Reasons are short and stable (e.g., `"missing field: library.did"`, `"invalid schema_version: 2"`) so the GUI's error handler can match on them if it wants.

## 6. State-machine module (`src/bsky_saves/_status.py`)

A single module owns the in-memory state, lazy expiry, disk loading, and the coalesced flush. All state is behind one threading.Lock.

### State

```python
@dataclass
class Snapshot:
    payload: dict             # The raw JSON dict received from POST /status.
    received_at: float        # time.monotonic() when the helper received the push.

_lock = threading.Lock()
_memory_snapshot: Snapshot | None = None
_memory_expires_at: float = 0.0       # time.monotonic() value; 0 means "no expiry pending"
_disk_loaded: bool = False
_disk_snapshot: Snapshot | None = None
_flush_pending = False
_last_flush_at: float = 0.0           # time.monotonic() of last successful disk write
_flush_lock = threading.Lock()        # Protects flush coordination state separate from _lock.
```

### Public API

```python
def receive_push(body: dict) -> None:
    """Apply a validated POST /status body. Updates memory; schedules disk flush
    if persist-mode. Synchronous flush if priority='final'."""

def read_snapshot() -> dict | None:
    """Return the current visible snapshot's payload, or None if 404. Performs
    lazy session-mode TTL expiry as a side effect."""

def delete_snapshot() -> None:
    """Drop memory + disk."""

def load_disk_on_startup(config_dir: Path) -> None:
    """Called once during run_serve startup. Reads <config_dir>/bsky-saves/status.json
    into _disk_snapshot if present. Idempotent; safe to call multiple times."""

def flush_synchronously() -> None:
    """Called from the run_serve shutdown hook. Writes the in-memory snapshot
    to disk if it's persist-mode and newer than the disk copy."""
```

### Lazy expiry

`read_snapshot()` checks `_memory_expires_at < time.monotonic()` and drops the in-memory state if expired. No background expiry timer — expiry happens on the next read.

## 7. Coalesced background flush

The helper bounds disk writes to ≤ 1/second for persist-mode pushes, with explicit bypass paths.

### Algorithm

```
On POST /status with storage.mode == "persist":
    Update _memory_snapshot under _lock.

    If priority == "final":
        Synchronously flush to disk under _flush_lock.
        Set _last_flush_at = monotonic().
        Return 204.

    Else (default coalesced):
        Under _flush_lock:
            If _flush_pending:
                Nothing more to do — a flush is already scheduled.
                It will pick up the latest _memory_snapshot when it fires.
            Else:
                _flush_pending = True.
                Schedule a Timer to fire at max(0, _last_flush_at + 1.0 - monotonic()).
                Timer callback writes to disk, updates _last_flush_at,
                clears _flush_pending.
        Return 204.
```

This guarantees:

- **At most one disk write per second** in steady state. If 100 pushes arrive in 1 second, the helper writes once.
- **No write older than ~1s** is left undisked. Each push schedules a flush no later than 1s after the last write.
- **`priority: "final"` always flushes synchronously.** Used by the GUI on `beforeunload` per cross-repo §4.3.
- **The Timer is daemon=True** so it doesn't block process exit; shutdown is handled by `flush_synchronously()` instead (§7.2).

### Shutdown flush

`run_serve`'s `finally` block calls `_status.flush_synchronously()` before `server.shutdown()`. The flush:

1. Acquires `_flush_lock`.
2. Cancels any pending Timer (best-effort; the Timer may already be running).
3. Reads the current `_memory_snapshot` under `_lock`.
4. If the snapshot exists and is newer than the on-disk copy (compare `received_at`), writes it via `atomic_write_status`.
5. Updates `_last_flush_at`.

The shutdown flush is synchronous — `run_serve` doesn't return until disk reflects the latest in-memory state. This makes shutdown the only guaranteed flush boundary in addition to `priority: "final"`.

### Session-mode

Session-mode pushes do NOT enqueue a flush. They never write to disk, regardless of `priority`. The `priority` field is logged but ignored when `storage.mode == "session"`.

## 8. Persistence file

### Path and perms

- Path: `<config_dir>/bsky-saves/status.json` (sibling of the token file).
- Perms: `0o600` (owner-read/write only).
- Dir perms: `0o700` (matches the v0.6.2 token-dir convention).

### Format

The file contains the most-recently-flushed payload's `payload` field as JSON, with a trailing newline. The helper does NOT store any metadata (no `received_at`, no version envelope) — the payload's own `updated_at` is the timestamp of record.

### Atomic write (`atomic_write_status`)

New helper in `src/bsky_saves/_io.py`:

```python
def atomic_write_status(path: Path, body: bytes) -> None:
    """Atomic write with per-write unique tmp names. Defense-in-depth against
    concurrent writers (today's caller is single-threaded by design; this
    pattern survives a future contributor running it from multiple threads)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=path.parent,
        prefix="status.",
        suffix=".tmp",
    ) as tmp:
        tmp.write(body)
        tmp_path = Path(tmp.name)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
```

The existing `atomic_write_inventory` is NOT used because its single-tmp-name pattern races under concurrent calls. We could refactor it but that's a separate concern; `atomic_write_status` is the focused fix.

### Startup load

`run_serve` calls `_status.load_disk_on_startup(config_dir())` once, between `make_handler` and `server.serve_forever`. If the file exists, its contents are loaded into `_disk_snapshot`. The first `GET /status` after startup returns this disk snapshot (until a fresh push overwrites it).

Malformed disk file (corrupt JSON, missing required fields, etc.) is logged as a warning and treated as if no disk file existed. The file is NOT auto-deleted on parse failure — operator can inspect.

## 9. Authentication

All three endpoints go through `_security_gate`'s existing chain unchanged:

1. `_check_host` — `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`; else 421.
2. `_check_origin` — `Origin` header must be in the allowlist (loopback + `saves.lightseed.net` + any `--allow-origin` additions); else 403.
3. `_check_token` — `Authorization: Bearer <token>` required; else 401 with `WWW-Authenticate: Bearer realm="bsky-saves"` (and `error="invalid_token"` for wrong-value variant).

None of `/status`'s endpoints are added to `EXEMPT_ROUTES`. They participate in the full auth chain.

CORS handling on the response: `_cors_headers` already emits `Access-Control-Allow-Headers: Content-Type, Authorization` (v0.6.5) and `Access-Control-Expose-Headers: WWW-Authenticate` (v0.6.5). Both are needed for the GUI's `POST /status` from `saves.lightseed.net`. No changes required.

## 10. Tests

### Unit tests (`tests/test_status.py`)

State-machine internals, no HTTP:

- `test_receive_persist_push_updates_memory`
- `test_receive_persist_push_schedules_flush`
- `test_receive_session_push_sets_expiry`
- `test_receive_session_push_skips_disk`
- `test_priority_final_flushes_synchronously`
- `test_session_mode_ignores_priority_final` (no disk write even with `priority: "final"`)
- `test_lazy_expiry_drops_memory_on_read_after_ttl`
- `test_coalesced_flush_bounds_writes_to_one_per_second` (rapid sequence of pushes → single disk write)
- `test_concurrent_pushes_preserve_last_write_wins` (threading test, ten concurrent pushes, final state matches the last writer)
- `test_load_disk_on_startup_populates_disk_snapshot`
- `test_load_disk_on_startup_handles_missing_file`
- `test_load_disk_on_startup_handles_corrupt_file`
- `test_flush_synchronously_writes_pending_in_memory`
- `test_delete_snapshot_clears_memory_and_disk`

### Integration tests (`tests/test_serve.py`)

End-to-end via the daemon, using the existing `serve_in_background` fixture pattern:

- `test_post_status_204_on_valid_persist_push`
- `test_post_status_204_on_valid_session_push`
- `test_post_status_400_on_malformed_json`
- `test_post_status_400_on_missing_required_field`
- `test_post_status_400_on_wrong_schema_version`
- `test_post_status_400_on_session_mode_without_ttl`
- `test_post_status_401_without_authorization`
- `test_get_status_404_with_no_prior_push`
- `test_get_status_200_after_persist_push`
- `test_get_status_200_after_session_push`
- `test_get_status_404_after_session_ttl_expiry`
- `test_get_status_falls_back_to_disk_when_memory_empty`
- `test_get_status_401_without_authorization`
- `test_delete_status_clears_memory_and_disk`
- `test_delete_status_401_without_authorization`
- `test_disk_flush_is_coalesced_under_load` (multiple pushes in quick succession; verify disk-write count via `stat()` mtime checks)
- `test_disk_flush_runs_synchronously_with_priority_final`
- `test_shutdown_flushes_pending_in_memory_to_disk`
- `test_persist_mode_disk_file_has_0o600_perms`

### Test scaffolding

A new fixture in `tests/test_serve.py`:

```python
@pytest.fixture
def isolated_status_dir(monkeypatch, tmp_path):
    """Point the helper's config_dir at a temp directory so status.json
    writes don't touch the real ~/.config/bsky-saves/ during tests."""
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    yield tmp_path
```

Tests that exercise persist-mode use this fixture; session-mode tests don't strictly need it (no disk writes) but use it for consistency.

A small `valid_payload()` helper that returns a complete valid POST body, so each test only specifies the fields it's exercising.

## 11. Backward compatibility

### Wire format

- `POST /status`, `GET /status`, `DELETE /status` are new routes; they don't conflict with anything.
- The payload's `schema_version: 1` is the first shipped version. Future GUI versions can ship payloads with the same `schema_version: 1` plus new optional fields — the helper preserves and stores unknown fields, future readers can see them. A bumped `schema_version: 2` would require a coordinated helper release.

### Protocol

`_PROTOCOL_VERSION` stays at `"2"` (set in v0.6.2). New additive endpoints don't bump protocol per `docs/protocol-versioning.md` — that rule fires on auth or shape changes to existing endpoints.

### Old GUIs

GUIs older than the one that ships the `POST /status` push code simply don't push. The panel sees `404` from `GET /status` and shows its "no snapshot yet" placeholder. Existing GUIs continue to work against credentialed endpoints (`/fetch`, etc.) unchanged.

### Old helpers + new GUIs

Symmetrically: a GUI that pushes `POST /status` to an older helper (pre-v0.6.7) gets `404 Not Found` on the unknown route. The GUI logs at debug level and continues — push failures are non-fatal per cross-repo §4.3. Panel shows the "no snapshot yet" placeholder until the helper is upgraded.

## 12. Sequencing

1. **Spec lands** (this doc). User reviews.
2. **Plan doc** at `docs/superpowers/plans/2026-05-21-bsky-saves-v0.6.7-status-endpoints.md` follows the v0.6.x pattern: 6–8 bite-sized TDD tasks, each producing a green-suite-at-task-boundary commit.
3. **Implementation** via the `superpowers:subagent-driven-development` flow used through v0.6.x.
4. **Release** via the existing `release.yml` tag pipeline. v0.6.7 ships to PyPI. The `wheel-version-bump` dispatch fires at `bsky-saves-install`; their auto-PR pins the new helper version into the next installer build.
5. **GUI ships its push code in a coordinated release** (whatever GUI version follows v0.6.4). The `gui-version-bump` dispatch back to `bsky-saves` auto-PRs the bundled-GUI pin; helper version with matching GUI bundle ships in the next coordinated release.
6. **Installer ships the panel UI** consuming `GET /status`. Coordinated bundle ships helper + GUI + installer together.

The CLI-side implementation is independent of the other two repos' work and can ship to PyPI as soon as the spec + plan + implementation land. The user-visible feature requires all three.

## 13. Open questions

None at spec-lock time. All design open-questions resolved in the cross-repo doc (R1–R10). The remaining decisions are sequencing (above) and bite-sized task ordering (which the plan doc owns).

If implementation surfaces a question that wasn't covered, it gets surfaced via:

- The cross-repo `installer-status-panel.md` doc (a new Q11+) if it changes the contract.
- This spec's revision (in-place edit) if it's an internal bsky-saves concern.

Either route requires a doc update before the implementing PR merges.
