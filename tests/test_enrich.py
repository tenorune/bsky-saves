"""Tests for bsky_saves.enrich."""
from __future__ import annotations

import json

from bsky_saves import enrich as _enrich_mod
from bsky_saves.enrich import enrich_inventory
from bsky_saves.tid import decode_tid_to_iso, rkey_of


# A real, decodable TID rkey (3jzfcijpj2z2a is from atproto docs examples).
SAMPLE_URI = "at://did:plc:abc/app.bsky.feed.post/3jzfcijpj2z2a"
SAMPLE_POST_CREATED_AT = decode_tid_to_iso(rkey_of(SAMPLE_URI))


def _make_inv(*entries: dict) -> dict:
    return {"fetched_at": "2026-04-27T10:14:00Z", "saves": list(entries)}


def _entry(uri: str, **extra) -> dict:
    e = {
        "uri": uri,
        "saved_at": "2026-04-12T18:31:00Z",
        "post_text": "x",
        "embed": None,
        "author": {"handle": "x", "display_name": "X", "did": "did:plc:x"},
        "images": [],
    }
    e.update(extra)
    return e


def test_enrich_writes_when_post_created_at_missing(tmp_path):
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(_make_inv(_entry(SAMPLE_URI))), encoding="utf-8")

    pre = inv_path.read_text(encoding="utf-8")
    enrich_inventory(inv_path)
    post = inv_path.read_text(encoding="utf-8")

    assert pre != post, "enrich must rewrite when adding post_created_at"
    data = json.loads(post)
    assert data["saves"][0]["post_created_at"] == SAMPLE_POST_CREATED_AT


def test_enrich_no_write_when_already_enriched_and_clean(tmp_path):
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(
        json.dumps(_make_inv(_entry(SAMPLE_URI, post_created_at=SAMPLE_POST_CREATED_AT))),
        encoding="utf-8",
    )

    pre = inv_path.read_text(encoding="utf-8")
    enrich_inventory(inv_path)
    post = inv_path.read_text(encoding="utf-8")

    assert pre == post, (
        "enrich must not rewrite when every entry is already enriched and clean"
    )


def test_enrich_writes_when_pub_dropped(tmp_path):
    """Bogus article_published_at (after post_created_at) must be removed."""
    inv_path = tmp_path / "inv.json"
    bogus_pub = "2099-01-01"  # well after post date
    entry = _entry(
        SAMPLE_URI,
        post_created_at=SAMPLE_POST_CREATED_AT,
        article_published_at=bogus_pub,
        article_fetched_at="2026-04-29T00:00:00Z",
    )
    inv_path.write_text(json.dumps(_make_inv(entry)), encoding="utf-8")

    pre = inv_path.read_text(encoding="utf-8")
    stats = enrich_inventory(inv_path)
    post = inv_path.read_text(encoding="utf-8")

    assert pre != post
    assert stats["pub_dropped"] == 1
    assert "article_published_at" not in json.loads(post)["saves"][0]


def test_enrich_no_write_with_refresh_when_recompute_matches(tmp_path):
    """refresh=True on already-correct entries must not rewrite the file."""
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(
        json.dumps(_make_inv(_entry(SAMPLE_URI, post_created_at=SAMPLE_POST_CREATED_AT))),
        encoding="utf-8",
    )

    pre = inv_path.read_text(encoding="utf-8")
    enrich_inventory(inv_path, refresh=True)
    post = inv_path.read_text(encoding="utf-8")

    assert pre == post, (
        "refresh must not rewrite when the recomputed value matches the existing one"
    )
