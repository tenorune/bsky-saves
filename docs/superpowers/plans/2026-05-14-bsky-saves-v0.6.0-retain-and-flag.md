# bsky-saves v0.6.0 — Retention Modes and Lifecycle Flags Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `bsky-saves fetch` three retention modes (`sync` / `keep-lost` / `keep-all`) and add four lifecycle flags to inventory entries so retained bookmarks are distinguishable as live / externally-removed / un-saved.

**Architecture:** All retention logic lives in `normalize.py` — `normalise_record` derives a `subject_status` from the `getBookmarks` `item` union, and `merge_into_inventory` is rewritten from a purely-additive union into a three-mode reconcile with an explicit lifecycle-flag pass. `fetch.py` and `cli.py` only plumb the `mode` value through. `serve.py` is untouched. A shared golden-fixture set under `tests/fixtures/retain/` is the executable anti-drift contract with the `bsky-saves-gui` reimplementation.

**Tech Stack:** Python 3.11+, pytest, respx (HTTP mocking). No new dependencies.

**Canonical spec:** `docs/superpowers/specs/2026-05-14-bsky-saves-v0.6.0-retain-and-flag.md`. Section references below (e.g. "spec §6.2") point into it.

---

## File Structure

**Modified:**
- `src/bsky_saves/normalize.py` — `normalise_record` gains `subject_status` derivation; `merge_into_inventory` gains `mode` + `now` params, a lifecycle-flag pass, mode-based absent-entry handling, and the `sync` prune; new private helper `_reconcile_subject_status`; new module constant `_LIFECYCLE_KEYS`.
- `src/bsky_saves/fetch.py` — `fetch_to_inventory` gains a `mode` parameter and passes `mode` + a single `now` timestamp into `merge_into_inventory`.
- `src/bsky_saves/cli.py` — the `fetch` subcommand gains a mutually-exclusive `--mode` / `--sync` / `--keep-all` group; `main()` passes `mode` through.
- `tests/test_normalize.py` — new tests for `subject_status` derivation, `_reconcile_subject_status`, and the three reconcile modes; five existing `merge_into_inventory` calls updated for the new required `now=` kwarg.
- `tests/test_fetch.py` — new tests for `fetch_to_inventory` mode plumbing and CLI argument parsing; one existing test repurposed.
- `tests/test_serve.py` — one new test that `/fetch` propagates `subject_status` end-to-end.
- `README.md` — document the three modes, the aliases, and the four new schema fields.
- `pyproject.toml` — version bump to `0.6.0`.

**Created:**
- `tests/fixtures/retain/*.json` — the shared golden-fixture set.
- `tests/test_retain_fixtures.py` — a parametrized runner that drives every fixture through `merge_into_inventory`.

`serve.py` is intentionally **not** modified — confirmed by grep that only `fetch.py` calls `merge_into_inventory`. The `/fetch` response shape nonetheless gains `subject_status` because `/fetch` runs `normalise_record`; Task 9 verifies that wiring.

---

## Task 1: `normalise_record` — derive `subject_status`

**Files:**
- Modify: `src/bsky_saves/normalize.py:19-83` (the `normalise_record` function)
- Test: `tests/test_normalize.py`

The hydrated `getBookmarks` entry's `item` field is a union: `app.bsky.feed.defs#postView` (live), `#notFoundPost`, or `#blockedPost`. Today `normalise_record` reads `item.get("record", {})` unconditionally, so a deleted-subject bookmark is silently emitted as a content-empty entry indistinguishable from a healthy text-less post. This task branches on `item.$type` and emits `subject_status`. The raw `listRecords` shape (no `item` key) becomes `subject_status = "unknown"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_normalize.py`:

```python
# ---------- normalise_record: subject_status ----------

def test_normalise_record_not_found_post_sets_subject_status():
    raw = {
        "createdAt": "2026-04-22T19:37:34Z",
        "subject": {"uri": "at://author/post1"},
        "item": {
            "$type": "app.bsky.feed.defs#notFoundPost",
            "uri": "at://author/post1",
            "notFound": True,
        },
    }
    entry = normalise_record(raw)
    assert entry["subject_status"] == "not_found"
    assert entry["uri"] == "at://author/post1"
    assert entry["post_text"] == ""
    assert entry["author"] == {"handle": "", "display_name": "", "did": ""}
    assert entry["images"] == []


def test_normalise_record_blocked_post_sets_subject_status():
    raw = {
        "createdAt": "2026-04-22T19:37:34Z",
        "subject": {"uri": "at://author/post1"},
        "item": {
            "$type": "app.bsky.feed.defs#blockedPost",
            "uri": "at://author/post1",
            "blocked": True,
            "author": {"did": "did:plc:author"},
        },
    }
    entry = normalise_record(raw)
    assert entry["subject_status"] == "blocked"
    assert entry["post_text"] == ""


def test_normalise_record_live_post_omits_subject_status():
    raw = {
        "createdAt": "2026-04-22T19:37:34Z",
        "subject": {"uri": "at://author/post1"},
        "item": {
            "$type": "app.bsky.feed.defs#postView",
            "uri": "at://author/post1",
            "author": {"handle": "h", "displayName": "H", "did": "did:plc:h"},
            "record": {"$type": "app.bsky.feed.post", "text": "live post"},
        },
    }
    entry = normalise_record(raw)
    assert "subject_status" not in entry
    assert entry["post_text"] == "live post"


def test_normalise_record_listrecords_shape_is_unknown():
    raw = {
        "uri": "at://did:plc:me/app.bsky.bookmark/rkey1",
        "value": {
            "subject": {"uri": "at://author/post1"},
            "createdAt": "2026-04-12T00:00:00Z",
        },
    }
    entry = normalise_record(raw)
    assert entry["subject_status"] == "unknown"
    assert entry["uri"] == "at://author/post1"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_normalize.py -k subject_status -v`
Expected: 4 FAILED — `KeyError: 'subject_status'` (live test fails on the `not in` assertion only if the key is wrongly present; the other three fail on the missing key).

- [ ] **Step 3: Implement the derivation in `normalise_record`**

Replace the body of `normalise_record` (`src/bsky_saves/normalize.py`, currently lines 19-83) with:

```python
def normalise_record(raw: dict) -> dict:
    """Map a raw bookmark record to the inventory schema."""
    embed_view: dict = {}
    subject_status: str | None = None
    if "item" in raw and isinstance(raw.get("item"), dict):
        # Hydrated `getBookmarks` shape.
        item = raw["item"]
        subject = raw.get("subject", {})
        post_uri = item.get("uri") or subject.get("uri", "")
        saved_at = raw.get("createdAt") or item.get("indexedAt", "")
        item_type = item.get("$type", "")
        if item_type == "app.bsky.feed.defs#notFoundPost":
            subject_status = "not_found"
        elif item_type == "app.bsky.feed.defs#blockedPost":
            subject_status = "blocked"
        record = item.get("record", {})
        post_text = record.get("text", "")
        embed_raw = record.get("embed") or {}
        embed_view = item.get("embed") or {}
        author_raw = item.get("author", {})
    else:
        # Raw `listRecords` shape — no hydrated post content, no subject state.
        subject_status = "unknown"
        value = raw.get("value", raw)
        subject = value.get("subject", value)
        post_uri = subject.get("uri") or raw.get("uri", "")
        saved_at = value.get("createdAt") or raw.get("indexedAt", "")
        post_value = subject.get("value", subject)
        post_text = post_value.get("text", "")
        embed_raw = post_value.get("embed") or {}
        author_raw = subject.get("author", {})

    embed = None
    if embed_raw.get("$type") == "app.bsky.embed.external":
        ext = embed_raw.get("external", {})
        embed = {
            "type": "external",
            "url": ext.get("uri", ""),
            "title": ext.get("title", ""),
            "description": ext.get("description", ""),
        }
    elif embed_raw.get("$type") == "app.bsky.embed.recordWithMedia":
        media_raw = embed_raw.get("media") or {}
        if media_raw.get("$type") == "app.bsky.embed.external":
            ext = media_raw.get("external", {})
            embed = {
                "type": "external",
                "url": ext.get("uri", ""),
                "title": ext.get("title", ""),
                "description": ext.get("description", ""),
            }

    author = {
        "handle": author_raw.get("handle", ""),
        "display_name": author_raw.get("displayName", ""),
        "did": author_raw.get("did", ""),
    }

    images = extract_media(embed_view)
    quoted_post = extract_quoted_post(embed_view)

    entry = {
        "uri": post_uri,
        "saved_at": saved_at,
        "post_text": post_text,
        "embed": embed,
        "author": author,
        "images": images,
    }
    if subject_status is not None:
        entry["subject_status"] = subject_status
    if quoted_post is not None:
        entry["quoted_post"] = quoted_post
    return entry
```

The only changes from today's function: the `subject_status` local initialised to `None`; the `item_type` branch in the hydrated path; `subject_status = "unknown"` at the top of the `else` branch; and the conditional `entry["subject_status"]` assignment near the end.

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python -m pytest tests/test_normalize.py -k subject_status -v`
Expected: 4 PASSED.

- [ ] **Step 5: Run the full normalize test module to confirm no regression**

Run: `python -m pytest tests/test_normalize.py -v`
Expected: all PASSED. (The pre-existing `test_extract_embed_external_*` and `test_extract_handles_missing_embed` tests use the no-`item` shape, so their entries now also carry `subject_status: "unknown"` — but those tests assert specific keys, not the full key set, so they still pass.)

- [ ] **Step 6: Commit**

```bash
git add src/bsky_saves/normalize.py tests/test_normalize.py
git commit -m "feat(normalize): derive subject_status from the getBookmarks item union"
```

---

## Task 2: `_reconcile_subject_status` helper

**Files:**
- Modify: `src/bsky_saves/normalize.py` (add a new private helper, placed directly above `merge_into_inventory`)
- Test: `tests/test_normalize.py`

This pure helper owns the `subject_status` / `subject_status_detected_at` reconciliation rules from spec §6.2. It is a separate, directly-unit-tested function because the logic — especially the downgrade-protected `"unknown"` case — is the part most prone to drift. It mutates a `working` dict in place.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_normalize.py`:

```python
# ---------- _reconcile_subject_status ----------

from bsky_saves.normalize import _reconcile_subject_status

_NOW = "2026-05-14T12:00:00Z"


def test_reconcile_live_clears_prior_status():
    working = {}
    prior = {"subject_status": "not_found", "subject_status_detected_at": "2026-05-01T00:00:00Z"}
    fresh = {}  # no subject_status -> live
    _reconcile_subject_status(working, prior, fresh, _NOW)
    assert "subject_status" not in working
    assert "subject_status_detected_at" not in working


def test_reconcile_not_found_on_brand_new_uri_sets_timestamp():
    working = {}
    _reconcile_subject_status(working, None, {"subject_status": "not_found"}, _NOW)
    assert working["subject_status"] == "not_found"
    assert working["subject_status_detected_at"] == _NOW


def test_reconcile_unchanged_status_carries_timestamp_forward():
    working = {}
    prior = {"subject_status": "not_found", "subject_status_detected_at": "2026-05-01T00:00:00Z"}
    _reconcile_subject_status(working, prior, {"subject_status": "not_found"}, _NOW)
    assert working["subject_status"] == "not_found"
    assert working["subject_status_detected_at"] == "2026-05-01T00:00:00Z"


def test_reconcile_changed_status_is_a_transition():
    working = {}
    prior = {"subject_status": "not_found", "subject_status_detected_at": "2026-05-01T00:00:00Z"}
    _reconcile_subject_status(working, prior, {"subject_status": "blocked"}, _NOW)
    assert working["subject_status"] == "blocked"
    assert working["subject_status_detected_at"] == _NOW


def test_reconcile_unknown_to_known_is_a_transition():
    working = {}
    prior = {"subject_status": "unknown"}
    _reconcile_subject_status(working, prior, {"subject_status": "not_found"}, _NOW)
    assert working["subject_status"] == "not_found"
    assert working["subject_status_detected_at"] == _NOW


def test_reconcile_unknown_on_brand_new_uri_stores_unknown_no_timestamp():
    working = {}
    _reconcile_subject_status(working, None, {"subject_status": "unknown"}, _NOW)
    assert working["subject_status"] == "unknown"
    assert "subject_status_detected_at" not in working


def test_reconcile_unknown_is_noop_over_existing_known_status():
    working = {}
    prior = {"subject_status": "not_found", "subject_status_detected_at": "2026-05-01T00:00:00Z"}
    _reconcile_subject_status(working, prior, {"subject_status": "unknown"}, _NOW)
    assert working["subject_status"] == "not_found"
    assert working["subject_status_detected_at"] == "2026-05-01T00:00:00Z"


def test_reconcile_unknown_is_noop_over_existing_live_entry():
    working = {}
    prior = {}  # prior exists but was live (no subject_status)
    _reconcile_subject_status(working, prior, {"subject_status": "unknown"}, _NOW)
    assert "subject_status" not in working
    assert "subject_status_detected_at" not in working
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_normalize.py -k reconcile -v`
Expected: collection ERROR — `ImportError: cannot import name '_reconcile_subject_status'`.

- [ ] **Step 3: Implement the helper**

In `src/bsky_saves/normalize.py`, add this function immediately above `def merge_into_inventory`:

```python
def _reconcile_subject_status(
    working: dict, prior: dict | None, fresh: dict, now: str
) -> None:
    """Reconcile subject_status / subject_status_detected_at on `working`.

    `prior` is the entry as it stood before this merge (or None for a
    brand-new URI); `fresh` is the newly-fetched normalised record. `working`
    is mutated in place. See the v0.6.0 spec section 6.2.
    """
    fresh_status = fresh.get("subject_status")
    prior_status = prior.get("subject_status") if prior is not None else None
    prior_detected = (
        prior.get("subject_status_detected_at") if prior is not None else None
    )

    working.pop("subject_status", None)
    working.pop("subject_status_detected_at", None)

    if fresh_status is None:
        # Live observation — both fields stay cleared.
        return
    if fresh_status in ("not_found", "blocked"):
        working["subject_status"] = fresh_status
        if prior_status == fresh_status and prior_detected is not None:
            working["subject_status_detected_at"] = prior_detected
        else:
            working["subject_status_detected_at"] = now
        return
    # fresh_status == "unknown": never overwrites, weakens, or clears an
    # existing entry; stored only for a brand-new URI; never sets the
    # timestamp.
    if prior is None:
        working["subject_status"] = "unknown"
    else:
        if prior_status is not None:
            working["subject_status"] = prior_status
        if prior_detected is not None:
            working["subject_status_detected_at"] = prior_detected
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_normalize.py -k reconcile -v`
Expected: 8 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/normalize.py tests/test_normalize.py
git commit -m "feat(normalize): add _reconcile_subject_status lifecycle helper"
```

---

## Task 3: `merge_into_inventory` — new signature + present-entry path

**Files:**
- Modify: `src/bsky_saves/normalize.py:191-221` (the `merge_into_inventory` function; add the `_LIFECYCLE_KEYS` constant above it)
- Test: `tests/test_normalize.py`

This task changes `merge_into_inventory`'s signature (adds keyword-only `mode` and required keyword-only `now`) and rewrites the **present-entry** path: field-fill that skips lifecycle keys, plus the lifecycle-flag pass (`last_seen_at`, clearing `removed_detected_at`, calling `_reconcile_subject_status`). **Absent entries are left in place untouched in this task** — mode-based absent handling comes in Task 4. Because the signature gains a required `now=`, the five existing `merge_into_inventory` test calls must be updated.

- [ ] **Step 1: Write the failing tests for the present-entry path**

Append to `tests/test_normalize.py`:

```python
# ---------- merge_into_inventory: present-entry lifecycle ----------

def test_merge_sets_last_seen_at_on_present_entry():
    existing = {"fetched_at": "2026-05-01T00:00:00Z", "saves": [
        {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "p",
         "embed": None, "author": {}, "images": []},
    ]}
    new_entries = [
        {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "p",
         "embed": None, "author": {}, "images": []},
    ]
    merged = merge_into_inventory(existing, new_entries, mode="keep-lost", now=_NOW)
    assert merged["saves"][0]["last_seen_at"] == _NOW


def test_merge_clears_removed_detected_at_on_reappearance():
    existing = {"fetched_at": "2026-05-01T00:00:00Z", "saves": [
        {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "p",
         "embed": None, "author": {}, "images": [],
         "removed_detected_at": "2026-05-10T00:00:00Z"},
    ]}
    new_entries = [
        {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "p",
         "embed": None, "author": {}, "images": []},
    ]
    merged = merge_into_inventory(existing, new_entries, mode="keep-lost", now=_NOW)
    assert "removed_detected_at" not in merged["saves"][0]


def test_merge_applies_subject_status_for_present_dead_subject():
    existing = {"fetched_at": None, "saves": []}
    new_entries = [
        {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "",
         "embed": None, "author": {}, "images": [], "subject_status": "not_found"},
    ]
    merged = merge_into_inventory(existing, new_entries, mode="keep-lost", now=_NOW)
    assert merged["saves"][0]["subject_status"] == "not_found"
    assert merged["saves"][0]["subject_status_detected_at"] == _NOW


def test_merge_now_is_keyword_only_and_required():
    import pytest
    with pytest.raises(TypeError):
        merge_into_inventory({"fetched_at": None, "saves": []}, [])


def test_merge_preserves_prior_content_when_subject_dies():
    """When the fresh record is a content-empty dead-subject entry, the prior
    hydrated content (post_text / author / images) is preserved by field-fill
    while the new subject_status is still applied."""
    existing = {"fetched_at": "2026-05-01T00:00:00Z", "saves": [
        {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z",
         "post_text": "the original text", "embed": None,
         "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"},
         "images": [{"kind": "image", "url": "https://cdn/x.jpg", "alt": ""}]},
    ]}
    new_entries = [
        {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "",
         "embed": None, "author": {"handle": "", "display_name": "", "did": ""},
         "images": [], "subject_status": "not_found"},
    ]
    merged = merge_into_inventory(existing, new_entries, mode="keep-lost", now=_NOW)
    entry = merged["saves"][0]
    assert entry["post_text"] == "the original text"
    assert entry["author"]["handle"] == "a"
    assert entry["images"] == [{"kind": "image", "url": "https://cdn/x.jpg", "alt": ""}]
    assert entry["subject_status"] == "not_found"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_normalize.py -k "merge_sets_last_seen or merge_clears_removed or merge_applies_subject or merge_now_is_keyword or merge_preserves_prior_content" -v`
Expected: FAILED — `test_merge_now_is_keyword_only_and_required` fails because today's signature accepts the 2-arg call without raising; the other three fail with `TypeError: merge_into_inventory() got an unexpected keyword argument 'mode'`.

- [ ] **Step 3: Add the `_LIFECYCLE_KEYS` constant and rewrite the present-entry path**

In `src/bsky_saves/normalize.py`, add this constant directly above `_reconcile_subject_status` (which Task 2 placed above `merge_into_inventory`):

```python
_LIFECYCLE_KEYS = frozenset(
    {"subject_status", "subject_status_detected_at", "last_seen_at", "removed_detected_at"}
)
```

Then replace the entire `merge_into_inventory` function (currently lines 191-221) with:

```python
def merge_into_inventory(
    existing: dict,
    new_entries: list[dict],
    *,
    mode: str = "keep-lost",
    now: str,
) -> dict:
    """Merge new_entries into existing inventory under a retention mode.

    ``mode`` is one of "sync", "keep-lost" (default), "keep-all". ``now`` is
    the fetch timestamp, written into the lifecycle flags. See the v0.6.0 spec
    section 6.2 for the full algorithm.

    Present entries: field-fill (never overwrite a non-empty existing value;
    lifecycle keys are owned by the flag pass, not the field-fill) plus a
    lifecycle-flag pass. Absent entries (Class 1) and the sync prune are added
    in later tasks.
    """
    by_uri: dict[str, dict] = {s["uri"]: dict(s) for s in existing.get("saves", [])}
    fetched_uris = {e["uri"] for e in new_entries if e.get("uri")}

    for entry in new_entries:
        uri = entry.get("uri", "")
        if not uri:
            continue
        prior = by_uri.get(uri)
        if prior is not None:
            prior_snapshot = dict(prior)
            working = prior
            for k, v in entry.items():
                if k in _LIFECYCLE_KEYS:
                    continue
                cur = working.get(k)
                if cur in (None, "", [], {}):
                    working[k] = v
        else:
            prior_snapshot = None
            working = dict(entry)
            by_uri[uri] = working
        working["last_seen_at"] = now
        working.pop("removed_detected_at", None)
        _reconcile_subject_status(working, prior_snapshot, entry, now)

    saves = sorted(by_uri.values(), key=lambda s: s.get("saved_at", ""), reverse=True)
    return {
        "fetched_at": existing.get("fetched_at"),
        "saves": saves,
    }
```

`fetched_uris` is computed now (it is unused until Task 4, but defining it here keeps the diff in Task 4 small and the variable's meaning obvious).

- [ ] **Step 4: Update the five existing `merge_into_inventory` test calls**

In `tests/test_normalize.py`, the pre-existing tests call `merge_into_inventory` with the old 2-argument signature. Update each call to pass `now=_NOW` (the `_NOW` module global was defined in Task 2's appended test block; because it is a module-level constant it is in scope for every test in the file regardless of where in the file it is defined).

Change these four calls (add `, now=_NOW` before the closing paren):
- `test_merge_preserves_existing_entries`: `merge_into_inventory(existing, new_entries, now=_NOW)`
- `test_merge_backfills_missing_fields`: `merge_into_inventory(existing, new_entries, now=_NOW)`
- `test_merge_backfills_empty_existing_field`: `merge_into_inventory(existing, new_entries, now=_NOW)`
- `test_merge_sorts_by_saved_at_desc`: `merge_into_inventory(existing, new_entries, now=_NOW)`

Then **replace** `test_merge_idempotent_when_no_new_saves` entirely (its full-`json.dumps` equality check breaks now that `last_seen_at` is added) with:

```python
def test_merge_adds_last_seen_at_without_disturbing_content():
    seed = {
        "fetched_at": "2026-04-01T00:00:00Z",
        "saves": [
            {
                "uri": "at://x/1",
                "saved_at": "2026-04-01T12:00:00Z",
                "post_text": "p",
                "embed": None,
                "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"},
            }
        ],
    }
    new_entries = [dict(seed["saves"][0])]
    merged = merge_into_inventory(seed, new_entries, mode="keep-lost", now=_NOW)
    assert len(merged["saves"]) == 1
    entry = merged["saves"][0]
    assert entry["last_seen_at"] == _NOW
    content = {k: v for k, v in entry.items() if k != "last_seen_at"}
    assert content == seed["saves"][0]
```

- [ ] **Step 5: Run the present-entry tests and the full module to verify all pass**

Run: `python -m pytest tests/test_normalize.py -v`
Expected: all PASSED — the four new present-entry tests, the rewritten `test_merge_adds_last_seen_at_without_disturbing_content`, and every pre-existing test (with their updated `now=_NOW` calls).

- [ ] **Step 6: Commit**

```bash
git add src/bsky_saves/normalize.py tests/test_normalize.py
git commit -m "feat(normalize): merge_into_inventory gains mode/now params and present-entry lifecycle pass"
```

---

## Task 4: `merge_into_inventory` — absent-entry mode handling

**Files:**
- Modify: `src/bsky_saves/normalize.py` (the `merge_into_inventory` function)
- Test: `tests/test_normalize.py`

A "Class 1" entry is a prior URI absent from the fetch — the user un-saved it. `keep-all` retains it and stamps `removed_detected_at`; `keep-lost` and `sync` drop it. This task adds the absent-entry loop between the present-entry loop and the final sort.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_normalize.py`:

```python
# ---------- merge_into_inventory: absent-entry (Class 1) handling ----------

def _inv_with(*entries):
    return {"fetched_at": "2026-05-01T00:00:00Z", "saves": list(entries)}


def _entry(uri, saved_at, **extra):
    base = {"uri": uri, "saved_at": saved_at, "post_text": "p",
            "embed": None, "author": {}, "images": []}
    base.update(extra)
    return base


def test_merge_keep_lost_drops_absent_entry():
    existing = _inv_with(
        _entry("at://x/1", "2026-04-10T00:00:00Z"),
        _entry("at://x/2", "2026-04-11T00:00:00Z"),
    )
    new_entries = [_entry("at://x/1", "2026-04-10T00:00:00Z")]
    merged = merge_into_inventory(existing, new_entries, mode="keep-lost", now=_NOW)
    uris = {s["uri"] for s in merged["saves"]}
    assert uris == {"at://x/1"}


def test_merge_sync_drops_absent_entry():
    existing = _inv_with(
        _entry("at://x/1", "2026-04-10T00:00:00Z"),
        _entry("at://x/2", "2026-04-11T00:00:00Z"),
    )
    new_entries = [_entry("at://x/1", "2026-04-10T00:00:00Z")]
    merged = merge_into_inventory(existing, new_entries, mode="sync", now=_NOW)
    uris = {s["uri"] for s in merged["saves"]}
    assert uris == {"at://x/1"}


def test_merge_keep_all_retains_absent_entry_and_flags_it():
    existing = _inv_with(
        _entry("at://x/1", "2026-04-10T00:00:00Z"),
        _entry("at://x/2", "2026-04-11T00:00:00Z", last_seen_at="2026-05-01T00:00:00Z"),
    )
    new_entries = [_entry("at://x/1", "2026-04-10T00:00:00Z")]
    merged = merge_into_inventory(existing, new_entries, mode="keep-all", now=_NOW)
    by_uri = {s["uri"]: s for s in merged["saves"]}
    assert set(by_uri) == {"at://x/1", "at://x/2"}
    assert by_uri["at://x/2"]["removed_detected_at"] == _NOW
    # last_seen_at on an absent entry stays at its prior value, not `now`.
    assert by_uri["at://x/2"]["last_seen_at"] == "2026-05-01T00:00:00Z"


def test_merge_keep_all_does_not_restamp_existing_removed_detected_at():
    existing = _inv_with(
        _entry("at://x/2", "2026-04-11T00:00:00Z",
               removed_detected_at="2026-05-09T00:00:00Z"),
    )
    merged = merge_into_inventory(existing, [], mode="keep-all", now=_NOW)
    assert merged["saves"][0]["removed_detected_at"] == "2026-05-09T00:00:00Z"


def test_merge_keep_all_class1_masks_class2():
    # An entry whose post died (Class 2 — subject_status set) and which the
    # user then un-saved (Class 1 — now absent) carries BOTH flags under
    # keep-all. The absent-entry path does not touch subject_status.
    existing = _inv_with(
        _entry("at://x/2", "2026-04-11T00:00:00Z",
               subject_status="not_found",
               subject_status_detected_at="2026-05-05T00:00:00Z"),
    )
    merged = merge_into_inventory(existing, [], mode="keep-all", now=_NOW)
    entry = merged["saves"][0]
    assert entry["removed_detected_at"] == _NOW
    assert entry["subject_status"] == "not_found"
    assert entry["subject_status_detected_at"] == "2026-05-05T00:00:00Z"


def test_merge_backward_compat_flagless_prior_upgrades():
    # A pre-0.6.0 inventory entry has none of the four flag fields.
    existing = _inv_with(_entry("at://x/1", "2026-04-10T00:00:00Z"))
    new_entries = [_entry("at://x/1", "2026-04-10T00:00:00Z")]
    merged = merge_into_inventory(existing, new_entries, mode="keep-lost", now=_NOW)
    assert merged["saves"][0]["last_seen_at"] == _NOW
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_normalize.py -k "drops_absent or retains_absent or restamp or class1_masks or backward_compat" -v`
Expected: `test_merge_keep_lost_drops_absent_entry` and `test_merge_sync_drops_absent_entry` FAIL (the absent entry is still present — assertion on the URI set fails); `test_merge_keep_all_retains_absent_entry_and_flags_it` and `test_merge_keep_all_class1_masks_class2` FAIL (`KeyError: 'removed_detected_at'`). `test_merge_keep_all_does_not_restamp...` and `test_merge_backward_compat...` may already pass coincidentally — that is fine.

- [ ] **Step 3: Add the absent-entry loop**

In `merge_into_inventory`, insert this block immediately **after** the present-entry `for entry in new_entries:` loop and **before** the `saves = sorted(...)` line:

```python
    # Absent entries (Class 1 — the user un-saved them).
    for uri, entry in list(by_uri.items()):
        if uri in fetched_uris:
            continue
        if mode == "keep-all":
            entry.setdefault("removed_detected_at", now)
        else:  # keep-lost or sync
            del by_uri[uri]
```

Also update the function docstring: change the last sentence from "Absent entries (Class 1) and the sync prune are added in later tasks." to "Absent entries (Class 1) are dropped under keep-lost/sync and flagged under keep-all; the sync prune is added in the next task."

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_normalize.py -v`
Expected: all PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/normalize.py tests/test_normalize.py
git commit -m "feat(normalize): merge_into_inventory absent-entry mode handling"
```

---

## Task 5: `merge_into_inventory` — `sync` active prune

**Files:**
- Modify: `src/bsky_saves/normalize.py` (the `merge_into_inventory` function)
- Test: `tests/test_normalize.py`

A "Class 2" entry — bookmark record still in the repo, but its post was deleted/blocked — is *never absent* from a fetch, so reconcile-by-absence cannot exclude it. `sync` mode must therefore **actively prune** entries whose `subject_status` is `not_found` or `blocked`. `"unknown"` entries are kept (not *known* to be dead). `keep-lost` and `keep-all` do not prune.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_normalize.py`:

```python
# ---------- merge_into_inventory: sync active prune (Class 2) ----------

def test_merge_sync_prunes_present_dead_subject():
    existing = {"fetched_at": "2026-05-01T00:00:00Z", "saves": []}
    new_entries = [
        _entry("at://x/1", "2026-04-10T00:00:00Z"),
        _entry("at://x/2", "2026-04-11T00:00:00Z", subject_status="not_found"),
    ]
    merged = merge_into_inventory(existing, new_entries, mode="sync", now=_NOW)
    uris = {s["uri"] for s in merged["saves"]}
    assert uris == {"at://x/1"}


def test_merge_keep_lost_retains_present_dead_subject():
    existing = {"fetched_at": "2026-05-01T00:00:00Z", "saves": []}
    new_entries = [
        _entry("at://x/1", "2026-04-10T00:00:00Z"),
        _entry("at://x/2", "2026-04-11T00:00:00Z", subject_status="not_found"),
    ]
    merged = merge_into_inventory(existing, new_entries, mode="keep-lost", now=_NOW)
    by_uri = {s["uri"]: s for s in merged["saves"]}
    assert set(by_uri) == {"at://x/1", "at://x/2"}
    assert by_uri["at://x/2"]["subject_status"] == "not_found"


def test_merge_sync_keeps_unknown_subject():
    existing = {"fetched_at": "2026-05-01T00:00:00Z", "saves": []}
    new_entries = [_entry("at://x/1", "2026-04-10T00:00:00Z", subject_status="unknown")]
    merged = merge_into_inventory(existing, new_entries, mode="sync", now=_NOW)
    assert {s["uri"] for s in merged["saves"]} == {"at://x/1"}


def test_merge_sync_is_idempotent_on_membership():
    existing = {"fetched_at": "2026-05-01T00:00:00Z", "saves": []}
    new_entries = [
        _entry("at://x/1", "2026-04-10T00:00:00Z"),
        _entry("at://x/2", "2026-04-11T00:00:00Z", subject_status="blocked"),
    ]
    first = merge_into_inventory(existing, new_entries, mode="sync", now=_NOW)
    second = merge_into_inventory(first, new_entries, mode="sync", now="2026-05-15T00:00:00Z")
    assert {s["uri"] for s in first["saves"]} == {s["uri"] for s in second["saves"]} == {"at://x/1"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_normalize.py -k "sync_prunes or keep_lost_retains_present or sync_keeps_unknown or sync_is_idempotent" -v`
Expected: `test_merge_sync_prunes_present_dead_subject` and `test_merge_sync_is_idempotent_on_membership` FAIL (the dead-subject entry is still present). `test_merge_keep_lost_retains_present_dead_subject` and `test_merge_sync_keeps_unknown_subject` should already PASS — that is fine, they guard against over-pruning.

- [ ] **Step 3: Add the sync prune**

In `merge_into_inventory`, insert this block immediately **after** the absent-entry loop and **before** the `saves = sorted(...)` line:

```python
    # sync mode actively prunes Class 2 entries — present bookmarks whose
    # subject post is known to be gone. "unknown" is not *known* dead, so it
    # is kept.
    if mode == "sync":
        for uri, entry in list(by_uri.items()):
            if entry.get("subject_status") in ("not_found", "blocked"):
                del by_uri[uri]
```

Update the function docstring's last sentence to: "Absent entries (Class 1) are dropped under keep-lost/sync and flagged under keep-all; sync additionally prunes Class 2 entries (present bookmarks with a dead subject)."

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_normalize.py -v`
Expected: all PASSED. `merge_into_inventory` is now feature-complete per spec §6.2.

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/normalize.py tests/test_normalize.py
git commit -m "feat(normalize): merge_into_inventory sync-mode Class 2 prune"
```

---

## Task 6: `fetch_to_inventory` — plumb the `mode` parameter

**Files:**
- Modify: `src/bsky_saves/fetch.py:294-351` (the `fetch_to_inventory` function)
- Test: `tests/test_fetch.py`

`fetch_to_inventory` gains a `mode` parameter and passes it — plus a single `now` timestamp shared between the lifecycle flags and `fetched_at` — into `merge_into_inventory`. One pre-existing test (`test_fetch_to_inventory_no_write_when_no_new_saves`) asserts behaviour that spec §6.5 explicitly changes (`last_seen_at` now advances every run, so the inventory is rewritten every run); it is repurposed.

- [ ] **Step 1: Write the failing test and repurpose the obsolete one**

In `tests/test_fetch.py`, **replace** `test_fetch_to_inventory_no_write_when_no_new_saves` (and its docstring) entirely with:

```python
@respx.mock
def test_fetch_to_inventory_advances_last_seen_at_every_run(tmp_path, monkeypatch):
    """Per spec section 6.5: because last_seen_at advances on every run, a
    second fetch of the same bookmarks rewrites the inventory (the old
    "no write when unchanged" behaviour is intentionally gone)."""
    _mock_create_session()
    respx.get(f"{PDS_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200, json={"bookmarks": [_bookmark_record("at://x/p/1")]}
        )
    )
    inv_path = tmp_path / "inv.json"
    timestamps = iter(["2026-04-12T00:00:00Z", "2026-04-12T01:00:00Z"])
    monkeypatch.setattr(_fetch_mod, "_now_iso", lambda: next(timestamps))

    _fetch_mod.fetch_to_inventory(
        inv_path, handle="user.bsky.social", app_password="app-password",
        pds_base=PDS_BASE, appview_base=APPVIEW_BASE,
    )
    first = json.loads(inv_path.read_text(encoding="utf-8"))
    assert first["saves"][0]["last_seen_at"] == "2026-04-12T00:00:00Z"

    respx.reset()
    _mock_create_session()
    respx.get(f"{PDS_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200, json={"bookmarks": [_bookmark_record("at://x/p/1")]}
        )
    )
    _fetch_mod.fetch_to_inventory(
        inv_path, handle="user.bsky.social", app_password="app-password",
        pds_base=PDS_BASE, appview_base=APPVIEW_BASE,
    )
    second = json.loads(inv_path.read_text(encoding="utf-8"))
    assert second["saves"][0]["last_seen_at"] == "2026-04-12T01:00:00Z"
    assert second["fetched_at"] == "2026-04-12T01:00:00Z"


@respx.mock
def test_fetch_to_inventory_keep_lost_drops_unsaved_bookmark(tmp_path):
    """A bookmark present on the first fetch but gone on the second is
    dropped under the default keep-lost mode."""
    _mock_create_session()
    respx.get(f"{PDS_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200, json={"bookmarks": [
                _bookmark_record("at://x/p/1"),
                _bookmark_record("at://x/p/2", saved_at="2026-04-13T00:00:00Z"),
            ]}
        )
    )
    inv_path = tmp_path / "inv.json"
    _fetch_mod.fetch_to_inventory(
        inv_path, handle="user.bsky.social", app_password="app-password",
        pds_base=PDS_BASE, appview_base=APPVIEW_BASE,
    )
    assert len(json.loads(inv_path.read_text(encoding="utf-8"))["saves"]) == 2

    respx.reset()
    _mock_create_session()
    respx.get(f"{PDS_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200, json={"bookmarks": [_bookmark_record("at://x/p/1")]}
        )
    )
    _fetch_mod.fetch_to_inventory(
        inv_path, handle="user.bsky.social", app_password="app-password",
        pds_base=PDS_BASE, appview_base=APPVIEW_BASE, mode="keep-lost",
    )
    saves = json.loads(inv_path.read_text(encoding="utf-8"))["saves"]
    assert {s["uri"] for s in saves} == {"at://x/p/1"}


@respx.mock
def test_fetch_to_inventory_keep_all_retains_unsaved_bookmark(tmp_path):
    """Under keep-all, the gone bookmark is retained and flagged."""
    _mock_create_session()
    respx.get(f"{PDS_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200, json={"bookmarks": [
                _bookmark_record("at://x/p/1"),
                _bookmark_record("at://x/p/2", saved_at="2026-04-13T00:00:00Z"),
            ]}
        )
    )
    inv_path = tmp_path / "inv.json"
    _fetch_mod.fetch_to_inventory(
        inv_path, handle="user.bsky.social", app_password="app-password",
        pds_base=PDS_BASE, appview_base=APPVIEW_BASE,
    )

    respx.reset()
    _mock_create_session()
    respx.get(f"{PDS_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200, json={"bookmarks": [_bookmark_record("at://x/p/1")]}
        )
    )
    _fetch_mod.fetch_to_inventory(
        inv_path, handle="user.bsky.social", app_password="app-password",
        pds_base=PDS_BASE, appview_base=APPVIEW_BASE, mode="keep-all",
    )
    by_uri = {s["uri"]: s for s in json.loads(inv_path.read_text(encoding="utf-8"))["saves"]}
    assert set(by_uri) == {"at://x/p/1", "at://x/p/2"}
    assert "removed_detected_at" in by_uri["at://x/p/2"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_fetch.py -k "advances_last_seen or keep_lost_drops or keep_all_retains" -v`
Expected: FAILED — `test_fetch_to_inventory_keep_lost_drops_unsaved_bookmark` and `..._keep_all_retains...` fail with `TypeError: fetch_to_inventory() got an unexpected keyword argument 'mode'`; `test_fetch_to_inventory_advances_last_seen_at_every_run` fails with `KeyError: 'last_seen_at'`.

- [ ] **Step 3: Add the `mode` parameter and update the merge call**

In `src/bsky_saves/fetch.py`, change the `fetch_to_inventory` signature (currently lines 294-302) to add `mode`:

```python
def fetch_to_inventory(
    inventory_path: Path,
    *,
    handle: str,
    app_password: str,
    pds_base: str = "https://bsky.social",
    appview_base: str = "https://bsky.social",
    appview_did_candidates: list[str] | None = None,
    mode: str = "keep-lost",
) -> int:
```

Then, in the same function, replace this block (currently around lines 334-346):

```python
    new_entries = [normalise_record(r) for r in raw]

    first_run = not inventory_path.exists()
    if first_run:
        existing = {"fetched_at": None, "saves": []}
    else:
        existing = json.loads(inventory_path.read_text(encoding="utf-8"))
    merged = merge_into_inventory(existing, new_entries)

    if first_run or merged["saves"] != existing["saves"]:
        merged["fetched_at"] = _now_iso()
        inventory_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_inventory(inventory_path, merged)
```

with:

```python
    new_entries = [normalise_record(r) for r in raw]

    first_run = not inventory_path.exists()
    if first_run:
        existing = {"fetched_at": None, "saves": []}
    else:
        existing = json.loads(inventory_path.read_text(encoding="utf-8"))
    now = _now_iso()
    merged = merge_into_inventory(existing, new_entries, mode=mode, now=now)

    if first_run or merged["saves"] != existing["saves"]:
        merged["fetched_at"] = now
        inventory_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_inventory(inventory_path, merged)
```

The single `now = _now_iso()` call is shared between the lifecycle flags and `fetched_at` so a run is internally consistent. The write guard is left in place — per spec §6.5 it is now almost always true (because `last_seen_at` advances), but it still correctly no-ops the genuinely-empty case.

- [ ] **Step 4: Run the new tests, then the full fetch module**

Run: `python -m pytest tests/test_fetch.py -v`
Expected: all PASSED — the three new/repurposed tests plus every pre-existing `test_fetch.py` test. (`test_fetch_to_inventory_writes_when_new_saves` and `test_fetch_to_inventory_creates_file_on_first_run_with_zero_records` are unaffected: the first asserts a write happened, the second is a first-run case that always writes.)

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/fetch.py tests/test_fetch.py
git commit -m "feat(fetch): fetch_to_inventory plumbs the retention mode"
```

---

## Task 7: CLI — `--mode` / `--sync` / `--keep-all` on `fetch`

**Files:**
- Modify: `src/bsky_saves/cli.py:57-68` (the `fetch` subparser) and `src/bsky_saves/cli.py:165-171` (the `fetch` dispatch in `main`)
- Test: `tests/test_fetch.py`

The `fetch` subcommand gets a mutually-exclusive group: `--mode {sync,keep-lost,keep-all}` plus `--sync` and `--keep-all` convenience aliases. The default is `keep-lost`, set via `set_defaults` (the robust idiom when several mutually-exclusive args share one `dest`). `main()` passes `mode=args.mode` into `fetch_to_inventory`. There are no existing CLI tests; this task adds the first.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fetch.py`:

```python
# --- CLI argument parsing for `fetch` retention modes ---

import pytest
from bsky_saves.cli import _build_parser


def test_cli_fetch_mode_defaults_to_keep_lost():
    args = _build_parser().parse_args(["fetch", "--inventory", "inv.json"])
    assert args.mode == "keep-lost"


def test_cli_fetch_mode_explicit():
    args = _build_parser().parse_args(
        ["fetch", "--inventory", "inv.json", "--mode", "sync"]
    )
    assert args.mode == "sync"


def test_cli_fetch_sync_alias():
    args = _build_parser().parse_args(["fetch", "--inventory", "inv.json", "--sync"])
    assert args.mode == "sync"


def test_cli_fetch_keep_all_alias():
    args = _build_parser().parse_args(
        ["fetch", "--inventory", "inv.json", "--keep-all"]
    )
    assert args.mode == "keep-all"


def test_cli_fetch_mode_aliases_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(
            ["fetch", "--inventory", "inv.json", "--sync", "--keep-all"]
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_fetch.py -k "cli_fetch" -v`
Expected: FAILED — `test_cli_fetch_mode_defaults_to_keep_lost` fails with `AttributeError: 'Namespace' object has no attribute 'mode'`; the alias tests fail because argparse rejects the unrecognised `--mode` / `--sync` / `--keep-all` arguments with `SystemExit`.

- [ ] **Step 3: Add the mutually-exclusive group to the `fetch` subparser**

In `src/bsky_saves/cli.py`, the `fetch` subparser is built around lines 57-68. Immediately **after** the existing `p_fetch.add_argument("--appview", ...)` block (i.e. after line 68, before `p_hydrate = ...`), add:

```python
    fetch_mode_group = p_fetch.add_mutually_exclusive_group()
    fetch_mode_group.add_argument(
        "--mode",
        choices=["sync", "keep-lost", "keep-all"],
        dest="mode",
        help=(
            "Inventory retention policy (default: keep-lost). "
            "sync: mirror only what is live on the server. "
            "keep-lost: also keep posts removed outside your control. "
            "keep-all: also keep bookmarks you deliberately un-saved."
        ),
    )
    fetch_mode_group.add_argument(
        "--sync",
        action="store_const",
        const="sync",
        dest="mode",
        help="Alias for --mode sync.",
    )
    fetch_mode_group.add_argument(
        "--keep-all",
        action="store_const",
        const="keep-all",
        dest="mode",
        help="Alias for --mode keep-all.",
    )
    p_fetch.set_defaults(mode="keep-lost")
```

- [ ] **Step 4: Pass `mode` through in `main()`**

In `src/bsky_saves/cli.py`, the `fetch` branch of `main()` (lines 165-171) calls `fetch_to_inventory`. Add `mode=args.mode` to that call:

```python
        fetch_to_inventory(
            args.inventory,
            handle=handle,
            app_password=app_password,
            pds_base=args.pds,
            appview_base=args.appview,
            mode=args.mode,
        )
```

- [ ] **Step 5: Run the CLI tests, then the full fetch module**

Run: `python -m pytest tests/test_fetch.py -v`
Expected: all PASSED.

- [ ] **Step 6: Commit**

```bash
git add src/bsky_saves/cli.py tests/test_fetch.py
git commit -m "feat(cli): fetch gains --mode/--sync/--keep-all retention flags"
```

---

## Task 8: Shared golden fixtures + runner

**Files:**
- Create: `tests/fixtures/retain/keep-lost-drops-unsaved.json`
- Create: `tests/fixtures/retain/keep-all-flags-unsaved.json`
- Create: `tests/fixtures/retain/sync-prunes-dead-subject.json`
- Create: `tests/fixtures/retain/reappearance-clears-removed-detected-at.json`
- Create: `tests/fixtures/retain/unknown-is-noop-on-existing.json`
- Create: `tests/test_retain_fixtures.py`

Per spec §10.4, these golden fixtures are the executable anti-drift contract: the `bsky-saves-gui` test suite consumes the *same JSON files* and asserts its TypeScript reconcile produces the same output. Each fixture is `{description, mode, now, prior_inventory, fetch_records, expected_output_inventory}`. `fetch_records` are already-normalised entries (post-`normalise_record`).

- [ ] **Step 1: Create the fixture directory and the five fixture files**

Create `tests/fixtures/retain/keep-lost-drops-unsaved.json`:

```json
{
  "description": "keep-lost drops a bookmark the user un-saved (absent from the fetch) and refreshes last_seen_at on the survivor",
  "mode": "keep-lost",
  "now": "2026-05-14T12:00:00Z",
  "prior_inventory": {
    "fetched_at": "2026-05-01T00:00:00Z",
    "saves": [
      {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": [], "last_seen_at": "2026-05-01T00:00:00Z"},
      {"uri": "at://x/2", "saved_at": "2026-04-11T00:00:00Z", "post_text": "post B", "embed": null, "author": {"handle": "b", "display_name": "B", "did": "did:plc:b"}, "images": [], "last_seen_at": "2026-05-01T00:00:00Z"}
    ]
  },
  "fetch_records": [
    {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": []}
  ],
  "expected_output_inventory": {
    "fetched_at": "2026-05-01T00:00:00Z",
    "saves": [
      {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": [], "last_seen_at": "2026-05-14T12:00:00Z"}
    ]
  }
}
```

Create `tests/fixtures/retain/keep-all-flags-unsaved.json`:

```json
{
  "description": "keep-all retains an un-saved bookmark and stamps removed_detected_at; its last_seen_at stays at the prior value; results sorted by saved_at desc",
  "mode": "keep-all",
  "now": "2026-05-14T12:00:00Z",
  "prior_inventory": {
    "fetched_at": "2026-05-01T00:00:00Z",
    "saves": [
      {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": [], "last_seen_at": "2026-05-01T00:00:00Z"},
      {"uri": "at://x/2", "saved_at": "2026-04-11T00:00:00Z", "post_text": "post B", "embed": null, "author": {"handle": "b", "display_name": "B", "did": "did:plc:b"}, "images": [], "last_seen_at": "2026-05-01T00:00:00Z"}
    ]
  },
  "fetch_records": [
    {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": []}
  ],
  "expected_output_inventory": {
    "fetched_at": "2026-05-01T00:00:00Z",
    "saves": [
      {"uri": "at://x/2", "saved_at": "2026-04-11T00:00:00Z", "post_text": "post B", "embed": null, "author": {"handle": "b", "display_name": "B", "did": "did:plc:b"}, "images": [], "last_seen_at": "2026-05-01T00:00:00Z", "removed_detected_at": "2026-05-14T12:00:00Z"},
      {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": [], "last_seen_at": "2026-05-14T12:00:00Z"}
    ]
  }
}
```

Create `tests/fixtures/retain/sync-prunes-dead-subject.json`:

```json
{
  "description": "sync keeps a live present bookmark but actively prunes a present bookmark whose subject post is not_found",
  "mode": "sync",
  "now": "2026-05-14T12:00:00Z",
  "prior_inventory": {
    "fetched_at": "2026-05-01T00:00:00Z",
    "saves": [
      {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": [], "last_seen_at": "2026-05-01T00:00:00Z"},
      {"uri": "at://x/2", "saved_at": "2026-04-11T00:00:00Z", "post_text": "post B", "embed": null, "author": {"handle": "b", "display_name": "B", "did": "did:plc:b"}, "images": [], "last_seen_at": "2026-05-01T00:00:00Z"}
    ]
  },
  "fetch_records": [
    {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": []},
    {"uri": "at://x/2", "saved_at": "2026-04-11T00:00:00Z", "post_text": "", "embed": null, "author": {"handle": "", "display_name": "", "did": ""}, "images": [], "subject_status": "not_found"}
  ],
  "expected_output_inventory": {
    "fetched_at": "2026-05-01T00:00:00Z",
    "saves": [
      {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": [], "last_seen_at": "2026-05-14T12:00:00Z"}
    ]
  }
}
```

Create `tests/fixtures/retain/reappearance-clears-removed-detected-at.json`:

```json
{
  "description": "a previously un-saved bookmark that reappears in the fetch has its removed_detected_at cleared and last_seen_at refreshed",
  "mode": "keep-lost",
  "now": "2026-05-14T12:00:00Z",
  "prior_inventory": {
    "fetched_at": "2026-05-01T00:00:00Z",
    "saves": [
      {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": [], "last_seen_at": "2026-05-01T00:00:00Z", "removed_detected_at": "2026-05-10T00:00:00Z"}
    ]
  },
  "fetch_records": [
    {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": []}
  ],
  "expected_output_inventory": {
    "fetched_at": "2026-05-01T00:00:00Z",
    "saves": [
      {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": [], "last_seen_at": "2026-05-14T12:00:00Z"}
    ]
  }
}
```

Create `tests/fixtures/retain/unknown-is-noop-on-existing.json`:

```json
{
  "description": "an unknown subject_status from a listRecords-fallback fetch never overwrites a known not_found status or its timestamp",
  "mode": "keep-lost",
  "now": "2026-05-14T12:00:00Z",
  "prior_inventory": {
    "fetched_at": "2026-05-01T00:00:00Z",
    "saves": [
      {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": [], "last_seen_at": "2026-05-01T00:00:00Z", "subject_status": "not_found", "subject_status_detected_at": "2026-05-05T00:00:00Z"}
    ]
  },
  "fetch_records": [
    {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "", "embed": null, "author": {"handle": "", "display_name": "", "did": ""}, "images": [], "subject_status": "unknown"}
  ],
  "expected_output_inventory": {
    "fetched_at": "2026-05-01T00:00:00Z",
    "saves": [
      {"uri": "at://x/1", "saved_at": "2026-04-10T00:00:00Z", "post_text": "post A", "embed": null, "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"}, "images": [], "last_seen_at": "2026-05-14T12:00:00Z", "subject_status": "not_found", "subject_status_detected_at": "2026-05-05T00:00:00Z"}
    ]
  }
}
```

- [ ] **Step 2: Write the failing runner test**

Create `tests/test_retain_fixtures.py`:

```python
"""Runs the shared golden fixtures (tests/fixtures/retain/*.json) through
merge_into_inventory. These same JSON files are consumed by the
bsky-saves-gui test suite as the cross-implementation anti-drift contract.
See the v0.6.0 spec section 10.4."""
from __future__ import annotations

import json
import pathlib

import pytest

from bsky_saves.normalize import merge_into_inventory

_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "retain"
_FIXTURES = sorted(_FIXTURE_DIR.glob("*.json"))


def test_fixture_directory_is_populated():
    assert _FIXTURES, "no golden fixtures found under tests/fixtures/retain/"


@pytest.mark.parametrize("fixture_path", _FIXTURES, ids=lambda p: p.stem)
def test_retain_golden_fixture(fixture_path):
    case = json.loads(fixture_path.read_text(encoding="utf-8"))
    result = merge_into_inventory(
        case["prior_inventory"],
        case["fetch_records"],
        mode=case["mode"],
        now=case["now"],
    )
    assert result == case["expected_output_inventory"], case["description"]
```

- [ ] **Step 3: Run the runner test to verify it passes**

Run: `python -m pytest tests/test_retain_fixtures.py -v`
Expected: PASSED — `test_fixture_directory_is_populated` plus five parametrized `test_retain_golden_fixture[...]` cases, one per fixture file. If any fixture fails, the `merge_into_inventory` output diverged from the hand-traced expectation — re-check the fixture's `expected_output_inventory` against the algorithm in spec §6.2 before changing code.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/retain/ tests/test_retain_fixtures.py
git commit -m "test(retain): shared golden fixtures for the merge reconcile"
```

---

## Task 9: Verify `/fetch` propagates `subject_status` end-to-end

**Files:**
- Test: `tests/test_serve.py`

Spec §11.2 calls for a "wiring check" that a `/fetch` round-trip carries `subject_status` through. A literal `smoke.yml` curl is not feasible — a real `/fetch` needs Bluesky credentials, which the smoke job does not have — so the feasible, honest form of the check is a `respx`-mocked integration test in `test_serve.py`. `serve.py` itself is **not** modified; this test confirms that because `/fetch` runs `normalise_record` (Task 1), the new field reaches the response. `smoke.yml` is intentionally left unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_serve.py` (in the `# --- /fetch endpoint ---` region, after `test_fetch_response_shape_matches_normalise_record`):

```python
@respx.mock
def test_fetch_propagates_subject_status_for_dead_post():
    """A bookmark whose item is a notFoundPost comes back with
    subject_status == 'not_found' in the /fetch response."""
    _mock_fetch_create_session()
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200,
            json={
                "bookmarks": [
                    {
                        "subject": {"uri": "at://x/p/dead"},
                        "createdAt": "2026-04-12T18:31:00Z",
                        "item": {
                            "$type": "app.bsky.feed.defs#notFoundPost",
                            "uri": "at://x/p/dead",
                            "notFound": True,
                        },
                    }
                ]
            },
        )
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"}},
        )
    assert status == 200
    entry = json.loads(body)["saves"][0]
    assert entry["subject_status"] == "not_found"
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `python -m pytest tests/test_serve.py::test_fetch_propagates_subject_status_for_dead_post -v`
Expected: PASSED — the wiring already works once Task 1 is in place (`/fetch` runs `normalise_record`, which now emits `subject_status`). This is a regression guard, so it passes immediately; if it *fails*, the `/fetch` handler is not routing through `normalise_record` as the serve docs claim, which would need investigation before proceeding.

- [ ] **Step 3: Commit**

```bash
git add tests/test_serve.py
git commit -m "test(serve): /fetch propagates subject_status end-to-end"
```

---

## Task 10: README + version bump

**Files:**
- Modify: `README.md` (the `## Use` section and the `## Inventory schema` block)
- Modify: `pyproject.toml:7`

Document the new surface and bump the version. Per spec §11, the version is a feature bump (`0.5.1` → `0.6.0`), and the release notes must call out the §6.4 default-mode behaviour change.

- [ ] **Step 1: Document the retention modes in the `## Use` section**

In `README.md`, the `## Use` section has a fenced block of `bsky-saves` example commands. Replace the first command block entry — currently:

```
# Pull all bookmarks → ./saves_inventory.json
bsky-saves fetch --inventory ./saves_inventory.json
```

with:

```
# Pull all bookmarks → ./saves_inventory.json
bsky-saves fetch --inventory ./saves_inventory.json

# Retention mode controls what happens to bookmarks no longer on the server.
#   keep-lost (default) — keep posts removed outside your control (deleted /
#                         blocked), drop bookmarks you deliberately un-saved.
#   --sync     (= --mode sync)     — mirror only what is live on the server.
#   --keep-all (= --mode keep-all) — keep everything, including your un-saves.
bsky-saves fetch --inventory ./saves_inventory.json --keep-all
```

Then, immediately after the `All commands are **idempotent**...` paragraph that follows the command block, add this paragraph:

```markdown
**Behaviour change in v0.6.0:** the default retention mode is `keep-lost`.
Before v0.6.0 the CLI was purely additive — it never removed an inventory
entry. From v0.6.0, the first `fetch` after upgrading will drop entries that
are no longer on the server *and* that you had un-saved. Run with
`--keep-all` to preserve the old additive-everything behaviour.
```

- [ ] **Step 2: Document the four new fields in the `## Inventory schema` block**

In `README.md`, the `## Inventory schema` section has a `jsonc` fenced block showing a `saves[]` entry. Inside that entry object, immediately after the `"images": [...]` line and before the `"quoted_post": ...` line, add:

```jsonc
      // Lifecycle flags (added by `fetch`; see retention modes above):
      "last_seen_at": "2026-04-30T14:00:00Z",          // last fetch that saw this URI
      "removed_detected_at": "2026-05-02T09:00:00Z",   // optional; you un-saved it
      "subject_status": "not_found",                   // optional; "not_found" | "blocked" | "unknown"
      "subject_status_detected_at": "2026-05-02T09:00:00Z", // optional; when subject_status went non-live
```

- [ ] **Step 3: Bump the version**

In `pyproject.toml`, change line 7 from:

```toml
version = "0.5.1"
```

to:

```toml
version = "0.6.0"
```

- [ ] **Step 4: Verify the build still works and run the full test suite**

Run: `python -m pytest -q`
Expected: the entire suite PASSES.

Run: `python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"`
Expected output: `0.6.0`

- [ ] **Step 5: Commit**

```bash
git add README.md pyproject.toml
git commit -m "docs: document v0.6.0 retention modes and lifecycle flags; bump version"
```

---

## Final verification

- [ ] **Run the complete test suite**

Run: `python -m pytest -q`
Expected: all tests pass, including the five `test_retain_fixtures.py` golden-fixture cases, the new `normalize` / `fetch` / `serve` tests, and every pre-existing test.

- [ ] **Confirm the CLI surface**

Run: `python -m bsky_saves.cli fetch --help`
Expected: the help text shows `--mode {sync,keep-lost,keep-all}`, `--sync`, and `--keep-all`, and they are presented as mutually exclusive.

- [ ] **Confirm `serve.py` was not modified**

Run: `git diff --stat main -- src/bsky_saves/serve.py`
Expected: no output (zero changes to `serve.py`).
