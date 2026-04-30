"""Tests for normalize.normalise_record / merge_into_inventory."""
from __future__ import annotations

import json

from bsky_saves.normalize import merge_into_inventory, normalise_record


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
    merged = merge_into_inventory(existing, new_entries)
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
    merged = merge_into_inventory(existing, new_entries)
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
    merged = merge_into_inventory(existing, new_entries)
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
    merged = merge_into_inventory(existing, new_entries)
    saved_ats = [s["saved_at"] for s in merged["saves"]]
    assert saved_ats == sorted(saved_ats, reverse=True)


def test_merge_idempotent_when_no_new_saves():
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
    new_entries = [seed["saves"][0].copy()]
    merged = merge_into_inventory(seed, new_entries)
    assert sorted(json.dumps(s, sort_keys=True) for s in merged["saves"]) == sorted(
        json.dumps(s, sort_keys=True) for s in seed["saves"]
    )


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
