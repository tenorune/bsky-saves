"""Tests for bsky_saves.images."""
from __future__ import annotations

import json
import os

import pytest
import respx

from bsky_saves.cli import _load_uris
from bsky_saves.images import _iter_image_urls, hydrate_images, filename_for_url


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


def test_load_uris_none_returns_none():
    assert _load_uris(None) is None


def test_load_uris_simple_list(tmp_path):
    p = tmp_path / "uris.txt"
    p.write_text("at://x/p/1\nat://x/p/2\n", encoding="utf-8")
    assert _load_uris(p) == {"at://x/p/1", "at://x/p/2"}


def test_load_uris_strips_comments_and_blanks(tmp_path):
    p = tmp_path / "uris.txt"
    p.write_text(
        "# this is a comment\n"
        "\n"
        "at://x/p/1\n"
        "  \n"
        "# another comment\n"
        "at://x/p/2  \n",
        encoding="utf-8",
    )
    assert _load_uris(p) == {"at://x/p/1", "at://x/p/2"}


def test_load_uris_dedupes(tmp_path):
    p = tmp_path / "uris.txt"
    p.write_text("at://x/p/1\nat://x/p/1\nat://x/p/2\n", encoding="utf-8")
    assert _load_uris(p) == {"at://x/p/1", "at://x/p/2"}


def test_load_uris_empty_file(tmp_path):
    p = tmp_path / "uris.txt"
    p.write_text("", encoding="utf-8")
    assert _load_uris(p) == set()


def test_load_uris_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        _load_uris(tmp_path / "does-not-exist.txt")


@respx.mock
def test_hydrate_images_downloads_all_entries(fixture_factory, tmp_path):
    f = fixture_factory
    inv = f.inventory(
        f.entry("at://x/p/1", images=[f.image("https://cdn.bsky.app/a.jpg")]),
        f.entry("at://x/p/2", images=[f.image("https://cdn.bsky.app/b.jpg")]),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")
    out_dir = tmp_path / "imgs"

    respx.get("https://cdn.bsky.app/a.jpg").respond(200, content=b"AAA")
    respx.get("https://cdn.bsky.app/b.jpg").respond(200, content=b"BBB")

    result = hydrate_images(inv_path, out_dir)
    entries_processed, downloaded, skipped, failed = result
    assert (entries_processed, downloaded, skipped, failed) == (2, 2, 0, 0)

    fname_a = filename_for_url("https://cdn.bsky.app/a.jpg")
    fname_b = filename_for_url("https://cdn.bsky.app/b.jpg")
    assert (out_dir / fname_a).read_bytes() == b"AAA"
    assert (out_dir / fname_b).read_bytes() == b"BBB"

    written = json.loads(inv_path.read_text(encoding="utf-8"))
    saves_by_uri = {s["uri"]: s for s in written["saves"]}
    assert saves_by_uri["at://x/p/1"]["local_images"] == [
        {"url": "https://cdn.bsky.app/a.jpg", "path": fname_a},
    ]
    assert saves_by_uri["at://x/p/2"]["local_images"] == [
        {"url": "https://cdn.bsky.app/b.jpg", "path": fname_b},
    ]


@respx.mock
def test_hydrate_images_no_images_no_local_images_field(fixture_factory, tmp_path):
    f = fixture_factory
    inv = f.inventory(f.entry("at://x/p/1"))  # no images
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")
    out_dir = tmp_path / "imgs"

    result = hydrate_images(inv_path, out_dir)
    assert result == (0, 0, 0, 0)

    written = json.loads(inv_path.read_text(encoding="utf-8"))
    assert "local_images" not in written["saves"][0]


@respx.mock
def test_hydrate_images_dedupes_urls_across_locations(fixture_factory, tmp_path):
    """Same URL appearing in post + thread reply downloads once, recorded once."""
    f = fixture_factory
    same_url = "https://cdn.bsky.app/dup.jpg"
    inv = f.inventory(
        f.entry(
            "at://x/p/1",
            images=[f.image(same_url)],
            thread_reply_images=[[f.image(same_url)]],
        )
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")
    out_dir = tmp_path / "imgs"

    route = respx.get(same_url).respond(200, content=b"X")

    result = hydrate_images(inv_path, out_dir)
    entries_processed, downloaded, skipped, failed = result
    assert (entries_processed, downloaded, skipped, failed) == (1, 1, 0, 0)
    assert route.call_count == 1

    written = json.loads(inv_path.read_text(encoding="utf-8"))
    fname = filename_for_url(same_url)
    assert written["saves"][0]["local_images"] == [{"url": same_url, "path": fname}]


@respx.mock
def test_hydrate_images_preserves_existing_fields(fixture_factory, tmp_path):
    """Other entry fields (article_text, thread_replies, etc.) must be preserved."""
    f = fixture_factory
    entry = f.entry("at://x/p/1", images=[f.image("https://cdn.bsky.app/a.jpg")])
    entry["article_text"] = "The full article body."
    entry["post_created_at"] = "2026-04-10T15:22:08Z"
    inv = f.inventory(entry)
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    respx.get("https://cdn.bsky.app/a.jpg").respond(200, content=b"A")
    hydrate_images(inv_path, tmp_path / "imgs")

    written = json.loads(inv_path.read_text(encoding="utf-8"))
    s = written["saves"][0]
    assert s["article_text"] == "The full article body."
    assert s["post_created_at"] == "2026-04-10T15:22:08Z"
    assert "local_images" in s


@respx.mock
def test_hydrate_images_uris_filter_processes_only_listed(fixture_factory, tmp_path):
    f = fixture_factory
    inv = f.inventory(
        f.entry("at://x/p/1", images=[f.image("https://cdn.bsky.app/a.jpg")]),
        f.entry("at://x/p/2", images=[f.image("https://cdn.bsky.app/b.jpg")]),
        f.entry("at://x/p/3", images=[f.image("https://cdn.bsky.app/c.jpg")]),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")
    out_dir = tmp_path / "imgs"

    route_a = respx.get("https://cdn.bsky.app/a.jpg").respond(200, content=b"A")
    route_b = respx.get("https://cdn.bsky.app/b.jpg").respond(200, content=b"B")
    route_c = respx.get("https://cdn.bsky.app/c.jpg").respond(200, content=b"C")

    result = hydrate_images(inv_path, out_dir, uris={"at://x/p/1", "at://x/p/3"})
    entries_processed, downloaded, _, _ = result
    assert (entries_processed, downloaded) == (2, 2)
    assert route_a.called
    assert not route_b.called
    assert route_c.called

    written = json.loads(inv_path.read_text(encoding="utf-8"))
    by_uri = {s["uri"]: s for s in written["saves"]}
    assert "local_images" in by_uri["at://x/p/1"]
    assert "local_images" not in by_uri["at://x/p/2"]
    assert "local_images" in by_uri["at://x/p/3"]


@respx.mock
def test_hydrate_images_uris_unknown_uri_silently_skipped(fixture_factory, tmp_path):
    f = fixture_factory
    inv = f.inventory(
        f.entry("at://x/p/1", images=[f.image("https://cdn.bsky.app/a.jpg")]),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    respx.get("https://cdn.bsky.app/a.jpg").respond(200, content=b"A")

    result = hydrate_images(
        inv_path,
        tmp_path / "imgs",
        uris={"at://x/p/1", "at://x/never-existed"},
    )
    entries_processed, downloaded, _, failed = result
    assert (entries_processed, downloaded, failed) == (1, 1, 0)


@respx.mock
def test_hydrate_images_empty_uris_processes_nothing(fixture_factory, tmp_path):
    f = fixture_factory
    inv = f.inventory(
        f.entry("at://x/p/1", images=[f.image("https://cdn.bsky.app/a.jpg")]),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    route = respx.get("https://cdn.bsky.app/a.jpg").respond(200, content=b"A")

    result = hydrate_images(inv_path, tmp_path / "imgs", uris=set())
    entries_processed, downloaded, _, _ = result
    assert (entries_processed, downloaded) == (0, 0)
    assert not route.called


@respx.mock
def test_hydrate_images_skips_existing_file(fixture_factory, tmp_path):
    """If <out>/<filename> already exists, no download; mapping still recorded."""
    f = fixture_factory
    url = "https://cdn.bsky.app/a.jpg"
    inv = f.inventory(f.entry("at://x/p/1", images=[f.image(url)]))
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")
    out_dir = tmp_path / "imgs"
    out_dir.mkdir()
    fname = filename_for_url(url)
    (out_dir / fname).write_bytes(b"PRE-EXISTING")

    route = respx.get(url).respond(200, content=b"NEW")

    result = hydrate_images(inv_path, out_dir)
    entries_processed, downloaded, skipped, failed = result
    assert (entries_processed, downloaded, skipped, failed) == (1, 0, 1, 0)
    assert not route.called
    assert (out_dir / fname).read_bytes() == b"PRE-EXISTING"

    written = json.loads(inv_path.read_text(encoding="utf-8"))
    assert written["saves"][0]["local_images"] == [{"url": url, "path": fname}]


@respx.mock
def test_hydrate_images_idempotent_across_runs(fixture_factory, tmp_path):
    """Run twice: second run is a no-op for downloads; local_images stays the same."""
    f = fixture_factory
    inv = f.inventory(
        f.entry("at://x/p/1", images=[f.image("https://cdn.bsky.app/a.jpg")]),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")
    out_dir = tmp_path / "imgs"

    respx.get("https://cdn.bsky.app/a.jpg").respond(200, content=b"A")

    first = hydrate_images(inv_path, out_dir)
    second = hydrate_images(inv_path, out_dir)

    assert first == (1, 1, 0, 0)
    assert second == (1, 0, 1, 0)

    written = json.loads(inv_path.read_text(encoding="utf-8"))
    fname = filename_for_url("https://cdn.bsky.app/a.jpg")
    assert written["saves"][0]["local_images"] == [
        {"url": "https://cdn.bsky.app/a.jpg", "path": fname},
    ]


@respx.mock
def test_hydrate_images_per_image_failure_nonfatal(fixture_factory, tmp_path):
    f = fixture_factory
    inv = f.inventory(
        f.entry(
            "at://x/p/1",
            images=[
                f.image("https://cdn.bsky.app/ok.jpg"),
                f.image("https://cdn.bsky.app/bad.jpg"),
                f.image("https://cdn.bsky.app/also-ok.jpg"),
            ],
        )
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")
    out_dir = tmp_path / "imgs"

    respx.get("https://cdn.bsky.app/ok.jpg").respond(200, content=b"O")
    respx.get("https://cdn.bsky.app/bad.jpg").respond(500)
    respx.get("https://cdn.bsky.app/also-ok.jpg").respond(200, content=b"A")

    result = hydrate_images(inv_path, out_dir)
    entries_processed, downloaded, skipped, failed = result
    assert (entries_processed, downloaded, skipped, failed) == (1, 2, 0, 1)

    written = json.loads(inv_path.read_text(encoding="utf-8"))
    paths = [li["url"] for li in written["saves"][0]["local_images"]]
    assert paths == [
        "https://cdn.bsky.app/ok.jpg",
        "https://cdn.bsky.app/also-ok.jpg",
    ]


@respx.mock
def test_hydrate_images_failure_in_one_entry_does_not_block_others(fixture_factory, tmp_path):
    f = fixture_factory
    inv = f.inventory(
        f.entry("at://x/p/1", images=[f.image("https://cdn.bsky.app/bad.jpg")]),
        f.entry("at://x/p/2", images=[f.image("https://cdn.bsky.app/ok.jpg")]),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    respx.get("https://cdn.bsky.app/bad.jpg").respond(500)
    respx.get("https://cdn.bsky.app/ok.jpg").respond(200, content=b"OK")

    result = hydrate_images(inv_path, tmp_path / "imgs")
    entries_processed, downloaded, _, failed = result
    assert (entries_processed, downloaded, failed) == (2, 1, 1)

    written = json.loads(inv_path.read_text(encoding="utf-8"))
    by_uri = {s["uri"]: s for s in written["saves"]}
    assert "local_images" not in by_uri["at://x/p/1"]
    assert "local_images" in by_uri["at://x/p/2"]


@respx.mock
def test_hydrate_images_atomic_write_via_tmp_file(
    fixture_factory, tmp_path, monkeypatch
):
    """Patch os.rename to capture that the write went through a temp file."""
    f = fixture_factory
    inv = f.inventory(
        f.entry("at://x/p/1", images=[f.image("https://cdn.bsky.app/a.jpg")]),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    respx.get("https://cdn.bsky.app/a.jpg").respond(200, content=b"A")

    rename_calls: list[tuple[str, str]] = []
    real_rename = os.rename

    def spy_rename(src, dst):
        rename_calls.append((str(src), str(dst)))
        real_rename(src, dst)

    monkeypatch.setattr("bsky_saves.images.os.rename", spy_rename)

    hydrate_images(inv_path, tmp_path / "imgs")

    assert len(rename_calls) == 1
    src, dst = rename_calls[0]
    assert dst == str(inv_path)
    assert src.endswith(".tmp")
    written = json.loads(inv_path.read_text(encoding="utf-8"))
    assert "local_images" in written["saves"][0]
