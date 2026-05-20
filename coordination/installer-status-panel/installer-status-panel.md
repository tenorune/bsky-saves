# Installer status panel — cross-repo coordination doc

> **Status:** drafting (2026-05-18). GUI revision: session-mode TTL + clear-path correction + `current_state` field + heartbeat/debounce requirements. CLI revision: answers to Q5–Q7, restored §4.7 security model, raised Q9. Awaiting installer review.
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
| `bsky-saves` | The helper daemon. New `POST /status`, `GET /status`, and `DELETE /status` endpoints in phase 1; the on-disk status-cache file for persist mode; an in-memory TTL slot for session mode; auth gating identical to other credentialed endpoints. |
| `bsky-saves-gui` | Pushing summary library stats to the helper at meaningful moments. Owns the payload contents (§4.4), the push trigger list (§4.3), the session-mode heartbeat (§4.3), and how library state is computed. |
| `bsky-saves-install` | The status panel UI. Polls `GET /status` and renders. Distinguishes "no snapshot yet", "active snapshot", and "stale snapshot" (where `updated_at` is older than a UI-defined threshold). In later phases, issues commands. |

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
                          │                     │
                          │ persist mode:       │
                          │   atomic-write ↓    │
                          └─────────────────────┘
                                   │
                                   ▼
                          <config_dir>/bsky-saves/status.json
                                   ▲
                              load on
                              helper startup
                              (persist mode only)
```

The helper is a state-cache proxy: it holds the latest status the GUI reported. In persist mode it mirrors that to a small file on disk so it survives helper restarts. In session mode it holds it in memory with a TTL that expires when the GUI's heartbeats stop. Either way, `GET /status` serves whatever the current state is.

### 4.2 Helper-side surface (`bsky-saves` repo)

**`POST /status`** — GUI pushes here.

- Auth: `Authorization: Bearer <token>` (same as every other credentialed endpoint).
- Request body: JSON; structure in §4.4. The helper validates basic shape (presence of required keys, types) and rejects with 400 on malformed.
- Response: `204 No Content` on accept.
- Concurrency: last-write-wins (see [R3](./installer-status-panel-resolved.md#r3--multiple-gui-sessions-on-one-helper)). The payload always carries `did` so per-DID indexing is a forward-compatible upgrade.
- **Mode-dependent storage** (the privacy-critical bit):
  - When `storage.mode === "persist"` (default), the helper updates its in-memory copy AND atomic-writes the on-disk mirror at `<config_dir>/bsky-saves/status.json`. Snapshot survives helper restart (loaded into memory on startup). Disk-write frequency is governed by Q7 (under proposal: coalesce ≤1/s).
  - When `storage.mode === "session"`, the helper updates its in-memory copy ONLY — no disk write — and applies a TTL whose value comes from the payload's `storage.session_ttl_seconds`. Each subsequent push from the same DID refreshes the TTL. When the TTL expires with no refresh, the helper drops the in-memory snapshot.
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
- Written via the existing `atomic_write_inventory` pattern (temp + `os.replace`), with concurrency-safe per-write tmp names if Q7 closes on "coalesced single-flighted background flush" rather than per-push writes.
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

- Idle heartbeat at a cadence shorter than the helper's session TTL (e.g., heartbeat every 15s when TTL is 60s — see Q6). This keeps the helper's in-memory session-mode snapshot alive while the tab is open. In persist mode the heartbeat is optional (no TTL to keep alive).

**Sign-out:**

- Stop the push loop. Do NOT send `DELETE /status`. The user's local library survives sign-out (per the GUI's existing persistence contract); the helper's snapshot should track that. For session mode, the helper's TTL naturally expires the snapshot within ~60s. For persist mode, the snapshot stays in place until a future "Clear all data" or new sign-in with a different DID.

**Account switch (sign out → sign in with different account):**

- Implicit. The next push from the new account carries a different `did`; single-slot last-write-wins overwrites the previous account's snapshot cleanly. No special handling in phase 1.

**Push rate limiting (debouncing):**

- The GUI batches/coalesces pushes so that no more than one push is in flight per ~500ms (proposed; see Q5). A burst of state changes during hydration (e.g., 10 images completed per second from the underlying Svelte store updates) generates at most ~2 pushes per second, with the most recent state always carried forward. The cadence floor is a GUI implementation detail; the contract guarantees the helper won't be hit at JS-store-update rate.

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
- `current_state` — one of `"idle"`, `"refreshing"`, `"hydrating"`, `"error"`. The panel uses this to show a live spinner during in-flight work without having to infer from `last_activity.finished_at`. `"error"` means the most recent activity failed; details in `last_activity.errors`.
- `library` — minimal identity + counts. `did` is required from sign-in onward (drives last-write-wins single-slot today, per-DID indexing later). `by_status` mirrors the v0.6.0 retention categories. Always present once the user is signed in and has a non-empty inventory.
- `hydration` — per-feature completion. Each entry is `{completed, total}`. Optional sections; absent entries mean the GUI can't cheaply compute that metric.
- `storage.mode` — `"session"` or `"persist"`. Drives the helper's storage decision (§4.2). Required.
- `storage.session_ttl_seconds` — integer; required when `mode === "session"`, null/absent in persist mode. The TTL the helper applies to its in-memory snapshot before dropping. Typical: 60s.
- `storage.browser_bytes_estimate` — `navigator.storage.estimate()` result if available; null otherwise. Informational; helps the panel show approximate disk footprint.
- `last_activity.kind` — `"fetch" | "hydrate_articles" | "hydrate_threads" | "hydrate_images" | "manual_refresh" | "idle"`. The panel renders e.g. "Last activity: fetch · 2 min ago · +3 / −0."
- `last_activity.errors` — array of `{kind: string, message: string, count: number}` objects. `kind` is a short stable identifier (e.g., `"pds_timeout"`, `"helper_504"`, `"thread_fetch_failed"`); `message` is human-readable; `count` is the multiplicity within this activity. Empty array means no errors. The panel can render counts and tooltip the messages.

Fields are optional except where noted; the GUI omits sections it can't cheaply compute. The panel renders only what's present.

### 4.5 Panel-side surface (`bsky-saves-install` repo)

The panel polls `GET /status` at a low rate while open (≤ once every 5 seconds is plenty given humans don't watch dashboards updating at sub-second granularity — final cadence determined by Q8), authenticates with the same session token it already holds from pairing, renders the fields.

UI choices live entirely in the installer repo. Suggested defaults: counts as numerals, hydration as bar gauges, `updated_at` rendered as "12 min ago" relative time, `current_state === "refreshing"` as a small spinner.

When `GET /status` returns 404 — no snapshot yet, or session-mode snapshot expired — panel displays a placeholder ("No active library status — open the GUI and run a fetch") with a button that opens the bundled GUI URL.

Staleness handling: if `updated_at` is older than a panel-defined threshold (e.g., 5 minutes), the panel renders the values with a subtle "last seen N min ago" indicator. The panel does NOT poll-with-backoff; the helper's TTL is the authoritative liveness signal for session mode, and persist-mode snapshots are expected to persist (the user is OK with the data lingering).

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

**Q5 — Push debouncing rate** *(raised by GUI 2026-05-18)*. GUI proposes max one push per 500ms (see §4.3). CLI team: does this match what the helper's HTTP stack can comfortably handle, or should the floor be tighter (250ms / 1s)? If 500ms is fine, this gets folded into §4.3 as the canonical floor.

> **CLI response (2026-05-18):** 500ms is comfortable for the helper. `ThreadingHTTPServer` handles each request on its own thread; the helper's per-request work for `POST /status` is JSON parse + dict assignment + (in persist mode) a queued disk write — all sub-millisecond. The helper could handle pushes at 10–100/sec without breaking; 500ms is generous. **Concurs with the GUI's proposal**; would not object to tighter (250ms) if the GUI wants snappier panel feedback during hydration bursts. Status: proposed-by-CLI (concurs).

**Q6 — Session-mode heartbeat cadence and TTL** *(raised by GUI 2026-05-18)*. GUI proposes 15s heartbeat / 60s TTL (4× safety margin against a single missed push). CLI team: any preference on the TTL value? Longer TTL = more tolerance for network blips; shorter = quicker cleanup after tab close. Whatever's chosen, the heartbeat should be ≤ TTL/3 to survive a missed push.

> **CLI response (2026-05-18):** 60s TTL / 15s heartbeat is fine. The helper-side implementation is lazy expiry — compare `now()` against `memory_expires_at` on each `GET /status`; no per-second tick, no background timer. Cheap regardless of TTL value. **Concurs with the GUI's proposal**; the panel UX, not the helper, is the constraint that should anchor the number, and 60s drops within "tab probably closed" intuition. Status: proposed-by-CLI (concurs).

**Q7 — Persist-mode disk-write frequency** *(raised by GUI 2026-05-18)*. Per §4.2, the helper atomic-writes to disk on every persist-mode push. For a heavy hydration phase (~2 pushes/second from §4.3 debouncer) that's a lot of disk writes. Is this OK, or does the helper want to coalesce writes (e.g., flush at most once per second, with a final write triggered by a `priority: "final"` hint the GUI can send on `beforeunload`)?

> **CLI response (2026-05-18):** **Coalesce.** Concrete proposal:
>
> - In-memory snapshot updates immediately on every `POST /status` (so `GET /status` always sees the latest).
> - Disk flush happens at most once per second via a debounced background task that observes the in-memory snapshot.
> - GUI can send `priority: "final"` (a new optional top-level boolean in the payload) to bypass the coalescer and flush immediately — useful for `beforeunload` cases where the GUI knows it's the last push of the session.
> - Helper shutdown (SIGTERM, Ctrl-C) synchronously flushes the latest in-memory snapshot to disk before exiting.
>
> Rationale on the helper side: not a perf concern (an atomic write on macOS SSD is ~1–5ms; 7,200 writes/hour during sustained hydration is trivial), but the existing `atomic_write_inventory` pattern uses a single tmp filename (`path.with_suffix(".tmp")`) which races under concurrent writes from multiple `ThreadingHTTPServer` worker threads. Coalescing through a single background flush task sidesteps the race without needing to fix the broader inventory-writer's tmp-name scheme.
>
> Tradeoff: up to ~1s of staleness on crash (the in-memory state ahead of disk by at most one debounce window). Acceptable for status — phase-1 contract doesn't promise crash-recovery freshness beyond the pre-push value.
>
> This implies a small body change to §4.2 once locked: the "atomic-writes the on-disk mirror" phrasing becomes "queues a flush to the coalesced background task" with the priority-final exception called out. Holding the body edit until Q7 resolves. Status: proposed-by-CLI.

**Q8 — Installer poll cadence** *(raised by GUI 2026-05-18 for installer team)*. §4.5 suggests ≤ once per 5 seconds. Installer team: what cadence works best with idle-friendly power management on macOS / Windows? Slower polls (e.g., 10s) are friendlier to battery; faster (1–2s) feels more "live" to the user. Or: switch cadence based on whether the panel is currently visible / focused?

> *(No CLI input — installer team's question.)*

**Q9 — Resolved-questions archive companion file missing** *(raised by CLI 2026-05-18)*. §4.2, §4.3, §6, and other sections backlink to `R3`, `R4`, `R5` in `installer-status-panel-resolved.md`, but that file is not present at the coord repo's `main` (raw URL returns 404). The GUI revision's changelog says "resolved appendix seeded with R1–R5" but the companion file wasn't included in the merged version. Two paths forward:
>
> - GUI team pushes the seeded `R1–R5` content as a separate revision against the coord repo's `installer-status-panel-resolved.md` path (the workflow YAML supports any `target` path under `docs/`, so the same mechanism works).
> - Or: the maintainer reconstructs minimal `R1–R5` stubs from the GUI's resolution rationale (Q1–Q4 closed in body + the R5 rationale referenced in §4.2's DELETE block).
>
> Either way, phase-1 implementation shouldn't proceed with broken cross-references — readers landing on `R3`/`R4`/`R5` links will hit 404s. Status: proposed-by-CLI (process gap; awaiting GUI team to push the missing file or maintainer to seed).

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
| 2026-05-18 | GUI | Session-mode privacy: added mode-dependent storage to §4.2 (memory-only + TTL for session, atomic disk write for persist). Added `current_state` field and `storage.session_ttl_seconds` to §4.4 payload. Clarified `last_activity.errors` shape. Made §4.3 push triggers explicit (required vs. mode-required). Added `DELETE /status` endpoint to §4.2 for explicit "Clear all data" path; clarified that sign-out is NOT a clear. Documented push debouncing floor. Raised Q5–Q8. Resolved Q1–Q4 in body; resolved appendix seeded with R1–R5. |
| 2026-05-18 | CLI | Answered Q5 (concurs with 500ms floor), Q6 (concurs with 60s/15s), Q7 (proposes coalesced background flush ≤1/s, with `priority: "final"` and shutdown-synchronous exceptions). Restored §4.7 security model (clear-text rationale + MUST-NOT list + sensitivity check at PR time) — drafted on a primary-repo branch that wasn't included in the GUI revision's basis. Noted in §4.2 that `<config_dir>/bsky-saves/status.json` write path may use concurrency-safe per-write tmp names if Q7 resolves on coalesced writes. Raised Q9 re: missing `installer-status-panel-resolved.md` companion file (R3/R4/R5 backlinks 404 against coord repo's main). No body content changed beyond §4.7 restoration; Q7's implied §4.2 body update held until the question resolves. |

---

## Appendix A — Decisions still to make before implementation starts

A condensed checklist for whoever drives phase 1 to ground. None of these are open design questions; they're sequencing-and-ownership decisions.

- [ ] Confirm helper-side endpoints (`POST /status`, `GET /status`, `DELETE /status`) and persistence path with the `bsky-saves` maintainer.
- [ ] GUI team commits to the §4.4 payload shape (or proposes amendments). Section 7 open questions Q5–Q9 resolved.
- [ ] Installer team confirms polling cadence (Q8) and UI rendering pass.
- [ ] Resolved-questions companion file (`installer-status-panel-resolved.md`) seeded and present at coord repo's `main` (per Q9).
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
