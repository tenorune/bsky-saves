"""Tests for normalize.normalise_record / merge_into_inventory."""
from __future__ import annotations

from bsky_saves.normalize import _reconcile_subject_status, merge_into_inventory, normalise_record


# ---------- merge_into_inventory ----------

def _empty_inventory():
    return {"fetched_at": None, "saves": []}


def test_merge_preserves_existing_entries():
    existing = {
        "fetched_at": "2026-04-01T00:00:00Z",
        "saves": [
            {
                "uri": "at://x/1",
                "saved_at": "2026-04-01T12:00:00Z",
                "post_text": "original",
                "embed": None,
                "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"},
            }
        ],
    }
    new_entries = [
        {
            "uri": "at://x/1",
            "saved_at": "2026-04-12T00:00:00Z",
            "post_text": "REPLACED — must not appear",
            "embed": None,
            "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"},
        },
        {
            "uri": "at://x/2",
            "saved_at": "2026-04-12T00:00:00Z",
            "post_text": "new",
            "embed": None,
            "author": {"handle": "b", "display_name": "B", "did": "did:plc:b"},
        },
    ]
    merged = merge_into_inventory(existing, new_entries, now=_NOW)
    by_uri = {s["uri"]: s for s in merged["saves"]}
    assert by_uri["at://x/1"]["post_text"] == "original"
    assert by_uri["at://x/2"]["post_text"] == "new"


def test_merge_backfills_missing_fields():
    existing = {
        "fetched_at": "2026-04-01T00:00:00Z",
        "saves": [
            {
                "uri": "at://x/1",
                "saved_at": "2026-04-01T12:00:00Z",
                "post_text": "original",
                "embed": None,
                "author": {"handle": "a", "display_name": "A", "did": "did:plc:a"},
            }
        ],
    }
    new_entries = [
        {
            "uri": "at://x/1",
            "saved_at": "2026-04-12T00:00:00Z",
            "post_text": "DIFFERENT",
            "embed": None,
            "author": {},
            "images": [{"kind": "image", "url": "https://cdn/x.jpg", "alt": "alt"}],
        },
    ]
    merged = merge_into_inventory(existing, new_entries, now=_NOW)
    e = {s["uri"]: s for s in merged["saves"]}["at://x/1"]
    assert e["post_text"] == "original"
    assert e["images"] == [{"kind": "image", "url": "https://cdn/x.jpg", "alt": "alt"}]


def test_merge_backfills_empty_existing_field():
    existing = {
        "fetched_at": None,
        "saves": [
            {
                "uri": "at://x/1",
                "saved_at": "2026-04-01T12:00:00Z",
                "post_text": "",
                "embed": None,
                "author": {},
            }
        ],
    }
    new_entries = [
        {
            "uri": "at://x/1",
            "saved_at": "2026-04-12T00:00:00Z",
            "post_text": "new text",
            "embed": {"type": "external", "url": "https://e/", "title": "t", "description": "d"},
            "author": {},
        },
    ]
    merged = merge_into_inventory(existing, new_entries, now=_NOW)
    e = {s["uri"]: s for s in merged["saves"]}["at://x/1"]
    assert e["post_text"] == "new text"
    assert e["embed"]["url"] == "https://e/"


def test_merge_sorts_by_saved_at_desc():
    existing = _empty_inventory()
    new_entries = [
        {"uri": "at://x/A", "saved_at": "2026-04-10T00:00:00Z", "post_text": "", "embed": None, "author": {}},
        {"uri": "at://x/B", "saved_at": "2026-04-12T00:00:00Z", "post_text": "", "embed": None, "author": {}},
        {"uri": "at://x/C", "saved_at": "2026-04-11T00:00:00Z", "post_text": "", "embed": None, "author": {}},
    ]
    merged = merge_into_inventory(existing, new_entries, now=_NOW)
    saved_ats = [s["saved_at"] for s in merged["saves"]]
    assert saved_ats == sorted(saved_ats, reverse=True)


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


# ---------- normalise_record ----------

def test_extract_embed_external_pulls_url_title_description():
    raw = {
        "uri": "at://x/1",
        "indexedAt": "2026-04-12T00:00:00Z",
        "value": {
            "createdAt": "2026-04-12T00:00:00Z",
            "subject": {
                "uri": "at://author/post1",
                "value": {
                    "text": "post text here",
                    "embed": {
                        "$type": "app.bsky.embed.external",
                        "external": {
                            "uri": "https://example.org/article",
                            "title": "Article title",
                            "description": "Article description",
                        },
                    },
                },
                "author": {
                    "handle": "author.bsky.social",
                    "displayName": "Author Name",
                    "did": "did:plc:author",
                },
            },
        },
    }
    entry = normalise_record(raw)
    assert entry["uri"] == "at://author/post1"
    assert entry["embed"]["url"] == "https://example.org/article"
    assert entry["embed"]["type"] == "external"


def test_normalise_record_extracts_images_from_hydrated_view():
    raw = {
        "createdAt": "2026-04-22T19:37:34Z",
        "subject": {"uri": "at://author/post1"},
        "item": {
            "uri": "at://author/post1",
            "indexedAt": "2026-04-22T17:27:55Z",
            "author": {"handle": "h", "displayName": "H", "did": "did:plc:h"},
            "record": {"$type": "app.bsky.feed.post", "text": "post"},
            "embed": {
                "$type": "app.bsky.embed.images#view",
                "images": [
                    {
                        "thumb": "https://cdn.bsky.app/img/feed_thumbnail/.../1@jpeg",
                        "fullsize": "https://cdn.bsky.app/img/feed_fullsize/.../1@jpeg",
                        "alt": "first image",
                    },
                ],
            },
        },
    }
    entry = normalise_record(raw)
    assert len(entry["images"]) == 1
    assert entry["images"][0]["alt"] == "first image"


def test_normalise_record_handles_record_with_media_view():
    raw = {
        "createdAt": "2026-04-22T19:37:34Z",
        "subject": {"uri": "at://author/post1"},
        "item": {
            "uri": "at://author/post1",
            "indexedAt": "2026-04-22T17:27:55Z",
            "author": {"handle": "h", "displayName": "H", "did": "did:plc:h"},
            "record": {"$type": "app.bsky.feed.post", "text": "post"},
            "embed": {
                "$type": "app.bsky.embed.recordWithMedia#view",
                "media": {
                    "$type": "app.bsky.embed.images#view",
                    "images": [
                        {"thumb": "https://cdn/t.jpg", "fullsize": "https://cdn/f.jpg", "alt": "a"}
                    ],
                },
            },
        },
    }
    entry = normalise_record(raw)
    assert entry["images"][0]["url"] == "https://cdn/f.jpg"


def test_extract_handles_missing_embed():
    raw = {
        "uri": "at://x/2",
        "indexedAt": "2026-04-12T00:00:00Z",
        "value": {
            "createdAt": "2026-04-12T00:00:00Z",
            "subject": {
                "uri": "at://author/post2",
                "value": {"text": "no embed"},
                "author": {"handle": "h", "displayName": "H", "did": "did:plc:h"},
            },
        },
    }
    entry = normalise_record(raw)
    assert entry["embed"] is None


def test_normalise_record_hydrated_getbookmarks_shape():
    raw = {
        "createdAt": "2026-04-22T19:37:34.460Z",
        "subject": {
            "uri": "at://did:plc:author/app.bsky.feed.post/abc",
        },
        "item": {
            "uri": "at://did:plc:author/app.bsky.feed.post/abc",
            "author": {
                "did": "did:plc:author",
                "handle": "author.bsky.social",
                "displayName": "Author Name",
            },
            "record": {
                "$type": "app.bsky.feed.post",
                "createdAt": "2026-04-22T17:27:55.496Z",
                "text": "post body text here",
            },
            "indexedAt": "2026-04-22T17:27:55.752Z",
        },
    }
    entry = normalise_record(raw)
    assert entry["uri"] == "at://did:plc:author/app.bsky.feed.post/abc"
    assert entry["saved_at"] == "2026-04-22T19:37:34.460Z"
    assert entry["post_text"] == "post body text here"


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
    assert entry["author"]["did"] == "did:plc:author"
    assert entry["author"]["handle"] == ""


def test_normalise_record_unknown_item_type_treated_as_live():
    """An unrecognised item $type (e.g. a future union member) is treated as
    live: no subject_status is emitted."""
    raw = {
        "createdAt": "2026-04-22T19:37:34Z",
        "subject": {"uri": "at://author/post1"},
        "item": {
            "$type": "app.bsky.feed.defs#someFutureType",
            "uri": "at://author/post1",
        },
    }
    entry = normalise_record(raw)
    assert "subject_status" not in entry


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


# ---------- _reconcile_subject_status ----------

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


def test_merge_prior_dead_entry_kept_under_keep_lost_pruned_under_sync():
    """A prior-inventory entry already flagged not_found, re-fetched as
    not_found: keep-lost retains it, sync prunes it."""
    existing = _inv_with(
        _entry("at://x/1", "2026-04-10T00:00:00Z"),
        _entry("at://x/2", "2026-04-11T00:00:00Z",
               subject_status="not_found",
               subject_status_detected_at="2026-05-05T00:00:00Z"),
    )
    new_entries = [
        _entry("at://x/1", "2026-04-10T00:00:00Z"),
        _entry("at://x/2", "2026-04-11T00:00:00Z", subject_status="not_found"),
    ]
    kept = merge_into_inventory(existing, new_entries, mode="keep-lost", now=_NOW)
    assert {s["uri"] for s in kept["saves"]} == {"at://x/1", "at://x/2"}
    pruned = merge_into_inventory(existing, new_entries, mode="sync", now=_NOW)
    assert {s["uri"] for s in pruned["saves"]} == {"at://x/1"}


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
