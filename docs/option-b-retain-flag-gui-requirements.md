# bsky-saves-gui requirements — inventory retention modes and lifecycle flags

> **Audience:** the `bsky-saves-gui` team.
> **Pairs with:** `bsky-saves` v0.6.0. Canonical bsky-saves-side spec: `bsky-saves/docs/superpowers/specs/2026-05-14-bsky-saves-v0.6.0-retain-and-flag.md`.
> **Status:** requirements for GUI-side work; deliver alongside `bsky-saves` 0.6.0.
> **The anti-drift contract:** the flag schema (§2) and the reconcile rules (§4) MUST match the bsky-saves CLI implementation exactly. They are two implementations of one spec; if they diverge, the CLI inventory and the GUI inventory stop agreeing — which is the bug this whole change exists to fix. §6 makes that contract executable: a shared golden-fixture set both repos run in CI.

---

## 1. Background — the problem this fixes

Today the CLI and the GUI disagree about what happens to a bookmark that is no longer on the server.

- **CLI** does a purely additive, URI-keyed union: it never drops anything, but it also never tells you an entry is stale.
- **GUI** (`mergeHydratedFields` in `library-refresh.ts`) merges *fields* but not *records*: it loads the prior inventory, indexes it by URI, then iterates only the **fresh fetch's** records, copying hydration annotations forward. It never re-adds a prior record the fresh fetch did not return. The written record set is therefore always exactly the fresh fetch — so un-saved or deleted bookmarks silently disappear from the library.

`bsky-saves` 0.6.0 makes retention an explicit, user-chosen **policy** with three modes, and adds **lifecycle flags** so retained entries are distinguishable. The GUI must adopt the same policy and flags so the two surfaces finally agree.

**Both sides change — this is not a GUI-only retrofit.** v0.6.0 rewrites the CLI's `merge_into_inventory` to the exact reconcile algorithm in §4: the purely-additive behaviour described above is *replaced*, not kept. So everywhere this doc says a behaviour "matches the CLI," it means the **new v0.6.0 CLI**, not today's. That symmetry is the whole point — one algorithm, two implementations (Python in `bsky-saves`, TypeScript here), kept honest by the shared golden fixtures (§6). The fixtures only enforce parity because both sides run the same new algorithm.

Two classes of "no longer synced" entry, with different value:

- **Class 1 — the user un-saved it.** The bookmark record left their repo. An explicit user gesture.
- **Class 2 — externally removed.** The bookmark record is still in the repo, but the post it points at was deleted, or its author blocked the user. The user never chose to lose this — the local archive is the only surviving copy.

## 2. The flag schema

Four new **optional** per-entry fields on each `saves[i]` entry. Timestamps are ISO 8601 UTC, `YYYY-MM-DDTHH:MM:SSZ` (the same format as the existing `fetched_at` / `saved_at`).

| Field | Type | Semantics |
|---|---|---|
| `last_seen_at` | string | Timestamp of the most recent fetch in which this URI was present. Refreshed on **every** fetch the URI appears in. |
| `removed_detected_at` | string, optional | Set when a URI present in the prior inventory is **absent** from a complete fetch (Class 1). **Cleared** when the URI reappears. Presence ⇔ "the bookmark record is no longer in the user's repo." |
| `subject_status` | string, optional | One of `"not_found"`, `"blocked"`, `"unknown"`. **Absent ⇔ the subject post is live.** `"not_found"` / `"blocked"` mean the post was deleted / the author blocked the user (Class 2). `"unknown"` means the entry has *only ever* been seen via the `listRecords` fallback, which carries no subject state — once any hydrated fetch establishes a real status, `"unknown"` can never overwrite it (see §4 step 3). |
| `subject_status_detected_at` | string, optional | Set when `subject_status` first becomes non-live (`not_found` / `blocked`). Cleared together with `subject_status` when the subject goes live again. Not set for `"unknown"`. |

These are additive. An inventory that has none of them (written by a pre-0.6.0 tool, or exported earlier by the GUI) is valid input — see §4.5.

### 2.1 `subject_status` arrives pre-populated — do NOT derive it in the GUI

`subject_status` derivation lives in **`normalise_record`, in the core `bsky_saves` package** (`bsky_saves/normalize.py`) — **not** in any `bsky-saves serve` wrapper code. There is no `serve`-layer-only derivation. This matters because the GUI has two fetch backends, and both run that same core function:

- the **Pyodide path** runs the core `bsky_saves` package in-browser, so it executes `normalise_record` directly;
- the **`/fetch` path** hits the `bsky-saves serve` daemon, which calls the same core `normalise_record` server-side.

Because the derivation is in the shared core, **both backends produce records with `subject_status` populated identically.** There is no backend where it silently goes missing — which is essential, since `sync`-mode's dead-subject prune would otherwise no-op for Pyodide-backed users. (On the `bsky-saves` side this is tracked explicitly: see spec §6.1 — `subject_status` derivation is a `normalize.py` work item, consumed identically by the CLI, `/fetch`, and Pyodide.)

As of `bsky-saves` 0.6.0, `normalise_record` derives `subject_status` from the `app.bsky.bookmark.getBookmarks` `item` union (`postView` → live / field absent; `notFoundPost` → `"not_found"`; `blockedPost` → `"blocked"`; `listRecords` raw-record shape → `"unknown"`). So **`subject_status` is already on every record the GUI receives, on every backend.** The GUI MUST NOT re-derive it and MUST NOT depend on inspecting raw `item.$type` itself — it consumes the normalised field.

The GUI's job is the **reconcile** (§4) — the record-level union/drop/prune and the `last_seen_at` / `removed_detected_at` / `subject_status_detected_at` lifecycle bookkeeping. That part is a separate TypeScript reimplementation because `merge_into_inventory` (Python) is CLI-only.

## 3. The three modes

A nested retention ladder — each mode keeps a superset of the one before:

| Mode | Keeps | Drops |
|---|---|---|
| `sync` | Entries present in the fetch with a live (or `unknown`) subject. | Class 1 (absent / un-saved) **and** Class 2 (present, `subject_status` ∈ {`not_found`, `blocked`}). |
| `keep-lost` | `sync` + Class 2 (dead-subject bookmarks, retained and flagged). | Class 1 (absent / un-saved). |
| `keep-all` | `keep-lost` + Class 1 (un-saved entries, retained, flagged with `removed_detected_at`). | Nothing. |

`sync` ⊂ `keep-lost` ⊂ `keep-all`.

### 3.1 The `sync` active-prune subtlety

A Class 2 entry — bookmark record still in the repo, post dead — is **never absent from a fetch**: every fetch keeps returning it (as a `bookmarkView` whose `item` is a `notFoundPost` / `blockedPost`). A reconcile that only drops *absent* entries therefore cannot exclude Class 2. So `sync` must **actively prune** entries whose `subject_status` is `not_found` / `blocked`, on top of dropping absent entries. This is idempotent (the dead entry reappears in the next raw fetch and is pruned again). It is intended: `sync` means "mirror the useful live state," and it will delete bookmarks that are still in the user's BlueSky store because their post died. `keep-lost` does **not** prune — that is the sole difference between `sync` and `keep-lost`.

`sync`'s prune targets `subject_status` ∈ {`not_found`, `blocked`} only. `"unknown"` entries are **kept** by `sync` — not known to be dead, benefit of the doubt.

## 4. The reconcile algorithm

Run this when assembling the inventory from a fetch. Inputs: the **prior inventory** (loaded via `loadInventory()`), the **fully-accumulated fresh fetch** (all `/fetch` pages, or the complete Pyodide fetch result), the selected **mode**, and a single **`now`** timestamp for the run.

> **Precondition — complete fetch only.** See §4.4. Do not run this reconcile on a partial fetch.

1. Index the prior inventory by `uri` → `priorByUri`.
2. Compute `fetchedUris` = the set of `uri` values in the fresh fetch.
3. **For each fresh-fetch entry (present):**
   - **Field-fill** (this is today's `mergeHydratedFields` behaviour, unchanged): if the URI exists in `priorByUri`, copy forward hydration annotations the fresh entry lacks (`article_text`, `article_title`, `article_published_at`, `article_fetched_at`, `local_images`, `thread_replies`, `thread_schema_version`, `thread_fetched_at`) — and, critically, last-known-good `post_text` / `author` / `images` when the fresh entry's subject is dead (a `notFoundPost` carries no content). Never overwrite a non-empty fresh value.
   - **Lifecycle-flag pass** (new — must be explicit, not part of the field-fill loop, because it must *update every run* and sometimes *clear*). Let `prior` be the entry as it stood in `priorByUri` (or absent, for a brand-new URI) and `fresh` the newly-fetched entry. Comparisons below are always against `prior`, never against the just-field-filled working entry:
     - `last_seen_at = now`.
     - If `prior` had `removed_detected_at`, **delete** it (reappearance).
     - **`subject_status` reconciliation.** `fresh.subject_status` has exactly three shapes — **absent** (live), **`"not_found"` / `"blocked"`** (Class 2), or **`"unknown"`** (the `listRecords` fallback path) — and all three are handled:
       - **`fresh` has no `subject_status`** (live) → delete `subject_status` **and** `subject_status_detected_at` from the working entry.
       - **`fresh.subject_status` ∈ {`not_found`, `blocked`}** → set `subject_status` to that value; set `subject_status_detected_at = now` **only if** `prior` had no `subject_status` or a *different* one (a state *transition* — this includes the brand-new-URI case where `prior` is absent, and the `"unknown"` → known case); if `prior` already had the *same* `subject_status`, carry its `subject_status_detected_at` forward unchanged.
       - **`fresh.subject_status == "unknown"`** → **`"unknown"` never overwrites, weakens, or clears an existing status, and never touches `subject_status_detected_at`.** If `prior` exists, leave both `subject_status` and `subject_status_detected_at` *exactly as they were* — even if `prior` had `not_found` / `blocked`, and even if `prior` had no status at all (a content-blind `listRecords` fallback must not erase or downgrade what a hydrated fetch established). Store `subject_status = "unknown"` **only** for a brand-new URI (no `prior`), and even then do not set `subject_status_detected_at`. Note this is *not* an unconditional "set `subject_status = "unknown"`" — that naive reading is wrong; for any existing entry, the `"unknown"` case is a no-op.
4. **For each prior URI absent from `fetchedUris` (Class 1):**
   - mode `keep-all` → keep the entry; set `removed_detected_at = now` if not already present; leave `last_seen_at` at its prior value; leave `subject_status` as-is.
   - mode `keep-lost` or `sync` → drop the entry.
5. **`sync` active prune (Class 2):** if mode is `sync`, drop every remaining entry whose `subject_status` ∈ {`not_found`, `blocked`}.
6. The resulting record set is the inventory's `saves`. Sorting/`fetched_at` handling is unchanged from current GUI behaviour.

### 4.1 What changes in `library-refresh.ts`

`mergeHydratedFields` today only does step 3's field-fill, iterating `newSaves`. The reconcile needs steps 4–5 (record-level union/drop/prune) and step 3's lifecycle-flag pass on top.

This is a larger change than "modify `mergeHydratedFields`" reads, for two reasons:

- **It owns array membership now, not just object fields.** `mergeHydratedFields` today returns `void` and mutates save *objects* in place — it never touches the `saves` *array*. Steps 4–5 require either in-place array splicing or returning a new inventory; the latter changes its call sites in `library-refresh.ts`.
- **Its "never overwrite fresh data" invariant no longer holds.** The lifecycle-flag pass sometimes *clears* fields (`removed_detected_at` on reappearance, `subject_status` / `subject_status_detected_at` when a post goes live again) — that is not a "fill missing fields" operation.

Realistically this becomes a new `reconcileInventory` that *wraps* `mergeHydratedFields` as its field-fill sub-step (step 3's first bullet), with the lifecycle pass and steps 4–5 around it. Treat the existing function as a building block, not the whole target.

### 4.2 Category predicates (for the filter UI — §5)

| Category | Predicate |
|---|---|
| `synced` | `removed_detected_at` absent **and** `subject_status` absent. (`subject_status == "unknown"` entries also fall here — present, not known-dead.) |
| `lost` | `subject_status` ∈ {`not_found`, `blocked`}. |
| `unsaved` | `removed_detected_at` present. |
| `all` | (no predicate) |

`lost` and `unsaved` are **not mutually exclusive** — an entry can carry both `removed_detected_at` and a stale `subject_status` (the user un-saved a bookmark whose post had already died). Such an entry shows under both filters. Do not assume the categories partition.

### 4.3 Persistence

The reconcile output is the inventory the GUI persists (IndexedDB `inventory:v1` / sessionStorage `inventory:session-v1`) — same `saveInventory` path as today. The only change is that the record set being persisted is now the reconcile result rather than "exactly the fresh fetch."

### 4.4 Partial-fetch guard — required

Absence-detection is only sound on a **complete** fetch: a URI may be declared absent (Class 1) only if every page was seen. The GUI paginates `/fetch` itself (the `runHelperPath` loop in `fetch-hydrator.ts`, accumulating pages until the cursor is `null`) and can be interrupted — tab close, network drop, an error mid-pagination. The GUI **MUST run the reconcile only on a fetch that paginated to completion** (cursor reached `null`, or the Pyodide fetch returned its full result without error). On an interrupted/partial fetch the GUI MUST either:

- fall back to additive-only behaviour for that run — field-fill the pages it did get, but perform **no** step-4 drops and **no** step-5 prune; or
- discard the partial run entirely and keep the prior inventory.

Running steps 4–5 on a partial page set would false-positive-flag live bookmarks as un-saved and, under `keep-lost` / `sync`, delete them. (The CLI does not have this hazard — its fetch layer is all-or-nothing.)

**Status: both current GUI backends already satisfy this guard naturally** — confirmed by the GUI team's review:

- **Helper path** — `fetchHydrator` paginates the full cursor chain and only returns when complete; the reconcile runs in `library-refresh.ts`'s `onAfterEnrich` callback, which fires *after* that. An interrupted helper fetch throws before `onAfterEnrich` runs.
- **Pyodide path** — `runFetchOnly` returns the complete inventory or throws; there is no partial-progress state.

So no new guard code is needed today. The requirement is documented here as an **invariant** — so a future refactor (moving the reconcile earlier, streaming pages into it, a new fetch backend) does not silently break it.

### 4.5 Backward compatibility

A prior inventory with none of the four flag fields (written by an older tool, or an older GUI export) is valid input. The first 0.6.0 reconcile populates `last_seen_at` / `subject_status` on still-present entries and treats every now-absent prior URI as Class 1. **Under the default `keep-lost`, that first run will drop prior entries no longer on the server** — including long-ago un-saved posts. This is intended and matches the new v0.6.0 CLI (which is rewritten to the same reconcile — see §1); see §5.1 for how the default view filter softens the user-visible impact.

## 5. UI surface

### 5.1 Default mode and default filter — recommended

- **Default mode: `keep-lost`.** Matches the CLI default. Data for `lost` entries is retained from the first run forward.
- **Default view filter: `synced`.** So an existing user opening the library after the update sees exactly what they saw before — only live, present bookmarks. They discover `lost` / `unsaved` / `all` by changing the filter.

This pairing is the point: `keep-lost` mode means the §4.5 first-run drop still happens (un-saved entries are not retained under `keep-lost`), but Class 2 "lost" entries are now retained — and the default `synced` filter means the user is not startled by them appearing in the main view. They opt in to seeing them.

`keep-all` is the "archive" mode: nothing is ever dropped, and the `unsaved` / `all` filters surface the full history. Expose it as an explicit user choice.

### 5.2 The mode toggle

Provide a user-facing setting equivalent to the CLI's `--mode` — three choices: `sync`, `keep-lost`, `keep-all`. Persist it with the rest of the GUI's library settings. Changing the mode takes effect on the next refresh; it does not retroactively rewrite the stored inventory (a later `sync` run will prune, a later `keep-all` run will simply stop dropping — consistent with the CLI).

The toggle SHOULD present **descriptive labels**, not the bare `sync` / `keep-lost` / `keep-all` identifiers — the distinction between them is not self-evident from the names alone. Wording is a GUI-side UX decision, but the labels should convey: `sync` = mirror only what's live on the server; `keep-lost` = also keep posts removed outside your control; `keep-all` = also keep bookmarks you deliberately un-saved (a full archive).

### 5.3 The filter UI

A filter control over the library view with the four categories from §4.2: `synced`, `lost`, `unsaved`, `all`. This is the "archive" experience the retention modes exist to enable — the user filtering their inventory by lifecycle state. Because `lost` and `unsaved` overlap, implement the filter as predicate matching, not as a partition.

## 6. Testing — the shared golden fixtures (required CI input)

The reconcile (§4) is implemented twice — in Python in `bsky-saves`, and in TypeScript here. A shared golden-fixture set is what keeps the two from drifting, and **running it in the GUI's CI is a required part of this work.**

- **Source.** The fixtures live in the `bsky-saves` repo at `tests/fixtures/retain/` (the canonical spec home). The GUI consumes the *same files* — via a git submodule or a pinned raw-content fetch in CI; your choice. Do **not** copy them into the GUI repo — a copy is a drift vector.
- **Format.** Each fixture is one JSON case: `{prior_inventory, fetch_records, mode, now, expected_output_inventory}`. `fetch_records` are already-normalised entries — the fixtures exercise the **reconcile**, not normalisation.
- **The GUI's obligation.** The GUI test suite must run every fixture through its reconcile function (the extended `mergeHydratedFields` or its sibling, §4.1) and assert the result equals `expected_output_inventory`. A fixture that passes in `bsky-saves` but fails here means the GUI reconcile has drifted from the spec — that is the alarm, and it must block the GUI release.
- **What they do not cover.** `subject_status` derivation — that is shared Python (§2.1), already unit-tested on the `bsky-saves` side. The fixtures are reconcile-only.
- **Release gate.** Per the `bsky-saves` v0.6.0 spec §11.2: a coordinated release does not bump the bundled `gui_version` until both repos are green on these fixtures.

## 7. Hydration: skip known-dead subjects

A `subject_status` of `not_found` / `blocked` means the subject post is gone — its account is deactivated, the post was deleted, or the author blocked the user. The GUI must take that into account when driving hydration:

- **Thread hydration — required GUI change.** The GUI must **exclude** entries whose `subject_status` is `not_found` or `blocked` from the `uris` list it POSTs to `serve`'s `/hydrate-threads`. That endpoint is a deliberately dumb URI-list thread-fetcher — it receives only bare URIs (no entry metadata, no `subject_status`), so it *cannot* and *should not* make this decision; the consumer that holds the inventory must. `app.bsky.feed.getPostThread` returns a `4xx` for a gone subject, so sending those URIs produces noisy `http_4xx` failures in the GUI's failed-modal for posts that were never hydratable. The `bsky-saves` CLI's `hydrate threads` already applies this skip on its side; the GUI must mirror it.
- **Article / image hydration — no GUI change needed.** A `not_found` / `blocked` entry has an empty `embed` and empty `images`, so article-text and image hydration naturally have nothing to do — they no-op without any special-casing.
- **It must be a pure runtime filter on the *current* `subject_status`** — never a persistent "do not hydrate" marker on the entry. `subject_status` is re-derived (and *cleared*) by every `fetch`/reconcile, and the reconcile's field-fill re-populates the entry's content when a subject is found again (e.g. an account comes back from deactivation). So once `subject_status` is gone, the entry must hydrate normally — threads, articles, and images all included. Filtering on the live value gives this for free; a sticky marker would break it.

## 8. Out of scope for this work

- Any change to the `bsky-saves serve` daemon *logic* — `serve.py` is untouched; `/fetch` stays a stateless, paginated, raw-page provider, and retention stays a consumer-side concern. **Note, however:** the `/fetch` *response shape* does gain `subject_status` additively — not because `serve` changed, but because `/fetch` calls `normalise_record`, which now emits it (§2.1). It is an additive field; existing readers that ignore unknown keys are unaffected. The GUI's MVP spec (`docs/bsky-saves-mvp-spec.md` §5.4), which documents the `/fetch` response, should gain a `subject_status` field entry once v0.6.0's record shape is final.
- The proactive capture daemon (`watch` / "Option C").
- Deriving `subject_status` in the GUI — it arrives pre-populated (§2.1).
