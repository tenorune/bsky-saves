# Installer status panel — cross-repo coordination doc

> **Status:** drafting (2026-05-21). Installer revision: Q8 (poll cadence) resolved in §4.5 — visibility-gated polling at 5s while the popover is visible. Staleness-indicator threshold pinned at 5 minutes. No payload, endpoint, or auth changes. §7 has no remaining open questions.
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
- `current_state` — one of `"idle"`, `"refreshing"`, `"hydrating"`, `"error"`. **Authoritative in-flight indicator** (per Q10): the panel reads `current_state` directly. Pre-Q10 GUI builds (≤ v0.6.5-rc.4) emit `"idle"` here while hydration is mid-flight; the installer's existing progress-delta inference (`hydration_is_progressing` + `_hydration_active_until` in `bsky_saves_launcher.popover`) is the fallback for those builds, and remains in place until the installer chooses to retire it. `"error"` means the most recent activity failed; details in `last_activity.errors`. Emission rules, stickiness, and rendering of `"error"` are still open (Q11).
- `priority` — optional top-level string; when set to `"final"` the helper bypasses its persist-mode flush coalescer and writes to disk synchronously before responding. Used by the GUI on `beforeunload` to ensure the last-known state lands on disk before tab close. Absent or any other value = treated as normal-priority (default coalesced flush). Session mode ignores this field entirely — session never writes to disk regardless. Extensible to other values (e.g., `"low"` for non-essential idle heartbeats) without a schema bump.
- `library` — minimal identity + counts. `did` is required from sign-in onward (drives last-write-wins single-slot today, per-DID indexing later). `by_status` mirrors the v0.6.0 retention categories. Always present once the user is signed in and has a non-empty inventory.
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

**Q10 — Semantics of `last_activity.kind` vs `current_state`** *(raised by Installer 2026-05-22 for GUI team)*. Empirically the installer's panel observes `last_activity.kind = "idle"` between activity transitions (e.g., between fetch completion and hydration start; between consecutive hydration phases; during steady-state). At the same time, the GUI doesn't always set `current_state = "hydrating"` while hydration is active. This produces two related panel-side problems:

1. **In-flight detection is unreliable.** The panel can't tell the GUI is mid-work from `current_state` alone, so it infers activity from observed hydration-progress deltas between polls (8s grace window after the last increase). Works, but means up to 8s of stale "Backing up…" persistence after hydration actually ends.

2. **Restart loses last-activity context.** When the installer restarts, the helper rebinds and loads its persisted disk snapshot (persist mode). If the most recent GUI push had `last_activity.kind = "idle"`, the panel renders no last-activity line at all on restart — even though a real activity happened minutes ago.

Proposed contract clarification, for the GUI team to confirm:

- **`last_activity.kind`** = the last *completed* operation. Values: `"fetch" | "hydrate_articles" | "hydrate_threads" | "hydrate_images" | "manual_refresh"`. Monotonically advances through real operations; never reverts to `"idle"` after the first real operation (only resets to `"idle"` on `DELETE /status` / "Clear all data").
- **`current_state`** = what the GUI is doing *right now*. Values: `"idle" | "refreshing" | "hydrating" | "error"`. This is the field that flips around during transitions; `"idle"` here is meaningful and expected during steady-state.

The contract today (§4.4) lists `"idle"` as a valid `last_activity.kind`. Either keep that and clarify it's only for the fresh-install / post-clear case, or remove `"idle"` from the kind enum entirely and have the panel treat the field as required-once-an-operation-has-happened.

Either resolution closes both panel-side problems: in-flight state stays canonical in `current_state`, and post-restart the persisted `last_activity` always has something meaningful to display.

Installer-side: when the GUI lands the fix, the installer can retire its progress-delta inference (`hydration_is_progressing` + `_hydration_active_until` in `bsky_saves_launcher.popover`) and read `current_state` directly. No installer release needs to ship with the GUI change for this to work — the installer already handles both contract shapes.

> **GUI response (2026-05-22):** confirms both behaviors are real bugs in the GUI's status-payload code, not panel-side perception. Also independently observed a third symptom during v0.6.5-rc.3 testing — panel renders blank during initial "First fetch in progress…" — same root cause as problem (1).
>
> Root causes:
>
> 1. **`current_state` ignores hydration stores.** `deriveCurrentState` only reads `libraryRefreshState` and `fetchProgress`; the three hydration stores (image / article / thread) aren't consulted. `current_state` drops to `"idle"` while hydration is still mid-flight.
> 2. **`last_activity.kind` clobbered on GUI startup.** The pusher's in-memory `currentActivity` initializes to `{ kind: "idle", ... }`. Watcher transitions fire only on activity edges and don't revert — but the initial value *is* idle, and the activation rising-edge "fresh-state" push at GUI startup overwrites the helper's on-disk snapshot with that default.
>
> Adopts the installer's proposed semantics verbatim. Tracked in [tenorune/bsky-saves-gui#85](https://github.com/tenorune/bsky-saves-gui/issues/85); fix ships in the next GUI RC.
>
> Coordination follow-up flagged by GUI team: verify the panel has a render branch for `current_state === "refreshing"` (not just `"hydrating"`); otherwise First Fetch will continue to render blank even after the GUI fix lands.

**Resolution (2026-05-22).** Semantics locked as proposed; `current_state` and `last_activity.kind` field notes in §4.4 updated accordingly. Two follow-ups land out-of-band:

- **Panel: handle `current_state === "refreshing"`.** Required action on the installer side before claiming the Q10 fix works end-to-end. Tracking issue to be filed on `bsky-saves-install`.
- **Installer cleanup (optional).** Retiring the progress-delta inference is a maintenance choice, not a gating requirement, since the installer already handles both contract shapes. If retired, gate on `gui_bundled` from `/ping` to keep older GUI pairings working.

Q10 closes for semantics; this entry moves to [`installer-status-panel-resolved.md`](./installer-status-panel-resolved.md) once the GUI fix ships. Status: resolved-by-GUI.

**Q11 — Semantics of `current_state === "error"`** *(raised by CLI 2026-05-22, derived from Q10 resolution)*. The Q10 resolution enumerates `"error"` as a `current_state` value, but neither side has agreed on emission, stickiness, or rendering rules. Open sub-questions:

- **Emission.** Which failures trigger `"error"` — auth failure, network failure, partial-hydration failure, persistence failure, all of the above?
- **Stickiness.** Does `"error"` persist until cleared by an explicit user action / next successful operation, or auto-clear after a transient failure?
- **Persistence behavior.** Does the helper mirror an `"error"` `current_state` to disk like other states, or only persist the last successful snapshot? If persisted, rehydration on restart re-renders the error — desired or surprising?
- **Panel rendering.** Toast, persistent banner, badge on the activity row, or all of the above? Is there a "retry" affordance, and if so what does the panel dispatch back to the GUI?

Required before any RC that emits `current_state === "error"` in anger. Status: proposed-by-CLI.

**Q12 — GUI-startup snapshot: overwrite vs merge** *(raised by CLI 2026-05-22, derived from Q10 Bug 2 root cause)*. Q10's root cause for Bug 2 surfaces a separate question independent of the kind/state semantics: every GUI startup currently clobbers the helper's on-disk snapshot with the GUI's just-initialized in-memory state. After the Q10 fix the clobbered value will at least be accurate — but the underlying "GUI startup wins, disk loses" contract isn't explicit anywhere in §4.4 or §6.

Options:

- **Overwrite (status quo, post-fix).** GUI startup push always wins. Simple. Implies the GUI's just-started in-memory state is always authoritative — fine if the GUI restores its own state quickly on startup.
- **Merge.** GUI startup push respects on-disk `last_activity` if its `finished_at` is newer than anything the GUI has in memory. Safer against future regressions where the GUI's startup state momentarily lags. More complex; requires a timestamp comparison and conflict rule.

Either is defensible. The choice should be documented in §4.4 or §6 (whichever section governs the startup-flow contract). Picking now avoids re-litigating after the next surprise. Status: proposed-by-CLI.

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

---

## Appendix A — Decisions still to make before implementation starts

A condensed checklist for whoever drives phase 1 to ground. None of these are open design questions; they're sequencing-and-ownership decisions.

- [ ] Confirm helper-side endpoints (`POST /status`, `GET /status`, `DELETE /status`) and persistence path with the `bsky-saves` maintainer.
- [ ] GUI team commits to the §4.4 payload shape (final shape locks once §7 is empty and all teams have confirmed their slice — §7 is now empty; awaiting GUI confirmation).
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
