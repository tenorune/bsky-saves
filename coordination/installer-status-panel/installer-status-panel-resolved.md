# Installer status panel — resolved-questions archive

> **Companion to:** [`installer-status-panel.md`](./installer-status-panel.md). Holds closed questions and their resolutions as a design-rationale archive.
> **Convention:** entries are append-only. Don't edit a resolved entry's text; if a decision is revisited, log a new entry with cross-references.
> **Workflow:** when a question in the main doc's §7 closes, its content moves here (renamed `R<n>`) and the resolution gets folded into the main doc's body (or §4.x as applicable). The main doc's body references back here via `(see R<n>)` when the rationale matters.

---

## R1 — Push trigger set

**Raised by:** CLI (2026-05-17) as §7 Q1.
**Resolved by:** GUI (2026-05-18) in §4.3.

**Question:** What triggers a status push from the GUI? End of every successful fetch + every hydrate cycle is the obvious set. Should the GUI also push on idle-tick, on unpair / clear-data events, on backup-toggle changes?

**Resolution:** Required triggers documented in §4.3:
- Successful fetch
- Each per-asset hydration phase complete
- Toggle on/off for any of {threads, images, articles}
- Sign-in (initial snapshot carrying the new DID)
- "Clear all data" — sends `DELETE /status` rather than a regular push

Plus, in session mode only:
- Idle heartbeat at ~15s cadence to keep the helper's TTL alive

Plus, recommended in persist mode (added by R8's coalesced-flush model):
- `beforeunload` push with `priority: "final"` so the helper synchronously flushes pre-tab-close state to disk

Sign-out is explicitly NOT a trigger (it stops the push loop but doesn't clear; see R5).

---

## R2 — Payload contents

**Raised by:** CLI (2026-05-17) as §7 Q2.
**Resolved by:** GUI (2026-05-18) in §4.4.

**Question:** What's in the payload? The §4.4 starter set is a proposal. The GUI team owns the final field list.

**Resolution:** §4.4 of the main doc holds the final phase-1 shape. GUI additions over the CLI's starter set:
- `current_state` ∈ `{"idle", "refreshing", "hydrating", "error"}` — gives the panel a live signal without inferring from `last_activity.finished_at`.
- `storage.session_ttl_seconds` — pairs with `storage.mode === "session"` to advertise the helper TTL value the GUI is choosing.
- `last_activity.errors` clarified to `{kind, message, count}` object shape rather than an unspecified array.

Subsequently (R8) added:
- Optional top-level `priority` string field; `"final"` triggers the helper's persist-mode synchronous-flush path.

Any future additions follow the `schema_version` bump rules in §4.4 notes plus the §4.7 sensitivity-check-at-PR-time gate.

---

## R3 — Multiple GUI sessions on one helper

**Raised by:** CLI (2026-05-17) as §7 Q3.
**Resolved by:** Joint (2026-05-18) — GUI ratified what CLI proposed.

**Question:** Multi-browser users (or maintainer-style multi-account setups) push to the same helper. Last-write-wins vs. keyed by `did`.

**Resolution:** Phase 1: last-write-wins, single-slot. The payload always carries `library.did` for forward-compat. Phase 3 (multi-handle / CLI-inventory work) layers per-DID indexing on top without a contract break — `GET /status?did=...` or a list-shaped response are both available as later extensions.

---

## R4 — Pyodide-fallback mode

**Raised by:** CLI (2026-05-17) as §7 Q4.
**Resolved by:** CLI (2026-05-17) in §4.3.

**Question:** GUI in Pyodide-fallback mode (no helper to push to).

**Resolution:** Status push is skipped. The panel — if anyone is viewing it via the bundled-GUI / installer flow — displays last-known status from the prior paired session, with a stale timestamp surfacing the staleness. Documented limitation; not a bug.

---

## R5 — Clear-path semantics: only "Clear all data", not sign-out

**Raised by:** GUI (2026-05-18) during scope refinement.
**Resolved by:** Joint (2026-05-18).

**Question:** Initial GUI proposal treated both sign-out and "Clear all data" as triggers for `DELETE /status`. Tenorune corrected: in the GUI, sign-out preserves local library data; only "Clear all data" wipes it.

**Resolution:** Only "Clear all data" sends `DELETE /status`. Sign-out stops the push loop without clearing. The helper's response per mode:
- **Persist mode after sign-out**: snapshot stays on disk. Panel keeps showing it until "Clear all data" or a new sign-in with a different DID overwrites it.
- **Session mode after sign-out**: heartbeats stop, TTL fires within ~60s, helper drops the in-memory snapshot, panel goes blank.

This mirrors the GUI's own persist/session contract: the user's local library outlives sign-out in persist mode, and is transient in session mode. The helper snapshot's lifecycle tracks that exactly.

---

## R6 — Push debouncing rate

**Raised by:** GUI (2026-05-18) as §7 Q5.
**Resolved by:** Joint (2026-05-20) — GUI ratified CLI's confirmation.

**Question:** GUI proposed max one push per 500ms. CLI team: does this match what the helper's HTTP stack can comfortably handle, or should the floor be tighter (250ms / 1s)?

**Resolution:** **500ms locked** as the contract floor. CLI confirmed (2026-05-18) the helper can comfortably handle pushes at 10–100/sec — `ThreadingHTTPServer` per-thread, per-request work is sub-millisecond — so 500ms is generous. CLI noted the GUI could tighten to 250ms later for snappier panel feedback during hydration bursts without breaking the contract; the 500ms documented in §4.3 is a floor, not a target. GUI implementation will use 500ms initially and reassess only if panel UX feels laggy.

---

## R7 — Session-mode heartbeat cadence and TTL

**Raised by:** GUI (2026-05-18) as §7 Q6.
**Resolved by:** Joint (2026-05-20) — GUI ratified CLI's confirmation.

**Question:** GUI proposed 15s heartbeat / 60s TTL (4× safety margin against a single missed push). CLI team: any preference on the TTL value?

**Resolution:** **15s heartbeat / 60s TTL locked.** CLI confirmed (2026-05-18) the helper's implementation is lazy expiry — `now() vs memory_expires_at` comparison on each `GET /status`; no background timer — so any TTL value is equally cheap on the helper side. The number is anchored by panel UX intuition ("tab probably closed by now"), not by helper performance. 60s is the published value; the heartbeat must remain ≤ TTL/3 to survive a single missed push (15s satisfies this). The TTL value is in the payload (`storage.session_ttl_seconds`), so future tuning is a payload-only change.

---

## R8 — Persist-mode disk-write frequency

**Raised by:** GUI (2026-05-18) as §7 Q7.
**Resolved by:** Joint (2026-05-20) — GUI adopted CLI's coalesced-flush proposal.

**Question:** Per §4.2, the helper atomic-writes to disk on every persist-mode push. For a heavy hydration phase (~2 pushes/second from §4.3 debouncer) that's a lot of disk writes. Coalesce, or keep per-push?

**Resolution:** **Coalesced background flush** per CLI's proposal:

- In-memory snapshot updates immediately on every `POST /status` (so `GET /status` always sees the latest).
- Disk flush happens at most once per second via a debounced background task that observes the in-memory snapshot.
- GUI can send `priority: "final"` (new optional top-level string in the §4.4 payload) to bypass the coalescer and flush immediately — used on `beforeunload` so terminal state lands on disk before tab close. GUI implementation uses `navigator.sendBeacon()` or `fetch({keepalive: true})` because the unload event doesn't reliably await async work.
- Helper shutdown (SIGTERM / Ctrl-C) synchronously flushes the latest in-memory snapshot to disk before exiting.

Tradeoff accepted: up to ~1s of staleness on crash (the in-memory state ahead of disk by at most one debounce window). The phase-1 contract does not promise crash-recovery freshness beyond the pre-push value.

GUI design call on `priority`: defined as a string enum, not a boolean, so future priorities (`"low"` for non-essential idle heartbeats, etc.) can be added without a schema bump.

Implementation note for the helper: the per-write tmp-name concurrency hazard CLI flagged in §4.2 (the broader inventory-writer's single-tmp scheme racing under multi-threaded writes) is sidestepped naturally by the single background flush task. Per-write tmp names called out in §4.2 as defense-in-depth.

---

## R9 — Resolved-questions archive companion file missing

**Raised by:** CLI (2026-05-18) as §7 Q9.
**Resolved by:** GUI (2026-05-20) — pushed companion file in the same PR as this revision.

**Question:** §4.2, §4.3, §6, and other sections backlink to `R3`/`R4`/`R5` in `installer-status-panel-resolved.md`, but that file was not present at the coord repo's `main` (raw URL returned 404). The GUI revision's changelog said "resolved appendix seeded with R1–R5" but the companion file wasn't included in the merged version.

**Resolution:** Root cause was the original coordination workflow being single-file per run; the GUI's first push only carried `installer-status-panel.md`, not its companion. The workflow has since been upgraded (2026-05-20) to a manifest-driven model that PRs an arbitrary file list in a single PR. The GUI's 2026-05-20 revision includes both files in one PR via that mechanism, closing the gap.

Process forward: any future revision that touches both files will include both in the manifest. Workflow drift between the body's `(see R<n>)` backlinks and the appendix's presence is now mechanically prevented — the manifest review during PR is where any mismatch surfaces.

---

## R10 — Installer poll cadence

**Raised by:** GUI (2026-05-18) as §7 Q8.
**Resolved by:** Installer (2026-05-21) in §4.5.

**Question:** §4.5 suggests ≤ once per 5 seconds. Installer team: what cadence works best with idle-friendly power management on macOS / Windows? Slower polls (e.g., 10s) are friendlier to battery; faster (1–2s) feels more "live" to the user. Or: switch cadence based on whether the panel is currently visible / focused?

**Resolution:** **Visibility-gated polling, 5s cadence.**

- One `GET /status` immediately on popover show — hydrates the panel without waiting for the first tick.
- Every 5 seconds while the popover remains visible.
- No polling while the popover is closed.

Rationale:

- **Why 5s, not faster or slower.** The installer already runs a 5s `Supervisor.is_alive()` + `/ping` health-poll timer (the one that drives the red menu-bar state badge when the helper is unresponsive / in port conflict). Co-fetching `/status` on the same tick means no second timer, no extra wake-ups for the power-management subsystem to reason about — one consolidated 5s heartbeat that runs while the popover is visible. 5s sits comfortably under R7's 15s heartbeat / 60s TTL window: the panel observes fresh GUI pushes within ≤5s of arrival, and observes session-mode TTL expiry (the 404 transition) within ≤5s of it happening. A faster cadence (1–2s) would feel marginally more "live" but consume battery for sub-perceptual gains; a slower cadence (10s) would risk the panel showing stale data through a TTL-expiry transition for up to half the TTL window. 5s is the sweet spot.

- **Why visibility-gated.** The panel renders only when the popover is visible. Polling while it's closed produces no observable effect — the user can't see what the panel would draw. On a battery-powered laptop with the popover closed for hours at a time, eliding the poll entirely keeps the launcher CPU-idle and lets macOS's app-nap and Windows's modern-standby paths do their thing without an HTTP-poll loop preventing deeper sleep states. NSPopover's `popoverWillShow:` / `popoverWillClose:` delegate methods on macOS are the start/stop signals; equivalent platform hooks will drive this on Windows / Linux when those installer ports land.

- **Why not focus-gated instead.** A focused-popover-only variant (poll only when the user has the popover focused) was considered and rejected: a transient popover loses focus the moment the user moves attention elsewhere on screen, but stays *visible* until it's dismissed by click-outside. Halting polling on focus-loss would freeze the panel during the user's most likely look-at moments (popover open, user reading the values while their attention's already drifted to the next thing).

Implementation note (informational, not contract): the installer's existing health poll timer lives in `bsky_saves_launcher.tray.TrayApp`; the status fetch will be added there. The popover's `popoverWillShow:` schedules an immediate fetch and `popoverWillClose:` clears the next-fetch timestamp. No change to the panel's existing auth flow — the same bearer token is reused.

---

## R11 — Semantics of `last_activity.kind` vs `current_state`

**Raised by:** Installer (2026-05-22) as §7 Q10.
**Resolved by:** GUI (2026-05-22) in §4.4 field semantics + code fix in [tenorune/bsky-saves-gui#85](https://github.com/tenorune/bsky-saves-gui/issues/85) (merged 2026-05-22, released in v0.6.5-rc.4).

**Question:** Installer-side panel observed two related defects against pre-fix GUI builds:

1. **In-flight detection unreliable.** Panel can't tell the GUI is mid-work from `current_state` alone (which dropped to `"idle"` while image / article / thread hydration was still running), so the panel falls back to inferring activity from observed hydration-progress deltas across polls (8s grace window). Side effect: up to 8s of stale "Backing up…" persistence after hydration ends.

2. **Restart loses last-activity context.** When the installer restarts, the helper rebinds and loads its persisted disk snapshot. If the most recent GUI push had `last_activity.kind = "idle"` (the in-memory default at GUI startup), the panel renders no last-activity line on restart — even though a real activity happened minutes ago.

GUI testing of v0.6.5-rc.3 surfaced a third symptom with the same root cause as (1): the panel renders blank during the initial "First fetch in progress…" phase because the panel's progress-delta inference window has nothing to observe yet (hydration hasn't started; only the fetch phase has).

**Resolution:** **Both bugs are GUI-side and the proposed semantics are adopted verbatim.**

- **`last_activity.kind`** = the last *completed* operation. Values: `"fetch" | "hydrate_articles" | "hydrate_threads" | "hydrate_images" | "manual_refresh"`. Monotonically advances through real operations; never reverts to `"idle"` after the first real operation. `"idle"` is retained in the enum only as a fresh-install / post-clear sentinel (i.e., before any real operation, or after `DELETE /status` / "Clear all data").
- **`current_state`** = what the GUI is doing *right now*. Values: `"idle" | "refreshing" | "hydrating" | "error"`. The field that flips around during transitions; `"idle"` here is meaningful and expected during steady-state.

§4.4 field-reference list was tightened to capture these semantics in-place.

**GUI implementation** (in v0.6.5-rc.4):

- **Fix 1 — `deriveCurrentState` is now hydration-aware.** It reads all three hydration stores (`imageHydration`, `articleHydration`, `threadProgress`) in addition to `libraryRefreshState` and `fetchProgress`. Returns `"hydrating"` whenever any hydration store is running, even after the library refresh itself has flipped back to idle (which it does well before the fire-and-forget image / article hydration completes).

- **Fix 2 — `last_activity` is now persisted to local browser storage.** The pusher writes the activity record to `idb-keyval` under `status-pusher:last-activity:v1` on every watcher transition, and restores it at `initStatusPusher` boot via a guarded fire-and-forget load. A "real activity that fires before the load resolves" race is handled by only applying the restored value while in-memory `currentActivity` is still at its initial idle / null-started_at default. Settings → "Clear all data" wipes the persisted record alongside the existing `deleteStatus()` call.

**Verification status (against v0.6.5-rc.4):**

- ✅ Mid-hydration `current_state` (problem 1) — fixed. `current_state === "hydrating"` is now pushed for the entire duration of image / article / thread hydration regardless of `libraryRefreshState` value.
- ✅ Post-restart `last_activity.kind` (problem 2) — fixed. GUI restart now restores the persisted record from idb before the activation rising-edge push, so the helper's on-disk snapshot is no longer overwritten with `"idle"`.
- ✅ First Fetch blank-panel (the third symptom) — fixed end-to-end. The installer panel now renders "Fetching library…" while `current_state === "refreshing"` (placeholder headline when no snapshot has handle yet) and "Refreshing…" inline once a library is identified. Landed on `claude/spec-installer-status-panel` in `tenorune/bsky-saves-install` (commit `73e035e`).

**Installer follow-ups — closed:**

- ✅ **`current_state === "refreshing"` render branch.** Implemented in `_render_library_section`: placeholder headline switches to "Fetching library…" when in-flight pre-handle, and the last-activity row reads "Refreshing…" / "Backing up…" inline when refreshing / hydrating.
- ✅ **Progress-delta inference retired.** `status.hydration_is_progressing`, `StatusPopover._hydration_active_until`, and the delta-detection branch in `update_library` were removed (commit `ec32356`). The panel now reads `snap.current_state` directly. No `/ping`-based version gate was added — the installer has no external user base at this stage (internal dogfooding only), so the legacy GUI fallback path was not needed.

---

## R12 — Semantics of `current_state === "error"`

**Raised by:** CLI (2026-05-22) as §7 Q11, derived from R11 resolution.
**Resolved by:** GUI (2026-05-22, proposed); CLI (2026-05-22, accepted).

**Question:** R11 locked the `current_state` enum to `"idle" | "refreshing" | "hydrating" | "error"`, but neither side had agreed on what `"error"` actually means in operation. Four sub-questions:

- **Emission.** Which failures trigger `"error"` — auth, network, partial-hydration, persistence, all of the above?
- **Stickiness.** Does `"error"` persist until cleared by an explicit user action / next successful operation, or auto-clear after a transient failure?
- **Persistence behavior.** Does the helper mirror an `"error"` `current_state` to disk like other states, or only persist the last successful snapshot? If persisted, rehydration on restart re-renders the error — desired or surprising?
- **Panel rendering.** Toast, persistent banner, badge, retry affordance?

Resolution was required before any RC that emits `current_state === "error"` in anger.

**Current GUI behavior (v0.6.5-rc.4) — for the contract to lock against:**

| Sub-question | Behavior |
|---|---|
| **Emission triggers** | Exclusively `libraryRefreshState.status === 'error'`, set by `library-refresh.ts:163` when the orchestrate try-block throws. Concretely: auth failures, PDS / helper network failures during the fetch+enrich+threads orchestration, the BUG-#35 "fetch returned 0 saves against non-zero prior" guard, reconcile failures. **Per-asset hydration failures (image / article / thread) do NOT promote** to `current_state === "error"` — they're collected into `last_activity.errors[]` and the run continues. A single failed image fetch should not mark the entire library as in an error state. |
| **Stickiness (GUI-side)** | In-memory only. `current_state === "error"` lives in `libraryRefreshState` until the next `startLibraryRefresh` call flips it back to `'running'`. Does NOT survive a GUI tab reload — after restart, `libraryRefreshState` initializes to `{ status: 'idle' }`, so the GUI's first push after reload will report `current_state: 'idle'` (overwriting any prior `"error"` snapshot on the helper). |
| **Stickiness (helper-side disk)** | The helper persists whatever the GUI pushes, including `"error"`. The error snapshot survives a helper restart, but is overwritten on the next GUI push (which, post-reload, will be `"idle"` per the row above). |
| **Error text** | Already in the payload: `last_activity.errors[0]` carries `{ kind: 'refresh_error', message: <stringified throw>, count: 1 }` (set by `status-pusher.ts` on the refresh running→error edge). The panel reads the human-readable message from there; no payload-shape change needed. |

**Resolution:** GUI-proposed five-point resolution adopted verbatim:

1. **Emission scope.** `current_state === "error"` means the most recent library-refresh attempt threw. Per-asset hydration failures continue to surface via `last_activity.errors[]` only, without flipping `current_state`.
2. **Stickiness model.** Snapshot-bound, not session-bound. `current_state === "error"` lives on the helper's persisted snapshot until the next GUI push overwrites it (which happens when a new refresh runs, OR when the GUI tab reloads and pushes its fresh-state push). The panel SHOULD render the persisted error until a new push replaces it — no client-side auto-clear timeout.
3. **Persistence behavior.** The helper persists `"error"` like any other state. Re-rendering it after an installer / helper restart is the desired behavior: the user's last-known truth is that something failed, and silently dropping that signal would be worse than showing it.
4. **GUI tab reload caveat.** Because the GUI doesn't re-emit `"error"` after a tab reload, an error snapshot on the helper can be silently overwritten with `"idle"` once the user reopens the GUI. Known limitation of the current GUI emission model; candidate for a future improvement (e.g., persist the last refresh error to idb the same way #85 persisted `last_activity`). Documented in the contract; not blocking on R12.
5. **Rendering / retry affordance.** Panel-side decision. The GUI surfaces retry via its in-app banners (`AuthErrorBanner`, etc.); the panel can mirror or differ. No GUI-side change required for this sub-question.

**Implementation status:**

- ✅ **CLI helper:** no code change required. The helper's payload-opaque persistence model already mirrors any `current_state` value, including `"error"`, to disk like any other state. The wholesale-replacement invariant (also pinned by R13's helper-side test) ensures a fresh GUI push cleanly overrides a prior error snapshot.
- ⏸ **Panel rendering** (installer-side): out of contract scope. The installer team will land its own rendering / retry surface independently.
- ⏸ **GUI idb-persisted refresh-error** (Q11 sub-point 4 follow-up): tracked as a candidate future improvement on the GUI side. Not blocking the panel work.

No payload-shape changes. Contract clarification only.

---

## R13 — GUI-startup snapshot: overwrite vs merge

**Raised by:** CLI (2026-05-22) as §7 Q12, derived from R11 Bug 2 root cause.
**Resolved by:** GUI (2026-05-22, proposed); CLI (2026-05-22, accepted).

**Question:** R11's Bug 2 root cause surfaced a separate concern independent of the kind/state semantics fix: every GUI startup push clobbers the helper's on-disk snapshot with the GUI's just-initialized in-memory state. After the R11 fix the clobbered value is at least accurate — but the underlying "GUI startup wins, disk loses" direction was not documented in any §4.x section, and the alternative (merge: GUI's startup push respects on-disk `last_activity` if its `finished_at` is newer) had not been ruled out.

**Resolution:** **Overwrite (status quo, post-#85).** GUI's proposed contract text adopted verbatim and codified as the new [§4.8 Startup-flow contract](./installer-status-panel.md#48-startup-flow-contract).

GUI-supplied rationale for picking overwrite over merge:

1. **Canonical-source ownership stays clean.** The GUI is the only process that observes when operations actually start and finish; the helper just stores what the GUI told it. Merge would force both sides to encode timestamp-comparison logic. Overwrite keeps the contract one-directional.
2. **Merge requires a startup `GET /status`.** Adds a localhost round-trip the GUI doesn't currently make — the GUI never reads `/status`, only writes it. Adding a read to the activation-rising-edge path widens the contract surface for marginal benefit.
3. **The single legitimate divergence scenario is "user wiped browser data, helper still has older snapshot on disk."** Overwriting with idle / no-history is the correct behavior in that case — "this browser has no history" is what the wipe means. Merge would resurrect data the user explicitly cleared.
4. **The helper-crashed-and-lost-disk-state scenario is symmetric.** Helper boots with empty disk; GUI's next push repopulates it. Merge doesn't help here either way.

Key context that made overwrite the right call: **post-#85, the GUI's startup push is no longer empty-default.** The pusher restores `last_activity` from `idb-keyval` under `status-pusher:last-activity:v1` at `initStatusPusher` boot, before the activation-rising-edge push fires. So overwrite means "GUI's locally-persisted state wins", not "GUI's just-initialized empty state wins" — which was the original R11 Bug 2 concern.

**Implementation status:**

- ✅ **§4.8 added** to `installer-status-panel.md` with the GUI's proposed contract text verbatim.
- ✅ **CLI helper already complies.** `bsky-saves:src/bsky_saves/_status.py`'s `receive_push` does a wholesale snapshot replacement — no field-level merging — on every push, including GUI startup pushes (which the helper cannot distinguish from any other push). The invariant is pinned by a test in `bsky-saves:tests/test_status.py` that verifies a fresh push with different values overwrites every field of a prior snapshot (no prior fields leak through).
- ✅ **GUI behavior matches the contract** as of v0.6.5-rc.4 (the startup-push payload structure was established by #85).

No payload-shape changes. Contract clarification only.
