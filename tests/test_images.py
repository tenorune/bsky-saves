"""Tests for bsky_saves.images."""
from __future__ import annotations

from bsky_saves.images import _iter_image_urls


def test_iter_image_urls_post_images_only(fixture_factory):
    f = fixture_factory
    entry = f.entry(
        "at://x/p/1",
        images=[f.image("https://cdn.bsky.app/a.jpg"),
                f.image("https://cdn.bsky.app/b.jpg")],
    )
    assert list(_iter_image_urls(entry)) == [
        "https://cdn.bsky.app/a.jpg",
        "https://cdn.bsky.app/b.jpg",
    ]


def test_iter_image_urls_quoted_post_images(fixture_factory):
    f = fixture_factory
    entry = f.entry(
        "at://x/p/1",
        quoted_images=[f.image("https://cdn.bsky.app/q.jpg")],
    )
    assert list(_iter_image_urls(entry)) == ["https://cdn.bsky.app/q.jpg"]


def test_iter_image_urls_thread_reply_images(fixture_factory):
    f = fixture_factory
    entry = f.entry(
        "at://x/p/1",
        thread_reply_images=[
            [f.image("https://cdn.bsky.app/t1.jpg")],
            [f.image("https://cdn.bsky.app/t2a.jpg"), f.image("https://cdn.bsky.app/t2b.jpg")],
        ],
    )
    assert list(_iter_image_urls(entry)) == [
        "https://cdn.bsky.app/t1.jpg",
        "https://cdn.bsky.app/t2a.jpg",
        "https://cdn.bsky.app/t2b.jpg",
    ]


def test_iter_image_urls_quoted_post_thread_reply_images(fixture_factory):
    f = fixture_factory
    entry = f.entry(
        "at://x/p/1",
        quoted_images=[],
        quoted_thread_reply_images=[[f.image("https://cdn.bsky.app/qt.jpg")]],
    )
    assert list(_iter_image_urls(entry)) == ["https://cdn.bsky.app/qt.jpg"]


def test_iter_image_urls_all_four_locations(fixture_factory):
    f = fixture_factory
    entry = f.entry(
        "at://x/p/1",
        images=[f.image("https://cdn.bsky.app/post.jpg")],
        quoted_images=[f.image("https://cdn.bsky.app/quoted.jpg")],
        thread_reply_images=[[f.image("https://cdn.bsky.app/thread.jpg")]],
        quoted_thread_reply_images=[[f.image("https://cdn.bsky.app/qthread.jpg")]],
    )
    assert sorted(_iter_image_urls(entry)) == [
        "https://cdn.bsky.app/post.jpg",
        "https://cdn.bsky.app/qthread.jpg",
        "https://cdn.bsky.app/quoted.jpg",
        "https://cdn.bsky.app/thread.jpg",
    ]


def test_iter_image_urls_no_images(fixture_factory):
    f = fixture_factory
    entry = f.entry("at://x/p/1")
    assert list(_iter_image_urls(entry)) == []


def test_iter_image_urls_skips_empty_url(fixture_factory):
    f = fixture_factory
    entry = f.entry("at://x/p/1", images=[{"kind": "image", "url": "", "alt": ""}])
    assert list(_iter_image_urls(entry)) == []
