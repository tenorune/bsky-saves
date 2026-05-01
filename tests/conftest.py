"""Shared pytest fixtures for the bsky-saves test suite."""
from __future__ import annotations

import pytest


def make_image(url: str, alt: str = "") -> dict:
    """Build an entry-images list element matching the normalize.py schema."""
    return {"kind": "image", "url": url, "thumb": url, "alt": alt}


def make_entry(
    uri: str,
    *,
    images: list[dict] | None = None,
    quoted_images: list[dict] | None = None,
    thread_reply_images: list[list[dict]] | None = None,
    quoted_thread_reply_images: list[list[dict]] | None = None,
) -> dict:
    """Build an inventory entry with image URLs in any combination of locations.

    - images: post-attached images
    - quoted_images: images on a quoted post
    - thread_reply_images: list of image-lists, one per same-author thread reply
    - quoted_thread_reply_images: list of image-lists, one per quoted-post thread reply
    """
    entry: dict = {
        "uri": uri,
        "saved_at": "2026-04-12T18:31:00Z",
        "post_text": "test post",
        "embed": None,
        "author": {"handle": "x.bsky.social", "display_name": "X", "did": "did:plc:x"},
        "images": images or [],
    }
    if quoted_images is not None or quoted_thread_reply_images is not None:
        entry["quoted_post"] = {
            "uri": "at://did:plc:y/app.bsky.feed.post/q1",
            "cid": "bafy",
            "author": {"handle": "y.bsky.social", "display_name": "Y", "did": "did:plc:y"},
            "text": "quoted",
            "created_at": "2026-04-10T00:00:00Z",
            "images": quoted_images or [],
        }
        if quoted_thread_reply_images is not None:
            entry["quoted_post"]["thread_replies"] = [
                {"uri": f"at://did:plc:y/app.bsky.feed.post/qt{i}",
                 "indexedAt": "2026-04-10T00:00:00Z",
                 "text": f"qt{i}",
                 "images": imgs}
                for i, imgs in enumerate(quoted_thread_reply_images)
            ]
    if thread_reply_images is not None:
        entry["thread_replies"] = [
            {"uri": f"at://did:plc:x/app.bsky.feed.post/t{i}",
             "indexedAt": "2026-04-12T18:35:00Z",
             "text": f"t{i}",
             "images": imgs}
            for i, imgs in enumerate(thread_reply_images)
        ]
    return entry


def make_inventory(*entries: dict) -> dict:
    return {"fetched_at": "2026-04-27T10:14:00Z", "saves": list(entries)}


@pytest.fixture
def fixture_factory():
    """Expose builder helpers as a single fixture object for tests."""
    class _F:
        image = staticmethod(make_image)
        entry = staticmethod(make_entry)
        inventory = staticmethod(make_inventory)
    return _F
