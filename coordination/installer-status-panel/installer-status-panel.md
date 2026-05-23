# Installer status panel — cross-repo coordination doc

> **Status:** phase 1 contract closed (2026-05-23). All open questions Q1–Q13 resolved and archived in [`installer-status-panel-resolved.md`](./installer-status-panel-resolved.md) as R1–R14. End-to-end cold-start UX validated by the installer team against the paired `bsky-saves==0.6.8` (final) + `bsky-saves-gui` v0.6.5 (final) + `bsky-saves-install` releases. Phase 2 (commands from panel to GUI) and phase 3 (CLI inventories) remain sketches in §§5–6; this doc is the source of truth for what's locked.
> **Lives at:** `bsky-saves-coordination:docs/installer-status-panel.md` (canonical). Mirrored as a draft in each primary repo's `coordination` branch and PRed back via cross-repo workflow.
> **Audience:** maintainers of `bsky-saves` (helper / CLI), `bsky-saves-gui` (Svelte PWA), and `bsky-saves-install` (native macOS app + future Win/Linux installers).
> **Scope:** the contract for the installer's status panel — what info it surfaces, where the info comes from, who's responsible for each leg.
> **Anti-drift:** this doc is the single source of truth for the cross-repo design. PRs that touch the contract on any side should also update this doc.
> **Resolved-questions archive:** see [`installer-status-panel-resolved.md`](./installer-status-panel-resolved.md) for closed questions and their resolutions, kept as a design-rationale record.

---

## 1. Purpose

The installer (`bsky-saves-install`) presents users with a native menu-bar icon plus a status panel. The panel should let users see, at a glance, the state of their backup library — and (in later phases) issue commands like refresh / export / backup-toggle changes without opening the GUI in a browser.

Because the data the panel shows is owned by `bsky-saves-gui` (browser-resident library state), and the panel is a native UI in the installer, the design needs cross-repo coordination: the GUI has to expose state in a form the panel can read, the helper (`bsky-saves`) sits between them as the transport, and the installer owns the panel UI.

This document captures the design that emerged from the v0.6.x release-cycle conversations and locks the contract for the three repos.

## 2. Audience and responsibilities

| Repo | What it owns in this design |
|---|---|
| `bsky-saves` | The helper daemon. New `POST /status`, `GET /status`, and `DELETE /status` endpoints in phase 1; the on-disk status-cache file for persist mode with coalesced background flush; an in-memory TTL slot for session mode; auth gating identical to other credentialed endpoints. |
| `bsky-saves-gui` | Pushing summary library stats to the helper at meaningful moments. Owns the payload contents (§4.4), the push trigger list (§4.3), the session-mode heartbeat (§4.3), the `priority: "final"` hint on terminal pushes (§4.3, §4.4), and how library state is computed. |
| `bsky-saves-install` | The status panel UI. Polls `GET /status` while the popover is visible (§4.5) and renders. Distinguishes "no snapshot yet", "active snapshot", and "stale snapshot" (where `updated_at` is older than the §4.5 staleness threshold). In later phases, issues commands. |

Each repo owns its part of the contract. The three repos coordinate via this document.

## 3. Background: the storage model

The user's "library" can live in three independent places (the "three tiers" framing from the design discussion):

| Tier | Where | Writer | Visible to panel? |
|---|---|---|---|
| **1. On-disk inventory** (`saves_inventory.json`) | Local filesystem | `bsky-saves fetch` (CLI) | Yes (phase 3 — direct file read) |
| **2. GUI in-memory state** ("session" mode) | Browser tab's JS heap | GUI via helper-relayed fetches *or* Pyodide-fallback path | Only while the GUI is pushing (phase 1, with TTL — see §4.2) |
| **3. GUI persisted state** ("persist" mode) | Browser `localStorage` / IndexedDB / OPFS | Same | Yes (phase 1, persistent across helper restarts) |

For the typical installer user the library lives in tier 2 or 3 (they use the GUI; they don't run CLI commands). For the maintainer / power-user case the library may also live in tier 1, possibly multiple times for different handles. The phasing reflects which user-population each tier serves.

## 4. Phase 1 (MVP) — read-only status snapshot

**Goal:** the panel can display summary information about the user's library, derived from GUI-pushed status, persistable across helper restarts and panel-open sessions where the user has opted into persistence — and transient where the user has opted into session-only privacy.

### 4.1 Data flow

```
GUI                       Helper                            Panel (installer)
─── POST /status ─────►   ┌─────────────────────┐
                          │ in-memory snapshot  │  ◄── GET /status ─── (poll)
                          │     (always)        │
                          │                     │
                          │ persist mode:       │
                          │   coalesced flush ↓ │
                          └─────────────────────┘
                                   │
                                   ▼ (≤ 1/s, +priority:final, +shutdown)
                          <config_dir>/bsky-saves/status.json
                                   ▲
                              load on
                              helper startup
                              (persist mode only)
```

The helper is a state-cache proxy: it holds the latest status the GUI reported. In persist mode it mirrors that to a small file on disk so it survives helper restarts, with disk writes coalesced through a background flush task. In session mode it holds it in memory with a TTL that expires when the GUI's heartbeats stop. Either way, `GET /status` serves whatever the current state is.

### 4.2 Helper-side surface (`bsky-saves` repo)

**`POST /status`** — GUI pushes here.

- Auth: `Authorization: Bearer <token>` (same as every other credentialed endpoint).
- Request body: JSON; structure in §4.4. The helper validates basic shape (presence of required keys, types) and rejects with 400 on malformed.
- Response: `204 No Content` on accept.
- Concurrency: last-write-wins (see [R3](./installer-status-panel-resolved.md#r3--multiple-gui-sessions-on-one-helper)). The payload always carries `did` so per-DID indexing is a forward-compatible upgrade.
- **Mode-dependent storage** (the privacy-critical bit):
  - When `storage.mode === "persist"` (default), the helper updates its in-memory copy IMMEDIATELY on every push (so subsequent `GET /status` reflects the latest), and queues a flush to a coalesced background task. The background task atomic-writes the on-disk mirror at `<config_dir>/bsky-saves/status.json` at most once per second. If the incoming push has `priority: "final"` (§4.4), the coalescer is bypassed and the flush runs synchronously before returning 204. On helper shutdown (SIGTERM / Ctrl-C), any in-memory snapshot newer than the on-disk copy is synchronously flushed before exit. Tradeoff: up to ~1s of staleness on crash; acceptable for status (the contract doesn't promise crash-recovery freshness beyond the pre-push value). See [R8](./installer-status-panel-resolved.md#r8--persist-mode-disk-write-frequency).
  - When `storage.mode === "session"`, the helper updates its in-memory copy ONLY — no disk write, ever, even with `priority: "final"` — and applies a TTL whose value comes from the payload's `storage.session_ttl_seconds`. Each subsequent push from the same DID refreshes the TTL. When the TTL expires with no refresh, the helper drops the in-memory snapshot.
  - The two storage tiers (memory-session, disk-persist) are independent. A session-mode push does NOT overwrite a previously written persist-mode disk snapshot from a different sign-in. Implementer's note: a simple model is `{ memory_snapshot, memory_expires_at, disk_snapshot }`. Reads (`GET /status`) prefer unexpired memory, fall back to disk, return 404 if neither exists.

The mode-dependent split honors the GUI's persist-vs-session privacy contract: a session-mode user closes their browser expecting "this browser keeps no record"; the heartbeat-driven TTL ensures the helper drops the snapshot within ~60s of tab close, and nothing was ever written to disk.

**`GET /status`** — installer polls here.

- Auth: `Authorization: Bearer <token>`.
- Response: `200` with the most recent unexpired payload as JSON, or `404` if no status has ever been pushed (or the in-memory session-mode snapshot has expired and no persist-mode disk snapshot exists).
- Caching: helper holds the value in memory; reads are cheap. No need for ETag / conditional-GET; polling cadence is the rate limiter.

**`DELETE /status`** — GUI calls this on explicit clear ("Clear all data").

- Auth: `Authorization: Bearer <token>`.
- Effect: helper drops the in-memory snapshot for the calling DID AND removes the on-disk mirror file (if present). Subsequent `GET /status` returns 404 until the next push.
- Response: `204 No Content`.
- Rationale: sign-out alone is not a clear — the GUI preserves the user's local library state across sign-out (signing back in resumes the same library). Only the GUI's explicit "Settings → Clear all data" action invokes this endpoint. See [R5](./installer-status-panel-resolved.md#r5--clear-path-semantics) for the design rationale.

**`<config_dir>/bsky-saves/status.json`** — persistence mirror (persist mode only).

- Same directory as the token file (`status.json` sibling to `token`).
- File perms: `0600` (matches the token file's threat model — the status payload carries handle info worth keeping out of other users' reads).
- Written by the coalesced background flush task. Uses concurrency-safe per-write tmp names (not the broader inventory-writer's single-tmp scheme) since the background task is single-threaded by design — the per-write naming is defense in depth against a future contributor running the same writer from multiple threads.
- Loaded into the helper's in-memory cache on startup; if file missing, in-memory cache starts empty.
- NOT written in session mode. The file's presence reflects exactly one user history: a persist-mode push happened, the daemon now caches it across restarts.

### 4.3 GUI-side surface (`bsky-saves-gui` repo)

The GUI is responsible for **what** to push and **when**.

**Push triggers — REQUIRED:**

- After every successful fetch of saves (completion of the inventory delta).
- After each per-asset hydration phase finishes (threads, images, articles).
- On user toggling any of {threads, images, articles} on or off.
- On successful sign-in (initial snapshot carrying the new DID).
- On "Settings → Clear all data" — sends `DELETE /status` rather than a regular push.

**Push triggers — REQUIRED in session mode:**

- Idle heartbeat at 15s cadence (within the helper's 60s session TTL — see [R7](./installer-status-panel-resolved.md#r7--session-mode-heartbeat-cadence-and-ttl)). This keeps the helper's in-memory session-mode snapshot alive while the tab is open. In persist mode the heartbeat is optional (no TTL to keep alive).

**Push triggers — RECOMMENDED in persist mode:**

- On `beforeunload` (tab close, navigation away), send a final push with `priority: "final"` (§4.4) so the helper bypasses its coalescer and flushes the latest in-memory state to disk synchronously. Without this, up to ~1s of state can be lost on tab close. The push is sent via `navigator.sendBeacon()` or a `fetch()` with `keepalive: true` because the unload event doesn't reliably await async work otherwise. Session mode does NOT use this — disk is never written for session mode.

**Sign-out:**

- Stop the push loop. Do NOT send `DELETE /status`. The user's local library survives sign-out (per the GUI's existing persistence contract); the helper's snapshot should track that. For session mode, the helper's TTL naturally expires the snapshot within ~60s. For persist mode, the snapshot stays in place until a future "Clear all data" or new sign-in with a different DID.

**Account switch (sign out → sign in with different account):**

- Implicit. The next push from the new account carries a different `did`; single-slot last-write-wins overwrites the previous account's snapshot cleanly. No special handling in phase 1.

**Push rate limiting (debouncing):**

- The GUI batches/coalesces pushes so that no more than one push is in flight per 500ms (see [R6](./installer-status-panel-resolved.md#r6--push-debouncing-rate)). A burst of state changes during hydration (e.g., 10 images completed per second from the underlying Svelte store updates) generates at most ~2 pushes per second, with the most recent state always carried forward. The contract guarantees the helper won't be hit at JS-store-update rate; CLI implementation notes that the helper could handle tighter (250ms) without trouble if the GUI prefers snappier panel feedback during hydration bursts — GUI can tighten later without contract change.

**Failure handling:**

- A failed push (network error, helper down, 4xx/5xx) is non-fatal. The GUI logs at debug level (one line per failure burst, not per attempt) and continues. The next successful push overwrites whatever stale state the helper might be holding.
- A 401 with `WWW-Authenticate: Bearer` from `POST /status` is handled by the existing pairing-401 path (`markPairingStale` → re-pair UX). No special-casing for status pushes — they participate in the same auth model as `/fetch`, `/enrich`, etc.

**Pyodide-fallback mode (no helper, hosted PWA without a paired daemon):**

- Skip the push entirely. The panel — if anyone is viewing it from a previous paired session — shows whatever was last pushed, with a stale timestamp surfacing the staleness. See [R4](./installer-status-panel-resolved.md#r4--pyodide-fallback-mode).

### 4.4 Status payload shape (phase 1)

```json
{
  "schema_version": 1,
  "updated_at": "2026-05-17T20:15:00Z",
  "current_state": "idle",
  "priority": "final",
  "library": {
    "handle": "alice.bsky.social",
    "did": "did:plc:abc123…",
    "total_saves": 1247,
    "by_status": {
      "synced": 1230,
      "lost": 15,
      "unsaved": 2
    }
  },
  "hydration": {
    "articles": {"completed": 973, "total": 1247},
    "threads":  {"completed": 412, "total": 1247},
    "images":   {"completed": 856, "total": 1247}
  },
  "storage": {
    "mode": "persist",
    "session_ttl_seconds": null,
    "browser_bytes_estimate": 18234567
  },
  "last_activity": {
    "kind": "fetch",
    "started_at": "2026-05-17T20:13:11Z",
    "finished_at": "2026-05-17T20:15:00Z",
    "added": 3,
    "removed": 0,
    "errors": []
  }
}
```

Field-level notes:

- `schema_version` — integer; bumps on non-backward-compatible payload changes. The panel reads older schemas and degrades gracefully (display what it understands, ignore what it doesn't).
- `updated_at` — ISO-8601 UTC; helps the panel surface staleness when the GUI hasn't pushed recently.
- `current_state` — one of `"idle"`, `"refreshing"`, `"hydrating"`, `"error"`. **Authoritative in-flight indicator** (per [R11](./installer-status-panel-resolved.md#r11--semantics-of-last_activitykind-vs-current_state)): the panel reads `current_state` directly. Pre-fix GUI builds (≤ v0.6.5-rc.3) emitted `"idle"` here while hydration was mid-flight; v0.6.5-rc.4 and later push the correct value. The installer's progress-delta inference fallback (`hydration_is_progressing` + `_hydration_active_until`) was retired in `tenorune/bsky-saves-install@ec32356` — `current_state` is now the panel's sole in-flight signal. No `/ping`-based version gate was added: the installer has no external user base (internal dogfooding only), so pre-rc.4 GUI compatibility was not required. `"error"` means the most recent library-refresh attempt failed; details in `last_activity.errors`. Emission rules, stickiness, and rendering of `"error"` are still open (Q11).
- `priority` — optional top-level string; when set to `"final"` the helper bypasses its persist-mode flush coalescer and writes to disk synchronously before responding. Used by the GUI on `beforeunload` to ensure the last-known state lands on disk before tab close. Absent or any other value = treated as normal-priority (default coalesced flush). Session mode ignores this field entirely — session never writes to disk regardless. Extensible to other values (e.g., `"low"` for non-essential idle heartbeats) without a schema bump.
- `library` — minimal identity + counts. `did` is required from sign-in onward (drives last-write-wins single-slot today, per-DID indexing later). `by_status` mirrors the v0.6.0 retention categories. Present once the user is signed in and has a non-empty inventory; MAY be absent before sign-in or while the inventory is still loading (cold-start First Fetch). The helper-side validator accepts library-absent payloads from `bsky-saves==0.6.8` onward (see [R14](./installer-status-panel-resolved.md#r14--cold-start-library-absent-acceptance)); sub-field validation (`handle`, `did`, `total_saves`) still fires when the block IS present.
- `hydration` — per-feature completion. Each entry is `{completed, total}`. Optional sections; absent entries mean the GUI can't cheaply compute that metric.
- `storage.mode` — `"session"` or `"persist"`. Drives the helper's storage decision (§4.2). Required.
- `storage.session_ttl_seconds` — integer; required when `mode === "session"`, null/absent in persist mode. The TTL the helper applies to its in-memory snapshot before dropping. Locked at 60s (see [R7](./installer-status-panel-resolved.md#r7--session-mode-heartbeat-cadence-and-ttl)); future tuning is a payload-only change.
- `storage.browser_bytes_estimate` — `navigator.storage.estimate()` result if available; null otherwise. Informational; helps the panel show approximate disk footprint.
- `last_activity.kind` — `"fetch" | "hydrate_articles" | "hydrate_threads" | "hydrate_images" | "manual_refresh" | "idle"`. **Last completed operation** (per Q10): monotonically advances through real operations and never reverts to `"idle"` once anything has happened. `"idle"` is only valid as a fresh-install / post-clear sentinel (i.e., before any real operation, or after `DELETE /status`). The field expresses what happened, not what's happening now — use `current_state` for the latter. The panel renders e.g. "Last activity: fetch · 2 min ago · +3 / −0."
- `last_activity.errors` — array of `{kind: string, message: string, count: number}` objects. `kind` is a short stable identifier (e.g., `"pds_timeout"`, `"helper_504"`, `"thread_fetch_failed"`); `message` is human-readable; `count` is the multiplicity within this activity. Empty array means no errors. The panel can render counts and tooltip the messages.

Fields are optional except where noted; the GUI omits sections it can't cheaply compute. The panel renders only what's present.

### 4.5 Panel-side surface (`bsky-saves-install` repo)

The panel polls `GET /status` **only while the popover is visible**: one fetch immediately on popover show, then every 5 seconds while the popover remains visible, stopping on dismiss. The 5s cadence matches the panel's existing health-poll timer (the `Supervisor.is_alive()` + `/ping` check that drives the menu-bar state badge), so `/status` is co-fetched on the same tick — no second timer. 5s sits comfortably under [R7](./installer-status-panel-resolved.md#r7--session-mode-heartbeat-cadence-and-ttl)'s 15s heartbeat / 60s TTL window: the panel observes fresh pushes and session-mode-TTL-expiry 404 transitions both within ≤5s of the helper-side change. Polling is gated on visibility because the popover is closed most of the time, and a poll the user can't see is unobserved work that consumes battery on macOS / Windows idle. See [R10](./installer-status-panel-resolved.md#r10--installer-poll-cadence).

The panel authenticates with the same session token it already holds from pairing.

UI choices live entirely in the installer repo. Suggested defaults: counts as numerals, hydration as bar gauges, `updated_at` rendered as "12 min ago" relative time, `current_state === "refreshing"` as a small spinner.

When `GET /status` returns 404 — no snapshot yet, or session-mode snapshot expired — panel displays a placeholder ("No active library status — open the GUI and run a fetch") with a button that opens the bundled GUI URL.

**Staleness handling:** if `updated_at` is older than **5 minutes**, the panel renders the values with a subtle "last seen N min ago" indicator. 5 min sits well above session mode's 15s heartbeat cadence (an actively-pushing session-mode GUI will never trip this) and above any reasonable GUI-side push delay, while still flagging genuinely-stale persist-mode snapshots from long-idle users. The panel does NOT poll-with-backoff; the helper's TTL is the authoritative liveness signal for session mode, and persist-mode snapshots are expected to persist (the user is OK with the data lingering).

### 4.6 Authentication and trust

All three endpoints (`POST /status`, `GET /status`, `DELETE /status`) sit behind the existing `_check_token` middleware introduced in v0.6.2. Same `Authorization: Bearer <token>` semantics as `/fetch`, `/auth/check`, etc. Same `WWW-Authenticate` shaping on 401 (per v0.6.5's add) so the GUI's 401-interceptor handles them identically.

The trust boundary is unchanged: anyone who can read `<config_dir>/bsky-saves/token` can call these endpoints; same as today.

### 4.7 Security model — clear-text rationale

The status payload is **clear text at every layer**: in transit on loopback HTTP, in helper process memory, and in the on-disk mirror file (persist mode only). This is intentional. The trust model and the payload's sensitivity bound jointly justify it.

#### Layers and their protections

| Stage | Form | What protects it |
|---|---|---|
| GUI → helper | Plain HTTP `POST /status` body | Helper binds `127.0.0.1` only; Bearer auth from `_check_token` |
| Helper memory | Python dict in process heap | Standard same-user process isolation |
| Helper → disk (persist mode only) | JSON file at `<config_dir>/bsky-saves/status.json`, `0600` | File-system perms; same trust boundary as the token file |
| Helper → panel | Plain HTTP `GET /status` body | Same as GUI → helper |

#### Why each layer isn't encrypted

- **Wire is not HTTPS** because the helper binds loopback only. Loopback traffic never leaves the machine, so there's no off-machine MITM exposure. Adding HTTPS would require provisioning a self-signed cert that every consumer (GUI, panel, scripts using `bsky-saves token`) accepts; the threat model doesn't justify it. Other processes on the same machine running as the same user can sniff loopback traffic, but those same processes can also read the token file at `0600` and call the helper with full credentials — so loopback HTTPS doesn't add real defense.
- **Memory is not encrypted** because any same-user process can attach a debugger to any other same-user process. Encryption at rest in process memory is theater.
- **Disk is not encrypted** for the same reason the token file isn't: `0600` perms + same-user trust model. Encrypting it would require either OS-keychain integration (per-platform complexity) or a user-managed password, both of which exceed the value being protected.

#### What the payload MUST NOT contain

This is the load-bearing constraint that makes the above acceptable. Each new field is reviewed against this list at PR time:

- Any save's full post text, author identity beyond the user's own handle, URI, or attached media
- JWTs, app passwords, OAuth tokens, refresh tokens, or the pairing token itself
- Image bytes, image URLs containing dynamic-key tokens, or local file paths revealing the user's filesystem layout
- Per-save metadata (titles, dates, links, hashtags, replied-to identities)
- Search queries, follow graph, mute/block lists, or any social-graph data
- Anything that, if leaked, would compromise the user's account or their library's contents

The phase-1 payload (§4.4) contains only: the user's own public Bluesky identity (handle + DID — both publicly resolvable), aggregate counts, completion ratios, storage mode, and last-activity summary. Sensitivity floor: roughly "someone who reads this learns Alice has 1,247 saves, last fetched 12 min ago, 78% have article hydration." That's lower-sensitivity than what's already in the user's tier-1 inventory file (which CLI users keep on disk in clear text without comparable scrutiny).

#### Sensitivity check at PR time

Any PR that adds a field to the payload (whether in `bsky-saves-gui`'s push code or in this doc's payload schema) must include a one-sentence note answering: *"What does this field tell a reader, and is it in the MUST NOT list above?"* If unsure, default to omitting and reopen the question with the maintainer of `bsky-saves` before merging.

This check applies symmetrically to phase 2's command payloads when that work lands.

### 4.8 Startup-flow contract

On GUI activation (the rising edge of the activation gate defined in `bsky-saves-gui:app/src/lib/status-pusher.ts`), the GUI pushes its restored `last_activity` and derived `current_state` unconditionally. The helper REPLACES its on-disk and in-memory snapshot with this push payload; it MUST NOT attempt to preserve any portion of its prior snapshot during a GUI-startup push. The GUI restores its own `last_activity` from local browser storage (`idb-keyval` under `status-pusher:last-activity:v1`, since v0.6.5-rc.4); a browser-data wipe on the user's side intentionally resets this to no-history.

The "GUI startup wins, disk loses" direction is deliberate. The GUI is the only process that observes when operations actually start and finish; the helper just stores what the GUI told it. Merging the helper's on-disk snapshot back into the GUI's startup push (e.g., preserving an older `last_activity` because its `finished_at` is newer than anything the GUI restored from idb) would resurrect data the user explicitly cleared in the browser-wipe scenario, and would force both sides to encode timestamp-comparison logic. See [R13](./installer-status-panel-resolved.md#r13--gui-startup-snapshot-overwrite-vs-merge) for the design rationale.

This contract is invariant-tested on the helper side: a wholesale-replacement assertion lives alongside the `POST /status` tests in `bsky-saves:tests/test_status.py` to prevent a future maintainer from "helpfully" adding a field-merge step.

## 5. Phase 2 — commands from panel to GUI

Out of phase-1 scope. Sketch only — full design in a follow-up doc when phase 2 is on deck.

**Use cases:** refresh button, export library, backup-toggle changes (threads / articles / images on/off).

**Two candidate patterns:**

1. **Helper-held command queue, GUI polls.** Panel `POST /commands` writes into the queue; GUI periodically `GET /commands?since=<id>` pulls pending; GUI acks via `POST /commands/ack`. Simple; ~3–5s latency depending on poll cadence.
2. **Server-Sent Events from helper to GUI.** GUI opens `GET /commands/stream` once at startup; helper pushes commands via SSE; sub-second latency. Adds a long-lived connection to the helper. The browser's `EventSource` API handles reconnection automatically.

Phase-2 design will pick one (likely (1) first, escalate to (2) if UX requires it).

## 6. Phase 3 — CLI inventories

Also out of phase-1 scope. Sketch only.

For users who run `bsky-saves fetch` and have a tier-1 on-disk inventory (the maintainer's flow; some power users), the panel should be able to display its stats alongside or instead of GUI-pushed status.

Likely shape: the helper accepts an optional `--inventory <path>` flag at startup. When configured, `GET /status` returns a payload that includes both GUI-reported library state (if any) and on-disk inventory stats (if any). The panel UI shows them as separate cards.

Multi-handle / multi-inventory edge cases (the maintainer setup explicitly hits these) come into play here; phase-3 design will need to decide:

- Single configured path vs. list of paths the panel can switch between.
- How to disambiguate when the GUI's reported `did` differs from the configured-inventory's `did`.
- Whether the snapshot keying upgrades from single-slot to per-DID at this point (likely yes).

## 7. Open questions (phase 1)

Numbered for ease of reference. Answers go inline once locked; resolved items move to [`installer-status-panel-resolved.md`](./installer-status-panel-resolved.md) with a backlink from the section they inform.

Q10 (semantics of `last_activity.kind` vs `current_state`) resolved and moved to [R11](./installer-status-panel-resolved.md#r11--semantics-of-last_activitykind-vs-current_state). GUI fix shipped in v0.6.5-rc.4 and installer follow-ups landed on `claude/spec-installer-status-panel` in `tenorune/bsky-saves-install` (First Fetch `"refreshing"` render branch + retirement of the progress-delta inference fallback). All three R11 symptoms — mid-hydration `current_state`, post-restart `last_activity.kind`, and First Fetch blank-panel — verified resolved end-to-end against rc.4.

Q11 (semantics of `current_state === "error"`) resolved and moved to [R12](./installer-status-panel-resolved.md#r12--semantics-of-current_state--error). GUI emission scoped to library-refresh-level failures only (per-asset hydration failures stay in `last_activity.errors[]`); snapshot-bound stickiness with the helper persisting `"error"` like any other state; rendering and retry affordance deferred to the panel team. The GUI tab-reload caveat — an `"error"` snapshot on the helper can be silently overwritten with `"idle"` on the next GUI activation push — is documented as a known limitation of the current GUI emission model and a candidate for a future improvement (idb persistence of the refresh-error similar to #85's `last_activity` persistence).

Q12 (GUI-startup snapshot: overwrite vs merge) resolved and moved to [R13](./installer-status-panel-resolved.md#r13--gui-startup-snapshot-overwrite-vs-merge). Locked **overwrite** — post-#85 the GUI's startup push carries `last_activity` restored from `idb-keyval`, so overwrite preserves the user's last-known state rather than clobbering it with empty defaults. Contract codified as new [§4.8](#48-startup-flow-contract); helper-side wholesale-replacement invariant pinned by a test in `bsky-saves:tests/test_status.py`.

Q13 (initial-fetch in-flight push: missing `"refreshing"` signal before any library data exists) resolved and moved to [R14](./installer-status-panel-resolved.md#r14--cold-start-library-absent-acceptance). GUI investigation revealed the root cause was not a missing push trigger but a contract violation on the CLI side — the helper rejected `library`-absent payloads with `HTTP 400` despite §4.4's prose allowing the block to be absent before non-empty inventory exists, and the GUI's `pushOnce` silently swallowed the 400. Fix is paired:

- **GUI `bsky-saves-gui` v0.6.5**: `buildStatusPayload` omits the `library` block entirely when `inventoryState.status !== 'ready'` (commit `31486ff`).
- **CLI `bsky-saves` v0.6.8**: `_validate_status_payload` treats `library` as optional; sub-field validation (`handle`, `did`, `total_saves`) fires only when the block is present. Pinned by `tests/test_serve.py::test_post_status_204_when_library_absent` + the wholesale-replacement variant against library-present prior snapshots.

End-to-end cold-start UX validated by the installer team against the paired `bsky-saves==0.6.8` + `bsky-saves-gui` v0.6.5 release. The pre-existing `_render_library_section` "no library identified → Fetching library…" branch (shipped during the R11 closure) renders correctly throughout the cold-start window now that the snapshot actually lands on the helper.

## 8. Maintenance

This document is the cross-repo contract. Any of the following changes should be accompanied by a PR updating this doc:

- Adding / removing fields from the status payload.
- Changing the auth shape, endpoint paths, or response codes.
- Bumping `schema_version`.
- Moving items between phases.
- Resolving the open questions in §7.

Reviewers ideally include one maintainer from each affected repo.

The doc lives in `bsky-saves-coordination` (a neutral fourth repo) because the contract is symmetric across the three primary repos; no single team's repo should host it.

When the design changes substantively (e.g., adopting phase 2's command flow), branch this doc into a phase-2 doc rather than retrofitting the phase-1 contract.

## 9. Changelog

| Date | Author | Summary |
|---|---|---|
| 2026-05-17 | CLI | Initial draft (§§1–8 + Appendices A–B). Open questions Q1–Q4 surfaced. |
| 2026-05-18 | GUI | Session-mode privacy: added mode-dependent storage to §4.2 (memory-only + TTL for session, atomic disk write for persist). Added `current_state` field and `storage.session_ttl_seconds` to §4.4 payload. Clarified `last_activity.errors` shape. Made §4.3 push triggers explicit (required vs. mode-required). Added `DELETE /status` endpoint to §4.2 for explicit "Clear all data" path; clarified that sign-out is NOT a clear. Documented push debouncing floor. Raised Q5–Q8. Resolved Q1–Q4 in body; resolved appendix seeded with R1–R5 (companion file NOT in this PR — addressed below). |
| 2026-05-18 | CLI | Answered Q5 (concurs with 500ms floor), Q6 (concurs with 60s/15s), Q7 (proposes coalesced background flush ≤1/s, with `priority: "final"` and shutdown-synchronous exceptions). Restored §4.7 security model (clear-text rationale + MUST-NOT list + sensitivity check at PR time) — drafted on a primary-repo branch that wasn't included in the GUI revision's basis. Noted in §4.2 that `<config_dir>/bsky-saves/status.json` write path may use concurrency-safe per-write tmp names if Q7 resolves on coalesced writes. Raised Q9 re: missing `installer-status-panel-resolved.md` companion file (R3/R4/R5 backlinks 404 against coord repo's main). No body content changed beyond §4.7 restoration; Q7's implied §4.2 body update held until the question resolves. |
| 2026-05-20 | GUI | Resolved Q5 in §4.3 (500ms floor locked, with note that GUI may tighten to 250ms later without contract change). Resolved Q6 in §4.3 (15s heartbeat / 60s TTL locked). Resolved Q7 in §4.2 (adopts CLI's coalesced background flush proposal; persist-mode in-memory updates immediately, disk flush ≤ 1/s, `priority: "final"` bypass via `navigator.sendBeacon` on `beforeunload`, shutdown-synchronous flush). Added `priority` optional top-level string field to §4.4 payload (string-enum for forward compat: `"final"` is the only recognized non-default value today). Added "RECOMMENDED in persist mode" trigger to §4.3 covering the beforeunload final push. Resolved Q9 by including `installer-status-panel-resolved.md` in this same PR (workflow now supports multi-file manifests). Moved Q5/Q6/Q7/Q9 to appendix as R6/R7/R8/R9. Q8 (installer poll cadence) remains open. |
| 2026-05-21 | Installer | Resolved Q8 in §4.5: visibility-gated polling — one fetch on popover show, every 5s while the popover is visible, no polling while closed. Co-fetches `/status` on the same 5s timer that already drives the menu-bar state badge (no second timer, no extra wake-ups). Pinned the staleness-indicator threshold to 5 minutes (was "e.g., 5 minutes" suggestion). No payload, endpoint, auth, or schema changes. Moved Q8 to appendix as R10. §7 now empty (all phase-1 questions resolved). Updated Appendix A to check off the polling-cadence item. |
| 2026-05-22 | Installer | Raised Q10: `last_activity.kind` semantics. Observed in v0.4.0 RC testing against `bsky-saves==0.6.8rc1` — the GUI emits `last_activity.kind = "idle"` between activity transitions and during steady-state, which (a) makes in-flight state inference unreliable from the panel side (currently mitigated by tracking hydration-progress deltas across polls with an 8s grace window), and (b) loses last-activity context on installer restart (persisted disk snapshot has `kind="idle"`, panel renders no last-activity line). Proposes clarifying that `last_activity.kind` is the last *completed* operation (never reverts to `"idle"` once anything has happened) and `current_state` is the right-now field. Awaiting GUI team review. No body content changes in this PR. |
| 2026-05-22 | CLI | Resolved Q10 in §7 with GUI confirmation (tenorune/bsky-saves-gui#85): both behaviors confirmed as GUI-side bugs (root causes in `deriveCurrentState` and `currentActivity` startup default); proposed semantics adopted verbatim. Tightened `current_state` and `last_activity.kind` field notes in §4.4 (semantics, not enums — `"idle"` retained as fresh-install / post-clear sentinel for `last_activity.kind`). Captured GUI team's panel-side follow-up (verify `current_state === "refreshing"` render branch) and the optional installer-side cleanup gating note in Q10's resolution. Raised Q11 (`"error"` semantics — emission/stickiness/persistence/rendering) and Q12 (GUI-startup snapshot overwrite vs merge contract) in §7. |
| 2026-05-22 | GUI | Q10 fix shipped: tenorune/bsky-saves-gui#85 merged to main, released in v0.6.5-rc.4. Moved Q10 from §7 to appendix as R11 with implementation status (mid-hydration + post-restart symptoms verified resolved against rc.4; First Fetch blank-panel symptom awaits the installer's `current_state === "refreshing"` render branch). Corrected the §4.4 `current_state` field note's pre-fix version reference (≤ v0.6.5-rc.3, not ≤ rc.4 — rc.4 is the first build with the fix). Answered Q11 with GUI-proposed resolution: emission scoped to library-refresh-level failures only (per-asset hydration failures stay in `last_activity.errors[]`); snapshot-bound stickiness with helper persisting `"error"` like any other state; rendering/retry deferred to panel team; called out the GUI-tab-reload caveat (current GUI doesn't re-emit `"error"` after reload, so a snapshot can be silently overwritten with `"idle"` — known limitation, candidate for a future improvement). Answered Q12 with GUI-proposed resolution: keep overwrite (status quo, post-#85). Proposed contract text for the startup-flow contract; suggested placement in §4.4 or a new §4.8. Q11 and Q12 both status: proposed-by-GUI, awaiting CLI acceptance. No payload-shape changes in this revision. |
| 2026-05-22 | Installer | Closed R11 end-to-end. Shipped the `current_state === "refreshing"` render branch on `claude/spec-installer-status-panel` (`bsky-saves-install`): placeholder headline reads "Fetching library…" pre-handle, last-activity row reads "Refreshing…" / "Backing up…" inline once a library is identified (commit `73e035e`). Retired the progress-delta inference fallback (`status.hydration_is_progressing`, `StatusPopover._hydration_active_until`, the delta-detection branch in `update_library`, and associated tests) — panel now reads `snap.current_state` directly (commit `ec32356`). No `/ping`-based `gui_bundled` gate added: no external installer user base, internal dogfooding only. Updated R11 verification + §4.4 `current_state` field note + §3 status header to reflect closure. No payload, endpoint, auth, or schema changes. |
| 2026-05-22 | CLI | Accepted GUI's Q11 and Q12 resolutions; moved Q11 → [R12](./installer-status-panel-resolved.md#r12--semantics-of-current_state--error) and Q12 → [R13](./installer-status-panel-resolved.md#r13--gui-startup-snapshot-overwrite-vs-merge). Added §4.8 (Startup-flow contract) with the GUI's proposed contract text describing the helper's wholesale-replacement obligation on GUI activation pushes. Cross-referenced the helper-side invariant test landing on `bsky-saves:tests/test_status.py` (separate commit on `tenorune/bsky-saves@main`). No payload-shape changes; §4.4 unchanged in this revision. |
| 2026-05-23 | Installer | Raised Q13: cold-start First Fetch in-flight push gap. Observed in v0.4.0 RC testing on a fresh pairing — the helper has no persisted snapshot, the GUI doesn't push any in-flight state during the initial fetch, and the panel polls `GET /status` to a 404 (`{"error": "no status snapshot"}`) throughout the entire fetch duration. R11's panel-side render branch is in place and verified working on manual-refresh flows, but it has nothing to render in the cold-start window because the GUI hasn't pushed any `current_state` yet. Proposes a single push at the start of the initial fetch with `library.handle: null` + `current_state: "refreshing"` so the existing panel branch can render "Fetching library…" during the cold-start window. No payload-shape change; just an added push trigger in §4.3. Awaiting GUI team review. Updated §3 status header. |
| 2026-05-23 | GUI | Investigated Q13 against `bsky-saves==0.6.8rc2` + `bsky-saves-gui` v0.6.5-rc.4. Root cause is **not** a missing push trigger: the GUI was already pushing at sign-in (activation rising edge) and at libraryRefreshState transition, but every push during cold-start was rejected by the helper with `HTTP 400 {"error": "missing or invalid field: library.total_saves"}` and silently swallowed by the GUI's push-once `try/catch`. Verified via curl. **GUI fix landing in v0.6.5** (commit `31486ff` on `claude/bsky-saves-gui-work-5VmfX`): `buildStatusPayload` omits the `library` block entirely when `inventoryState.status !== 'ready'`, per §4.4's existing prose ("always present once signed in AND has non-empty inventory"). An earlier attempt to ship a `total_saves: 0` placeholder (commit `e33a87a`) was reverted — it codified a shape the contract doesn't sanction, misrepresented state on the wire, and would have forced a second walk-back once the helper aligned. **Ask to CLI team raised inline in Q13:** relax the helper to accept payloads with `library` absent, per §4.4's prose. Target `bsky-saves==0.6.8rc3`. Helper currently rejects absent `library` with `HTTP 400 {"error": "missing field: library"}` — itself a §4.4 contract violation. Until paired, cold-start panel UX remains "No active library status yet." for the duration of the fetch (no regression vs. today; the helper-rejected pushes drop on the floor either way). Once paired, the installer's existing "no library identified" render branch handles cold-start as Q13 asked. Also flagged a separate GUI-side hardening item (silent-swallow of non-401 push failures — tenorune/bsky-saves-gui#88; tracked outside the coord doc, not blocking). No payload-shape change on either side. |
| 2026-05-23 | CLI | Resolved Q13 in §7 and moved to [R14](./installer-status-panel-resolved.md#r14--cold-start-library-absent-acceptance). Shipped `library`-optional relaxation in `_validate_status_payload` per the GUI ask — sub-field validation (`handle`, `did`, `total_saves`) still fires when the block is present. Pinned by `tests/test_serve.py::test_post_status_204_when_library_absent` + a wholesale-replacement variant verifying library-absent pushes cleanly overwrite library-present prior snapshots. Tightened §4.4 `library` prose to make the optionality explicit ("MAY be absent before sign-in or while inventory is still loading"), with a forward-reference to R14 for the implementation history. Shipped first in `bsky-saves==0.6.8rc3` and validated end-to-end with the installer; then bundled into `bsky-saves==0.6.8` (final) paired with `bsky-saves-gui` v0.6.5 (final). Updated §3 status header to reflect phase-1 contract closure. Phase 1 closes for the trio with all of Q1–Q13 archived as R1–R14. No payload-shape changes. |

---

## Appendix A — Decisions still to make before implementation starts

A condensed checklist for whoever drives phase 1 to ground. None of these are open design questions; they're sequencing-and-ownership decisions.

- [ ] Confirm helper-side endpoints (`POST /status`, `GET /status`, `DELETE /status`) and persistence path with the `bsky-saves` maintainer.
- [ ] GUI team commits to the §4.4 payload shape (final shape locks once §7 is empty and all teams have confirmed their slice — §7 holds Q11/Q12, both currently proposed-by-GUI awaiting CLI acceptance, no payload-shape changes in either).
- [x] Installer team confirms polling cadence (Q8 resolved 2026-05-21 — see [R10](./installer-status-panel-resolved.md#r10--installer-poll-cadence)). UI rendering pass pending implementation in `bsky-saves-install`.
- [x] Resolved-questions companion file (`installer-status-panel-resolved.md`) seeded and present at coord repo's `main` (closed by GUI 2026-05-20 — see [R9](./installer-status-panel-resolved.md#r9--resolved-questions-archive-companion-file-missing)).
- [ ] Spec docs open in each primary repo (`docs/superpowers/specs/YYYY-MM-DD-status-snapshot.md` per the project convention); plan docs follow; implementation goes through the existing subagent-driven-development flow.
- [ ] Coordinated release: helper version that ships the endpoints, GUI version that ships the push call, installer version that ships the panel. All three pinned together in the installer's bundle.

## Appendix B — Glossary

- **CLI** — the `bsky-saves` command and its subcommands (`fetch`, `hydrate`, `enrich`, `serve`, `token`).
- **Helper** — the long-running HTTP daemon started by the `bsky-saves serve` CLI subcommand. Listens on `127.0.0.1:47826`.
- **GUI** — the `bsky-saves-gui` Svelte/Vite static web app, distributed both bundled into the `bsky-saves` wheel and hosted at `https://saves.lightseed.net`.
- **Panel** — the status UI in the `bsky-saves-install` native menu-bar app.
- **Library** — the user's collection of bookmarked saves, regardless of which storage tier holds it.
- **Status / status payload** — the JSON object the GUI pushes to the helper to describe library state for panel consumption. Defined in §4.4.
- **Persist mode / session mode** — the user's privacy choice at sign-in. Persist: data survives browser quit (IndexedDB / disk). Session: data wiped at tab close (sessionStorage / memory only). The helper's storage behavior in §4.2 mirrors this distinction.
- **Priority hint** — the optional `priority` field in the §4.4 payload. `"final"` instructs the helper to bypass its persist-mode flush coalescer and write to disk synchronously before responding. Used by the GUI on `beforeunload` so terminal state lands on disk before tab close.
