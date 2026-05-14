# bsky-saves v0.6.0 — Inventory retention modes and lifecycle flags

> **Status:** approved 2026-05-14. Implementation pending.
> **Branch:** `claude/bluesky-native-format-export-H414r` in `tenorune/bsky-saves`.
> **Releases as:** PyPI `bsky-saves==0.6.0`. Consumers: the `bsky-saves-gui` static PWA.
> **External contract:** `docs/option-b-retain-flag-gui-requirements.md` — the requirements handed to the `bsky-saves-gui` team. That document is canonical for the GUI-side reimplementation; this document is canonical for the bsky-saves-side implementation. The two MUST agree on the flag schema (§4) and the reconcile rules (§6) — that agreement is the anti-drift contract.

---

## 1. Context

`bsky-saves` ingests a user's BlueSky bookmarks into a JSON inventory. Today the CLI and the GUI handle "an entry I had before is no longer on the server" inconsistently:

- **CLI** (`merge_into_inventory`, `normalize.py:191`) is a purely additive URI-keyed union. It never deletes. A bookmark you un-save, or a post that gets deleted, stays in the inventory forever — silently, with no indication it is stale.
- **GUI** (`mergeHydratedFields` in `bsky-saves-gui`) merges *fields* but not *records*: the written record set is always exactly the fresh fetch. Un-saved or deleted bookmarks silently disappear.

So the two surfaces disagree (one hoards, one syncs) and *neither* lets the user reason about *why* an entry is or isn't present. v0.6.0 ("Option B") fixes both:

1. Makes retention an explicit, user-chosen **policy** with three modes.
2. Adds **lifecycle flags** so retained entries are distinguishable as live / externally-removed / un-saved.
3. Defines those modes and flags once, as a shared spec both the CLI and the GUI implement — ending the drift.

### Why three modes

Two classes of "no longer synced" entry have different value:

- **Class 1 — you un-saved it.** The `app.bsky.bookmark` record left your repo. Only you (or a client with your credentials) can do this; it is an explicit user gesture.
- **Class 2 — externally removed.** The bookmark record is still in your repo, but the post it points at was deleted, or its author blocked you. You never chose to lose this.

Class 2 retention is the core value (the local archive is the only surviving copy). Class 1 retention is a lower-value "undo buffer." A binary retain/sync toggle cannot express "keep what the world took from me, drop what I deliberately let go of," so v0.6.0 ships three nested modes — see §5.

### Why this is robust to the atproto roadmap

BlueSky bookmarks are stored off-protocol today in private storage ("stash"), modelled on lexicon definitions to ease an eventual on-protocol migration. That migration, if it happens, lands on the *fetch layer* (which endpoint, which shape) — which `bsky-saves` already absorbs via its multi-endpoint probe and graceful degradation. The retention/flag logic in this spec operates entirely on `bsky-saves`'s own normalised inventory and is unaffected by where or how BlueSky stores the source data. The inventory is an independent archive, not a cache.

## 2. Scope

`bsky-saves` remains an ingestion package. v0.6.0 changes the `fetch` subcommand and the inventory schema; it adds no new subcommands and no new endpoints.

### In scope

- **Inventory schema:** four new optional per-entry fields (`last_seen_at`, `removed_detected_at`, `subject_status`, `subject_status_detected_at`) — §4.
- **`normalise_record`:** derive `subject_status` from the `getBookmarks` `item` union; this also fixes a latent bug where a deleted-subject bookmark is silently emitted as a content-empty entry indistinguishable from a healthy text-less post — §6.1.
- **`merge_into_inventory`:** gains a `mode` parameter and a `now` timestamp; implements the three-mode reconcile and the lifecycle-flag pass — §6.2.
- **`fetch_to_inventory` + CLI:** the `fetch` subcommand gains `--mode {sync,keep-lost,keep-all}` (default `keep-lost`) plus `--sync` and `--keep-all` convenience aliases — §7.
- **Shared category predicates:** the spec defines `synced` / `lost` / `unsaved` / `all` as predicates over the flag fields so any consumer (the GUI's filter UI, a future CLI `list`) classifies entries identically — §5.3.
- **`README.md`:** document the modes and the new schema fields.
- **GUI requirements doc:** `docs/option-b-retain-flag-gui-requirements.md`, the handoff artifact for the `bsky-saves-gui` team — §8.

### Out of scope (explicitly deferred)

- **`serve` changes.** `/fetch` stays a stateless, paginated, raw-page provider. Retention is structurally a *consumer* concern: it needs prior state and a *complete* fetch, neither of which a stateless paginated endpoint has. A mode-aware `serve` would be a stateful daemon — that is Option C. See §9.
- **Option C — the proactive capture daemon (`watch`).** A long-running daemon that captures saves continuously and owns the inventory file. Noted as the next follow-up after v0.6.0.
- **A CLI `list --filter` command.** The filter is a GUI feature for v0.6.0; this spec only defines the category *predicates* (§5.3) so a CLI `list` can be a small, clean follow-up later.
- **Active post-existence probing.** Distinguishing `not_found` / `blocked` is free from the `getBookmarks` `item` union (§6.1); no extra network calls are needed and none are added.
- **The `getActorBookmarks` probe-list cleanup.** `app.bsky.feed.getActorBookmarks` is not in the current official lexicon set; whether `bsky-saves` should keep probing it is a separate question, not part of this work.
- **L2 CAR export / signed-repo-slice work.** Unrelated; and BlueSky bookmarks being off-protocol stash data means they are likely not in the public repo CAR anyway.

## 3. Architecture and module layout

### Files modified

| File | Change |
|---|---|
| `src/bsky_saves/normalize.py` | `normalise_record` gains `subject_status` derivation from `item.$type` (and the latent-bug fix). `merge_into_inventory` gains `mode` and `now` keyword params, an explicit lifecycle-flag pass, and the `sync`-mode active prune. |
| `src/bsky_saves/fetch.py` | `fetch_to_inventory` gains a `mode` parameter; passes `mode` and `now=_now_iso()` to `merge_into_inventory`. The "complete fetch" safety property is documented (see §6.3). |
| `src/bsky_saves/cli.py` | `fetch` subcommand gains a mutually-exclusive group: `--mode {sync,keep-lost,keep-all}` (default `keep-lost`), `--sync` (alias for `--mode sync`), `--keep-all` (alias for `--mode keep-all`). `main()` passes `mode=args.mode` to `fetch_to_inventory`. |
| `src/bsky_saves/serve.py` | No change. Listed here only to make the "no change" explicit and reviewable. |
| `tests/test_normalize.py` | New tests for `normalise_record` `subject_status` derivation (each `item.$type`, listRecords shape) and for `merge_into_inventory` across all three modes and every lifecycle transition. |
| `tests/test_fetch.py` | New tests for `fetch_to_inventory` mode plumbing and for the CLI argument parsing (`--mode`, `--sync`, `--keep-all`, the mutually-exclusive guard, the default). |
| `README.md` | Document the three modes, the convenience aliases, and the four new schema fields. Update the "Inventory schema" block. |
| `pyproject.toml` | Bump `version = "0.6.0"`. |

### Files created

| File | Responsibility |
|---|---|
| `docs/option-b-retain-flag-gui-requirements.md` | The external contract for the `bsky-saves-gui` team: the flag schema, the reconcile rules, the category predicates, the mode toggle, the partial-fetch guard, and the default-mode / default-filter guidance. Mirror of §4–§6 written for the GUI audience. |

### Module-boundary intent

All retention logic lands in `normalize.py`, which already owns both `normalise_record` and `merge_into_inventory` — the per-record shape mapping and the inventory-level merge. No new module is warranted: the change is two existing functions growing well-bounded new behaviour, not a new responsibility. `fetch.py` and `cli.py` only plumb the `mode` value through; they gain no retention logic of their own. `serve.py` is untouched.

## 4. Inventory schema additions

Four new **optional** per-entry fields. All timestamps use the existing inventory convention: ISO 8601 UTC, `%Y-%m-%dT%H:%M:%SZ`, matching `fetched_at` / `saved_at`.

| Field | Type | Semantics |
|---|---|---|
| `last_seen_at` | string (ISO 8601) | The timestamp of the most recent fetch in which this URI was present. Refreshed on **every** fetch the URI appears in. Present on every entry once a fetch has observed it. |
| `removed_detected_at` | string (ISO 8601), optional | Set when a URI present in the prior inventory is **absent** from a complete fetch (Class 1 — un-saved). **Cleared** if the URI later reappears in a fetch. Presence ⇔ "the bookmark record is no longer in your repo." |
| `subject_status` | string, optional | One of `"not_found"`, `"blocked"`, `"unknown"`. **Absent ⇔ the subject post is live.** `"not_found"` / `"blocked"` come from the `getBookmarks` `item` union (Class 2 — externally removed). `"unknown"` means the entry was fetched via the `listRecords` fallback, which carries no subject state. Values intentionally echo the existing `quoted_post.unavailable` vocabulary. |
| `subject_status_detected_at` | string (ISO 8601), optional | Set when `subject_status` first becomes non-live (`not_found` / `blocked`). **Cleared** together with `subject_status` if the subject goes live again. Not set for `"unknown"` (that is not a "went non-live" event). |

These fields are additive; existing inventory readers that ignore unknown keys are unaffected. An inventory written by an older `bsky-saves` (no flags) is valid input — see §6.4.

## 5. The three modes

### 5.1 Definitions

The modes form a **nested retention ladder** — each keeps a superset of the one before:

| Mode | Keeps | Drops |
|---|---|---|
| `sync` | Entries present in the fetch with a live (or `unknown`) subject. | Class 1 (absent / un-saved) **and** Class 2 (present but `subject_status` ∈ {`not_found`, `blocked`}). |
| `keep-lost` *(default)* | `sync` + Class 2 (dead-subject bookmarks, retained and flagged). | Class 1 (absent / un-saved). |
| `keep-all` | `keep-lost` + Class 1 (un-saved entries, retained and flagged with `removed_detected_at`). | Nothing. |

`sync` ⊂ `keep-lost` ⊂ `keep-all`.

### 5.2 The `sync` active-prune subtlety

A Class 2 entry — bookmark record still in your repo, post dead — is **never absent from a fetch**: every fetch keeps returning it as a `bookmarkView` whose `item` is a `notFoundPost`/`blockedPost`. Reconcile-by-absence therefore cannot drop it. For `sync` to be a genuinely distinct mode that excludes dead-subject bookmarks, it must **actively prune** entries whose `subject_status` is non-live, in addition to dropping absent entries. This is idempotent: the dead entry reappears in the next raw fetch and is pruned again, so the inventory stably contains only live (or `unknown`) entries. This is intended behaviour: `sync` means "mirror the *useful* live state," and it will delete bookmarks that are still in your BlueSky store because their post died. `keep-lost` does **not** do this prune — that is the one and only difference between `sync` and `keep-lost`.

`sync`'s active prune targets `subject_status` ∈ {`not_found`, `blocked`} only. `"unknown"` entries are **kept** by `sync` — they are not *known* to be dead, and get the benefit of the doubt.

### 5.3 Category predicates (shared with consumers)

The GUI's filter UI (and any future CLI `list`) classifies entries with these predicates. They are defined here so every consumer agrees:

| Category | Predicate |
|---|---|
| `synced` | `removed_detected_at` absent **and** `subject_status` absent. (A present, live bookmark. `subject_status == "unknown"` entries also fall here — present, not known-dead.) |
| `lost` | `subject_status` ∈ {`not_found`, `blocked`}. |
| `unsaved` | `removed_detected_at` present. |
| `all` | (no predicate) |

`lost` and `unsaved` are **not mutually exclusive**: an entry can carry both a stale `subject_status` and a `removed_detected_at` (you un-saved a bookmark whose post had already died — "Class 1 masks Class 2," see §6.2). Such an entry appears under both filters. This is acceptable and intended; consumers MUST NOT assume the categories partition.

## 6. Implementation detail

### 6.1 `normalise_record` — deriving `subject_status`

The hydrated `app.bsky.bookmark.getBookmarks` entry's `item` field is a union (per `app.bsky.bookmark.defs#bookmarkView`):

- `app.bsky.feed.defs#postView` — a live post.
- `app.bsky.feed.defs#notFoundPost` — `{$type, uri, notFound: true}`. No `record` / `author` / `embed`.
- `app.bsky.feed.defs#blockedPost` — `{$type, uri, blocked: true, author}`. No `record` / `embed`.

`normalise_record` currently reads `item.get("record", {})` etc. unconditionally, so a `notFoundPost` is silently emitted as a content-empty entry indistinguishable from a healthy text-less post. The fix: branch on `item.get("$type")`.

- `item.$type == "app.bsky.feed.defs#notFoundPost"` → set `subject_status = "not_found"`; do not attempt content extraction (content fields take their existing empty defaults).
- `item.$type == "app.bsky.feed.defs#blockedPost"` → set `subject_status = "blocked"`; same.
- `item.$type == "app.bsky.feed.defs#postView"`, or no `$type` (today's behaviour) → live; **do not emit `subject_status`** (absence ⇔ live).
- `listRecords` raw-record shape (no `item` key) → set `subject_status = "unknown"`.

`subject_status` is emitted on the entry only when it is non-live or `unknown`; a healthy entry has no `subject_status` key. `normalise_record` remains per-record and stateless — it does not see prior inventory state; all reconciliation of `subject_status` against history happens in `merge_into_inventory`.

### 6.2 `merge_into_inventory` — the three-mode reconcile

New signature:

```python
def merge_into_inventory(
    existing: dict,
    new_entries: list[dict],
    *,
    mode: str = "keep-lost",
    now: str,
) -> dict:
```

`now` is the fetch timestamp, supplied by the caller (`fetch_to_inventory` passes `_now_iso()`; tests inject a fixed value). `mode` is one of `"sync"`, `"keep-lost"`, `"keep-all"`.

Algorithm:

1. `by_uri` ← prior saves keyed by `uri` (as today).
2. `fetched_uris` ← `{e["uri"] for e in new_entries if e.get("uri")}`.
3. **For each new entry (present in the fetch):**
   - **Field-fill** (unchanged rule): for a URI already in `by_uri`, add missing fields from the new entry but never overwrite a non-empty existing value (preserves `article_text`, `thread_replies`, and — importantly — last-known-good `post_text` / `author` / `images` when the latest `item` is a `notFoundPost`). For a new URI, insert the entry as-is.
   - **Lifecycle-flag pass** (explicit, *not* routed through the field-fill loop — the field-fill loop cannot express "update every run" or "clear"). Let `prior` be the entry as it stood in the prior inventory (or `None` for a brand-new URI) and `fresh` the newly-fetched entry. Comparisons below are always against `prior`, never against the just-field-filled working entry:
     - `last_seen_at = now`.
     - Delete `removed_detected_at` if `prior` had it (reappearance).
     - **`subject_status` reconciliation**, driven by `fresh`'s `subject_status`:
       - `fresh` has no `subject_status` (live observation from `getBookmarks`) → delete `subject_status` and `subject_status_detected_at`.
       - `fresh.subject_status` ∈ {`not_found`, `blocked`} → set `subject_status` to that value; set `subject_status_detected_at = now` **only if** `prior` had no `subject_status` or a different one (a state *transition* — this includes the brand-new-URI case, where `prior` is `None`); if `prior` already had the same `subject_status`, carry its `subject_status_detected_at` forward unchanged.
       - `fresh.subject_status == "unknown"` → if `prior` exists, **leave its `subject_status` / `subject_status_detected_at` untouched** (downgrade protection: a `listRecords` fallback must not erase a known status). For a brand-new URI, store `subject_status = "unknown"` with no `subject_status_detected_at`.
4. **For each existing URI absent from `fetched_uris` (Class 1):**
   - `mode == "keep-all"` → keep the entry; set `removed_detected_at = now` if not already set; leave `last_seen_at` at its prior value; leave `subject_status` as-is (it may carry a stale Class 2 status — "Class 1 masks Class 2").
   - `mode in ("keep-lost", "sync")` → drop the entry.
5. **`sync` active prune (Class 2):** if `mode == "sync"`, drop every remaining entry whose `subject_status` ∈ {`not_found`, `blocked`}.
6. Sort by `saved_at` descending (unchanged). Return `{"fetched_at": existing.get("fetched_at"), "saves": [...]}` — the caller still owns `fetched_at`.

### 6.3 The "complete fetch" safety property

Reconcile-by-absence is only sound on a **complete** fetch — a URI must be declared absent only if *all* pages were seen. The CLI is **naturally safe**: `probe_bookmark_endpoints` is all-or-nothing per endpoint — it returns a fully-paginated record list from one endpoint or raises `NoBookmarkEndpointError`, and on a raise `fetch_to_inventory` propagates the exception and writes nothing. There is no code path by which `fetch_to_inventory` reaches `merge_into_inventory` with a partial page set. No new guard code is needed on the CLI side; the spec records the property so it is not accidentally broken later. (The GUI, which paginates `/fetch` itself and can be interrupted, is **not** naturally safe — see §8 / the GUI requirements doc.)

### 6.4 Backward compatibility

An inventory written by a pre-0.6.0 `bsky-saves` has no lifecycle flags. On the first 0.6.0 fetch:

- Every URI still present gets `last_seen_at = now` and (if applicable) a `subject_status`.
- Every prior URI now absent is treated as Class 1: dropped under `keep-lost` (the default) / `sync`, or retained with `removed_detected_at = now` under `keep-all`.

This means **the first `keep-lost` run after upgrading will drop any entries that were in the old inventory but are no longer on the server** — including posts the user un-saved long ago. This is the documented, intended consequence of `keep-lost` becoming the default (see §7). A user who wants the old additive-everything behaviour runs `--keep-all`. The `README` change and the release notes MUST call this out.

### 6.5 Write-on-every-run consequence

Because `last_seen_at` advances on every fetch, `merge_into_inventory`'s output differs from the prior inventory on essentially every run that has any saves. `fetch_to_inventory`'s existing `if first_run or merged["saves"] != existing["saves"]` write guard therefore becomes almost always true: `fetch` now rewrites the inventory file on every run. This is acceptable — the write is atomic (`atomic_write_inventory`) and the file is small. The guard is left in place (it still correctly no-ops the genuinely-empty case); the consequence is documented here so it is not mistaken for a bug.

## 7. CLI surface

The `fetch` subcommand gains a mutually-exclusive argument group:

- `--mode {sync,keep-lost,keep-all}` — explicit mode selector. **Default: `keep-lost`.**
- `--sync` — convenience alias, equivalent to `--mode sync`.
- `--keep-all` — convenience alias, equivalent to `--mode keep-all`.

The three are mutually exclusive (argparse mutually-exclusive group); supplying more than one is an argument error. The resolved value lands in `args.mode`; `main()` passes `mode=args.mode` into `fetch_to_inventory`.

`keep-lost` is the default because it is the principled middle ground: it honours an explicit un-save (drops Class 1) while protecting against external loss (keeps Class 2). It is **not** a no-op upgrade — see §6.4 — but the convenience alias `--keep-all` gives any user who depends on the old additive behaviour a one-flag path back to it.

No other subcommand changes. `hydrate`, `enrich`, and `serve` are untouched.

## 8. GUI requirements (summary; full doc is the external contract)

`docs/option-b-retain-flag-gui-requirements.md` is written for the `bsky-saves-gui` team and is canonical for the GUI-side work. It specifies:

1. **`subject_status` arrives pre-populated.** Both GUI data paths run `bsky_saves.normalise_record` — the Pyodide path directly, the `/fetch` path server-side in the daemon. Once 0.6.0 ships, `subject_status` is already on every record the GUI receives. The GUI does **not** derive it.
2. **The GUI must reimplement the reconcile.** `mergeHydratedFields` (or a sibling) must be extended to perform the §6.2 record-level reconcile — the union/drop/prune and the lifecycle-flag pass — matching this spec's rules exactly. This is the anti-drift contract.
3. **The four-field schema (§4) and the reconcile rules (§6.2) verbatim**, as the shared spec.
4. **The category predicates (§5.3)** for the filter UI: `synced` / `lost` / `unsaved` / `all`.
5. **The partial-fetch guard.** The GUI paginates `/fetch` itself and can be interrupted; it MUST run the reconcile only when pagination completed (the `/fetch` cursor reached `null`). On an interrupted run it MUST fall back to additive-only behaviour (no absence-detection, no drops) or discard the partial run.
6. **Default mode and default filter.** Recommended: the GUI defaults to **`keep-lost` mode** (data is retained) but its default **view filter is `synced`** — so existing GUI users see exactly what they see today, and discover `lost` / `unsaved` / `all` when they go looking. This neutralises the "why did old bookmarks reappear" surprise without hiding the feature.
7. **A user-facing mode toggle** equivalent to the CLI's `--mode`.

## 9. Why `serve` is untouched

The retain/sync decision needs two inputs: the **prior inventory** and a **complete** fetch. `serve`'s `/fetch` endpoint has neither — it is stateless ("writes nothing to disk, reads no config files") and paginated (one page per call; only the caller knows when the cursor reaches `null`). The only parties positioned to make the decision are the **consumers** — the CLI's `merge_into_inventory` and the GUI's reconcile function. Making `serve` mode-aware would mean making it stateful (holding prior inventory, tracking pagination completion) — which is precisely Option C, the `watch` daemon. For v0.6.0, `/fetch` stays a dumb raw-page provider and the mode lives in each consumer. This keeps `serve`'s stateless-bridge identity intact and means there is exactly one place per consumer where mode logic lives.

## 10. Testing

All in `tests/test_normalize.py` and `tests/test_fetch.py`, matching the existing layout.

### `normalise_record` (`test_normalize.py`)

- Hydrated `item` is `postView` → no `subject_status` key; content extracted as today.
- Hydrated `item` is `notFoundPost` → `subject_status == "not_found"`; content fields empty; no crash.
- Hydrated `item` is `blockedPost` → `subject_status == "blocked"`; content fields empty.
- `listRecords` raw-record shape → `subject_status == "unknown"`.

### `merge_into_inventory` (`test_normalize.py`)

- **Present-entry lifecycle:** `last_seen_at` set/refreshed; `removed_detected_at` cleared on reappearance.
- **`subject_status` reconciliation:** live observation clears a prior status; `not_found`/`blocked` sets status + `subject_status_detected_at`; an unchanged status does not move `subject_status_detected_at`; `"unknown"` does not downgrade a known status; a brand-new `listRecords` URI stores `"unknown"`.
- **Mode `keep-all`:** absent entry retained with `removed_detected_at`; prior hydrated content preserved when the latest `item` is `notFoundPost`.
- **Mode `keep-lost`:** absent entry (Class 1) dropped; present dead-subject entry (Class 2) retained and flagged.
- **Mode `sync`:** absent entry dropped; present dead-subject entry actively pruned; `"unknown"` entry kept; idempotency — a second `sync` run over the same fetch is a no-op on membership.
- **Class 1 masks Class 2:** under `keep-all`, an entry can carry both `removed_detected_at` and a stale `subject_status`.
- **Backward compatibility:** a flag-less prior inventory upgrades correctly under each mode (§6.4).

### `fetch_to_inventory` + CLI (`test_fetch.py`)

- `fetch_to_inventory` passes `mode` and `now` through to `merge_into_inventory`.
- CLI: `--mode` parses each of the three values; `--sync` and `--keep-all` resolve to the right `args.mode`; the default is `keep-lost`; supplying two of the mutually-exclusive flags is an argument error.

## 11. Release

`bsky-saves` is at `0.5.1`. v0.6.0 is a feature release: a new inventory-schema surface (four optional fields) and a new CLI flag, both additive at the wire level but with a documented behaviour change in the default (§6.4). Bump `pyproject.toml` `version = "0.6.0"`. The release notes MUST call out the §6.4 default-mode behaviour change and point users who want the old additive behaviour at `--keep-all`.
