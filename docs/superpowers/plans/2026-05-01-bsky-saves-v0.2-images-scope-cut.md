# bsky-saves v0.2 — Image Scope Cut Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the v0.1 `hydrate images` subcommand (Markdown rewriter for stories-of-47's Jekyll layout) with a v0.2 inventory-driven, format-agnostic image downloader. Clean break: no compatibility shim.

**Architecture:** All work on a `v0.2` branch in `tenorune/bsky-saves`. The `hydrate images` subcommand becomes pure ingestion: read URLs from `inventory.json`, download to a flat output directory, record `url → path` mappings as a `local_images` field on each affected entry. Markdown discovery, frontmatter parsing, slug-based subdirs, and URL-prefix rewriting all delete. Stories-of-47 absorbs the Markdown rewriter on its own side (out of scope for this plan).

**Tech Stack:** Python 3.11+, `httpx` for downloads, `respx` (existing dev dep) for mocking HTTP in tests, `pytest`, `hatchling` build backend.

**Spec:** `docs/superpowers/specs/2026-05-01-bsky-saves-v0.2-images-scope-cut.md`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/bsky_saves/images.py` | Rewrite in place | Image-URL discovery from inventory entries, image download, inventory mutation. Keep `filename_for_url`, `download_to`, `DEFAULT_USER_AGENT`, `TIMEOUT`. Remove `IMG_PATTERN`, `slug_from_frontmatter`, `localize_images`. Add `hydrate_images` (entry point) and a private `_iter_image_urls` helper. |
| `src/bsky_saves/cli.py` | Modify (subparser + dispatch) | Replace `hydrate images` argparse subparser flags. Add `_load_uris` helper. |
| `tests/test_images.py` | Create | All new image-related tests. |
| `tests/conftest.py` | Create | Shared fixture builders for inventory JSON. |
| `pyproject.toml` | Modify (one line) | Bump `version = "0.2.0"`. |
| `README.md` | Modify (relevant section) | Reflect new `hydrate images` CLI shape. No migration banner. |

The existing tests (`test_fetch.py`, `test_normalize.py`, `test_tid.py`) are unchanged. There is no existing `tests/test_images.py` file to replace.

---

## Task 1: Create the v0.2 branch

**Files:** none (git operation only)

- [ ] **Step 1: Create and switch to the v0.2 branch from main**

```bash
cd /home/user/bsky-saves
git checkout main
git pull origin main
git checkout -b v0.2
```

Expected: switched to a new branch 'v0.2', tracking main as the merge base.

- [ ] **Step 2: Push the empty branch to origin to establish tracking**

```bash
git push -u origin v0.2
```

Expected: `branch 'v0.2' set up to track 'origin/v0.2'`.

---

## Task 2: Create shared test fixture builders

**Files:**
- Create: `tests/conftest.py`

These fixtures build minimal, realistic inventory entries used across `test_images.py` tests. Centralizing them avoids per-test inventory boilerplate.

- [ ] **Step 1: Create `tests/conftest.py`**

```python
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
```

- [ ] **Step 2: Verify pytest discovers the conftest**

```bash
cd /home/user/bsky-saves
python -m pip install -e ".[dev]"
pytest --collect-only tests/ 2>&1 | tail -10
```

Expected: existing tests collected without errors. No new tests to collect yet.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: add shared inventory fixture builders"
```

---

## Task 3: TDD — `_iter_image_urls` helper extracts URLs from all four locations

**Files:**
- Modify: `src/bsky_saves/images.py`
- Test: `tests/test_images.py`

The helper walks one inventory entry and yields every image URL it contains. URLs from all four locations (post images, quoted-post images, same-author thread reply images, quoted-post thread reply images). Yields URL strings — deduplication and filename mapping are the caller's job.

- [ ] **Step 1: Write the failing test in a new `tests/test_images.py`**

```python
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
        "https://cdn.bsky.app/quoted.jpg",
        "https://cdn.bsky.app/qthread.jpg",
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/user/bsky-saves
pytest tests/test_images.py -v
```

Expected: ImportError or ModuleNotFoundError on `_iter_image_urls` (function doesn't exist yet).

- [ ] **Step 3: Implement `_iter_image_urls` in `src/bsky_saves/images.py`**

Add this function near the top of the existing `src/bsky_saves/images.py` (don't remove anything yet — that happens in Task 11):

```python
from typing import Iterator


def _iter_image_urls(entry: dict) -> Iterator[str]:
    """Yield every image URL referenced by an inventory entry.

    Walks four locations, in order:
      1. entry["images"]
      2. entry["quoted_post"]["images"]
      3. entry["thread_replies"][i]["images"]
      4. entry["quoted_post"]["thread_replies"][i]["images"]

    Empty / missing URLs are skipped. Order matches discovery order so
    downstream consumers can rely on positional correspondence.
    """
    for img in entry.get("images") or []:
        url = img.get("url")
        if url:
            yield url

    quoted = entry.get("quoted_post") or {}
    for img in quoted.get("images") or []:
        url = img.get("url")
        if url:
            yield url

    for reply in entry.get("thread_replies") or []:
        for img in reply.get("images") or []:
            url = img.get("url")
            if url:
                yield url

    for reply in quoted.get("thread_replies") or []:
        for img in reply.get("images") or []:
            url = img.get("url")
            if url:
                yield url
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_images.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/images.py tests/test_images.py
git commit -m "feat(images): add _iter_image_urls helper for inventory-driven discovery"
```

---

## Task 4: TDD — `_load_uris` helper parses the `--uris` file format

**Files:**
- Modify: `src/bsky_saves/cli.py`
- Test: `tests/test_images.py`

Helper that reads the optional `--uris FILE` and returns a `set[str] | None`. Handles comments (`#` prefix), blank lines, and trailing whitespace. Returns `None` when called with `None` (signals "no filter; process all entries").

- [ ] **Step 1: Add failing tests to `tests/test_images.py`**

Append to the existing `tests/test_images.py`:

```python
import pytest

from bsky_saves.cli import _load_uris


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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_images.py -v
```

Expected: ImportError on `_load_uris`.

- [ ] **Step 3: Implement `_load_uris` in `src/bsky_saves/cli.py`**

Add this function near the top of `src/bsky_saves/cli.py`, after the imports:

```python
def _load_uris(path: Path | None) -> set[str] | None:
    """Load a newline-delimited URI list. Returns None if path is None.

    Strips blank lines and `#`-prefixed comments; trims surrounding whitespace
    on each URI; deduplicates.
    """
    if path is None:
        return None
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.add(s)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_images.py -v
```

Expected: all tests in this file pass (now 13 total).

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/cli.py tests/test_images.py
git commit -m "feat(cli): add _load_uris helper for --uris file parsing"
```

---

## Task 5: TDD — `hydrate_images` happy path: download all, record `local_images`

**Files:**
- Modify: `src/bsky_saves/images.py`
- Test: `tests/test_images.py`

The main entry point. Walks every inventory entry (no filter), downloads every image URL, writes the inventory back with `local_images` arrays added.

This task only covers the happy path. Filtering, idempotency, error handling, and atomic writes come in later tasks.

- [ ] **Step 1: Add failing tests to `tests/test_images.py`**

Append:

```python
import json

import respx
import httpx as _httpx_mod  # noqa: F401  (used implicitly by respx)

from bsky_saves.images import hydrate_images, filename_for_url


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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_images.py -v
```

Expected: ImportError on `hydrate_images`.

- [ ] **Step 3: Implement `hydrate_images` in `src/bsky_saves/images.py`**

Add at the bottom of the file (the existing `localize_images` is removed in Task 11):

```python
import json
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hydrate_images(
    inventory_path: Path,
    out_dir: Path,
    *,
    uris: set[str] | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
) -> tuple[int, int, int, int]:
    """Download CDN images referenced by inventory entries.

    Args:
        inventory_path: Path to a JSON inventory written by ``bsky-saves fetch``.
        out_dir: Directory for downloaded images. Created if absent. Flat layout.
        uris: If provided, only inventory entries whose ``uri`` is in this set are
            processed. URIs in ``uris`` that don't appear in the inventory are
            silently skipped. If ``None``, every inventory entry with images is
            processed.
        user_agent: User-Agent header for outbound HTTP requests.

    Returns:
        ``(entries_processed, downloaded, skipped, failed)``.
        - entries_processed: number of inventory entries actually walked.
        - downloaded: number of images written to disk this run.
        - skipped: number of images that already existed on disk (idempotent).
        - failed: number of images whose download raised.

    The inventory is mutated in place: each processed entry that has at least
    one image gains a ``local_images`` field of ``{url, path}`` dicts. Paths are
    relative to ``out_dir``. Existing fields are never modified. Inventory writes
    are atomic.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    inv = json.loads(inventory_path.read_text(encoding="utf-8"))

    entries_processed = 0
    downloaded = 0
    skipped = 0
    failed = 0

    for entry in inv.get("saves", []):
        if uris is not None and entry.get("uri") not in uris:
            continue
        urls = list(dict.fromkeys(_iter_image_urls(entry)))  # ordered dedup
        if not urls:
            continue
        entries_processed += 1
        local_images: list[dict] = []
        for url in urls:
            fname = filename_for_url(url)
            dest = out_dir / fname
            if dest.exists():
                skipped += 1
            else:
                try:
                    download_to(url, dest, user_agent=user_agent)
                    downloaded += 1
                except Exception as e:
                    failed += 1
                    print(
                        f"  FAIL {url[:80]}: {type(e).__name__}: {e}",
                        file=sys.stderr,
                    )
                    continue
            local_images.append({"url": url, "path": fname})
        if local_images:
            entry["local_images"] = local_images

    inv["fetched_at"] = _now_iso()
    inventory_path.write_text(
        json.dumps(inv, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(
        f"bsky-saves: processed {entries_processed} entries, "
        f"downloaded {downloaded} images, skipped {skipped} (already present), "
        f"{failed} failed",
        file=sys.stderr,
    )
    return entries_processed, downloaded, skipped, failed
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_images.py -v
```

Expected: all 17 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/images.py tests/test_images.py
git commit -m "feat(images): add hydrate_images entry point (happy path)"
```

---

## Task 6: TDD — `--uris` filter limits processing to a subset

**Files:**
- Test: `tests/test_images.py`

Verifies that the `uris` parameter limits which entries are processed. The implementation already supports it (Task 5); this task ensures coverage.

- [ ] **Step 1: Add failing tests to `tests/test_images.py`**

Append:

```python
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
```

- [ ] **Step 2: Run tests to verify they pass (no implementation change needed)**

```bash
pytest tests/test_images.py -v
```

Expected: all 20 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_images.py
git commit -m "test(images): cover --uris filter behavior"
```

---

## Task 7: TDD — Idempotency: pre-existing files not re-downloaded; no duplicate entries on re-run

**Files:**
- Test: `tests/test_images.py`

The implementation already handles both cases (existing-file skip in Task 5; the dict-based dedup of URLs makes re-runs naturally idempotent because the same `local_images` array is rebuilt with the same content). Tests confirm.

- [ ] **Step 1: Add failing tests to `tests/test_images.py`**

Append:

```python
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
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
pytest tests/test_images.py -v
```

Expected: all 22 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_images.py
git commit -m "test(images): cover idempotency for existing files and repeat runs"
```

---

## Task 8: TDD — Per-image failure is non-fatal

**Files:**
- Test: `tests/test_images.py`

A failing download counts as `failed`, doesn't appear in `local_images`, and doesn't abort processing of other images.

- [ ] **Step 1: Add failing test to `tests/test_images.py`**

Append:

```python
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
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
pytest tests/test_images.py -v
```

Expected: all 24 tests pass. (`download_to` raises `httpx.HTTPStatusError` on `raise_for_status()` for 500s, which the existing `except Exception` catches.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_images.py
git commit -m "test(images): cover per-image failure non-fatality"
```

---

## Task 9: TDD — Atomic inventory write (temp file + rename)

**Files:**
- Modify: `src/bsky_saves/images.py`
- Test: `tests/test_images.py`

Currently the implementation writes the inventory directly with `inventory_path.write_text(...)`. The spec requires atomic writes (temp file + `os.rename`) to prevent corruption if the process is killed mid-write. Switch to a temp-file-then-rename pattern.

- [ ] **Step 1: Add failing test to `tests/test_images.py`**

Append:

```python
import os


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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_images.py::test_hydrate_images_atomic_write_via_tmp_file -v
```

Expected: AssertionError — `len(rename_calls) == 0` because the current implementation writes directly.

- [ ] **Step 3: Modify `hydrate_images` to write atomically**

In `src/bsky_saves/images.py`, add `import os` at the top with the other imports, then replace the final `inventory_path.write_text(...)` block in `hydrate_images` with:

```python
    inv["fetched_at"] = _now_iso()
    tmp_path = inventory_path.with_suffix(inventory_path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(inv, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.rename(tmp_path, inventory_path)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_images.py -v
```

Expected: all 25 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/images.py tests/test_images.py
git commit -m "feat(images): atomic inventory write via tmp file + rename"
```

---

## Task 10: Wire up the new CLI subcommand

**Files:**
- Modify: `src/bsky_saves/cli.py`
- Test: `tests/test_images.py`

Replace the v0.1 `hydrate images` subparser (currently has `--stories`, `--assets`, `--assets-url-prefix`) with the v0.2 shape (`--inventory`, `--out`, `--uris`). Update the dispatch in `main()`.

- [ ] **Step 1: Add failing CLI integration test to `tests/test_images.py`**

Append:

```python
from bsky_saves.cli import main as cli_main


@respx.mock
def test_cli_hydrate_images_basic_flow(fixture_factory, tmp_path):
    f = fixture_factory
    inv = f.inventory(
        f.entry("at://x/p/1", images=[f.image("https://cdn.bsky.app/a.jpg")]),
        f.entry("at://x/p/2", images=[f.image("https://cdn.bsky.app/b.jpg")]),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")
    out_dir = tmp_path / "imgs"

    respx.get("https://cdn.bsky.app/a.jpg").respond(200, content=b"A")
    respx.get("https://cdn.bsky.app/b.jpg").respond(200, content=b"B")

    rc = cli_main([
        "hydrate", "images",
        "--inventory", str(inv_path),
        "--out", str(out_dir),
    ])
    assert rc == 0

    written = json.loads(inv_path.read_text(encoding="utf-8"))
    assert all("local_images" in s for s in written["saves"])


@respx.mock
def test_cli_hydrate_images_with_uris_file(fixture_factory, tmp_path):
    f = fixture_factory
    inv = f.inventory(
        f.entry("at://x/p/1", images=[f.image("https://cdn.bsky.app/a.jpg")]),
        f.entry("at://x/p/2", images=[f.image("https://cdn.bsky.app/b.jpg")]),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")
    uris_path = tmp_path / "uris.txt"
    uris_path.write_text("at://x/p/1\n", encoding="utf-8")

    respx.get("https://cdn.bsky.app/a.jpg").respond(200, content=b"A")
    route_b = respx.get("https://cdn.bsky.app/b.jpg").respond(200, content=b"B")

    rc = cli_main([
        "hydrate", "images",
        "--inventory", str(inv_path),
        "--out", str(tmp_path / "imgs"),
        "--uris", str(uris_path),
    ])
    assert rc == 0
    assert not route_b.called

    written = json.loads(inv_path.read_text(encoding="utf-8"))
    by_uri = {s["uri"]: s for s in written["saves"]}
    assert "local_images" in by_uri["at://x/p/1"]
    assert "local_images" not in by_uri["at://x/p/2"]


def test_cli_hydrate_images_old_flags_rejected(tmp_path):
    """v0.1 --stories / --assets / --assets-url-prefix are gone."""
    import argparse

    with pytest.raises(SystemExit):
        cli_main([
            "hydrate", "images",
            "--stories", str(tmp_path),
            "--assets", str(tmp_path),
        ])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_images.py -v -k "test_cli_hydrate_images"
```

Expected: failures — old flags still accepted, `--out` / `--uris` not recognized, etc.

- [ ] **Step 3: Replace the `hydrate images` subparser block in `src/bsky_saves/cli.py`**

Find the existing block (currently `lines 69-86`):

```python
    p_images = hsub.add_parser("images", help="Localize cdn.bsky.app image refs in Markdown files.")
    p_images.add_argument(
        "--stories",
        type=Path,
        required=True,
        help="Directory of Markdown files to scan.",
    )
    p_images.add_argument(
        "--assets",
        type=Path,
        required=True,
        help="Directory to download images into (under <slug>/ subdirs).",
    )
    p_images.add_argument(
        "--assets-url-prefix",
        default="/assets/stories",
        help="Root-relative URL prefix that will replace cdn.bsky.app URLs (default: /assets/stories).",
    )
```

Replace with:

```python
    p_images = hsub.add_parser("images", help="Download CDN images referenced in the inventory.")
    _add_inventory_arg(p_images)
    p_images.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Directory to download images into (flat layout; created if absent).",
    )
    p_images.add_argument(
        "--uris",
        type=Path,
        default=None,
        help="Optional newline-delimited list of at:// post URIs to limit download to. "
             "If omitted, all inventory entries with images are processed.",
    )
```

- [ ] **Step 4: Replace the dispatch block for `hydrate images` in `main()`**

Find the existing block (currently around `lines 133-141`):

```python
        if args.hydrate_what == "images":
            from .images import localize_images

            localize_images(
                args.stories,
                args.assets,
                assets_url_prefix=args.assets_url_prefix,
            )
            return 0
```

Replace with:

```python
        if args.hydrate_what == "images":
            from .images import hydrate_images

            hydrate_images(
                args.inventory,
                args.out,
                uris=_load_uris(args.uris),
            )
            return 0
```

- [ ] **Step 5: Update the docstring at the top of `src/bsky_saves/cli.py`**

Find the line:
```python
  bsky-saves hydrate images   --stories DIR --assets DIR [--assets-url-prefix /assets/stories]
```

Replace with:
```python
  bsky-saves hydrate images   --inventory PATH --out DIR [--uris FILE]
```

- [ ] **Step 6: Run tests to verify all pass**

```bash
pytest tests/ -v
```

Expected: all tests pass (including existing `test_fetch.py`, `test_normalize.py`, `test_tid.py`).

- [ ] **Step 7: Commit**

```bash
git add src/bsky_saves/cli.py tests/test_images.py
git commit -m "feat(cli): replace hydrate images subcommand with inventory-driven shape"
```

---

## Task 11: Remove dead code from images.py

**Files:**
- Modify: `src/bsky_saves/images.py`

The old `IMG_PATTERN`, `slug_from_frontmatter`, and `localize_images` functions are no longer reachable. Delete them. Also drop the `re` import (no longer used) and update the module docstring.

- [ ] **Step 1: Replace the docstring and removed-symbols block**

In `src/bsky_saves/images.py`, replace the existing module docstring (lines 1-12, the one that starts with `"""Localize CDN image references in Markdown files.`) with:

```python
"""Download CDN images referenced by inventory entries.

Walks an inventory entry to discover image URLs (post images, quoted-post
images, same-author thread reply images, quoted-post thread reply images),
downloads each into a flat output directory using a deterministic
hash-based filename, and records a ``local_images`` field of
``{url, path}`` mappings on each affected entry.

Idempotent: existing files on disk are not re-downloaded; re-running the
function rebuilds identical ``local_images`` arrays.
"""
```

- [ ] **Step 2: Remove `import re` (no longer used)**

Remove the line `import re` from the imports.

- [ ] **Step 3: Delete the `IMG_PATTERN` regex constant**

Delete this block:

```python
# Markdown image syntax: ![alt](url). Captures the leading "![alt](" and
# trailing ")" so we can replace just the URL.
IMG_PATTERN = re.compile(
    r'(?P<head>!\[[^\]]*\]\()'
    r'(?P<url>https://cdn\.bsky\.app/[^)\s]+)'
    r'(?P<tail>\))'
)
```

- [ ] **Step 4: Delete `slug_from_frontmatter`**

Delete this function:

```python
def slug_from_frontmatter(text: str) -> str | None:
    m = re.search(r"^slug:\s*(\S+)", text, re.MULTILINE)
    return m.group(1) if m else None
```

- [ ] **Step 5: Delete `localize_images`**

Delete the entire `localize_images` function (the v0.1 entry point — long function ending with the `return total_downloaded, total_rewritten, total_failed` line).

- [ ] **Step 6: Run tests to verify nothing breaks**

```bash
pytest tests/ -v
```

Expected: all tests pass. No imports from the deleted symbols anywhere in `src/` or `tests/`.

- [ ] **Step 7: Confirm no stale references with grep**

```bash
grep -rn "localize_images\|slug_from_frontmatter\|IMG_PATTERN" src/ tests/
```

Expected: no output.

- [ ] **Step 8: Commit**

```bash
git add src/bsky_saves/images.py
git commit -m "refactor(images): remove dead v0.1 Markdown rewriter code"
```

---

## Task 12: Bump version and User-Agent string to 0.2.0

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/bsky_saves/images.py`

- [ ] **Step 1: Bump `pyproject.toml` version**

In `pyproject.toml`, change:
```toml
version = "0.1.0"
```
to:
```toml
version = "0.2.0"
```

- [ ] **Step 2: Bump the `DEFAULT_USER_AGENT` string in `src/bsky_saves/images.py`**

Change:
```python
DEFAULT_USER_AGENT = (
    "bsky-saves/0.1 (+https://github.com/tenorune/bsky-saves)"
)
```
to:
```python
DEFAULT_USER_AGENT = (
    "bsky-saves/0.2 (+https://github.com/tenorune/bsky-saves)"
)
```

- [ ] **Step 3: Verify version reads correctly**

```bash
python -c "import tomllib; print(tomllib.loads(open('pyproject.toml','rb').read().decode())['project']['version'])"
```

Expected: `0.2.0`.

- [ ] **Step 4: Run full test suite once more**

```bash
pytest tests/ -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/bsky_saves/images.py
git commit -m "chore: bump version to 0.2.0; refresh User-Agent string"
```

---

## Task 13: Update README

**Files:**
- Modify: `README.md`

The README should describe the v0.2 `hydrate images` CLI. No deprecation banner.

- [ ] **Step 1: Read the current README to find the relevant section**

```bash
cat README.md
```

Note the section that documents `hydrate images` (likely a CLI usage section listing all subcommands with their flags and a short description).

- [ ] **Step 2: Replace the `hydrate images` section**

Find the section describing `hydrate images`. Replace whatever flag descriptions / examples appear there with this content (preserve surrounding markdown structure — heading level, code-block style, etc., to match neighbours):

```markdown
### `bsky-saves hydrate images`

Download CDN images referenced by inventory entries into a flat output directory, and record `url → path` mappings as a `local_images` field on each affected entry.

```
bsky-saves hydrate images --inventory PATH --out DIR [--uris FILE]
```

| Flag | Required | Description |
|---|---|---|
| `--inventory PATH` | yes | Path to the JSON inventory written by `bsky-saves fetch`. Read for image URL discovery; written to record `local_images`. |
| `--out DIR` | yes | Directory to download images into. Created if absent. Flat layout — no per-post subdirectories. |
| `--uris FILE` | no | Newline-delimited list of `at://...` post URIs. Only entries whose URI is in this list are processed. Lines beginning with `#` and blank lines are ignored. URIs absent from the inventory are silently skipped. If omitted, every inventory entry with images is processed. |

The function is idempotent: pre-existing files on disk are not re-downloaded, and re-running rebuilds identical `local_images` arrays. Per-image failures are non-fatal — they're counted and logged to stderr, and processing continues.

Each processed entry that has at least one image gains a `local_images` field:

```json
"local_images": [
  { "url": "https://cdn.bsky.app/...", "path": "img-9f2c8e1b....jpg" }
]
```

`path` is relative to `--out DIR`. Downstream tools (e.g., HTML/Markdown renderers) join `<out-dir>/<path>` or rebase as needed.
```

- [ ] **Step 3: Verify the rest of the README still makes sense**

```bash
grep -in "stories\|--assets\|assets-url-prefix\|jekyll\|frontmatter\|slug" README.md
```

Expected: no remaining references to the old image flags or to stories-of-47-specific concepts. If matches appear, edit them out (only those that describe the old `hydrate images` behavior — leave general project description alone).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(readme): update hydrate images for v0.2 CLI"
```

---

## Task 14: Final verification gate

**Files:** none (verification only)

This is the gate before pushing the v0.2 branch and before tagging the release.

- [ ] **Step 1: Run the full test suite from a clean state**

```bash
cd /home/user/bsky-saves
pytest tests/ -v
```

Expected: every test passes (existing `test_fetch.py`, `test_normalize.py`, `test_tid.py` plus the full `test_images.py` suite from this plan).

- [ ] **Step 2: Build sdist and wheel**

```bash
pip install build twine
python -m build
ls -la dist/
```

Expected: `dist/bsky_saves-0.2.0-py3-none-any.whl` and `dist/bsky_saves-0.2.0.tar.gz` both present.

- [ ] **Step 3: Run twine check on the artifacts**

```bash
twine check dist/*
```

Expected: `PASSED` for both files.

- [ ] **Step 4: Smoke-test the wheel in a clean venv**

```bash
python -m venv /tmp/v02-smoke
/tmp/v02-smoke/bin/pip install dist/bsky_saves-0.2.0-py3-none-any.whl
/tmp/v02-smoke/bin/bsky-saves --help
/tmp/v02-smoke/bin/bsky-saves hydrate --help
/tmp/v02-smoke/bin/bsky-saves hydrate images --help
```

Expected: all three help outputs render. The `hydrate images --help` output must show only `--inventory`, `--out`, `--uris` (and standard `-h/--help`); no `--stories`, `--assets`, or `--assets-url-prefix` may appear anywhere in any help text.

- [ ] **Step 5: Confirm grep**

```bash
/tmp/v02-smoke/bin/bsky-saves hydrate images --help 2>&1 | grep -E "stories|assets|frontmatter|slug" || echo "CLEAN"
```

Expected: `CLEAN`.

- [ ] **Step 6: Push the v0.2 branch**

```bash
git push -u origin v0.2
```

Expected: branch pushed; tracking set up.

- [ ] **Step 7: Stop and hand off**

At this point, the bsky-saves side of v0.2 is implementation-complete and pushed. Per the spec (§6.3), the next gate is the **stories-of-47 integration test** — a separate effort in `tenorune/tenorune.github.io` on a `migrate-bsky-saves-v0.2` branch that installs `bsky-saves` from this v0.2 git branch and verifies its build still produces the expected output. Do not merge `v0.2 → main` and do not tag `v0.2.0` until that integration test passes.

Report back to the user:
- All tests green.
- Wheel + sdist built and twine-checked.
- v0.2 branch pushed.
- Awaiting stories-of-47 integration test before final release.

---

## Self-review notes

After writing this plan I checked it against the spec:

- **Spec §3.1 (CLI flags):** Task 10 implements `--inventory`, `--out`, `--uris` and removes `--stories`/`--assets`/`--assets-url-prefix`. Covered.
- **Spec §3.2 (behavior):** Task 5 (happy path), Task 6 (filter), Task 7 (idempotency), Task 8 (failure handling), Task 9 (atomic write). All covered.
- **Spec §3.3 (what it doesn't do):** Task 11 deletes the Markdown/frontmatter/IMG_PATTERN code. Covered.
- **Spec §4.1 (`local_images` shape):** Task 5 tests the exact shape. The "no `local_images` key when no images" case is in Task 5's `test_hydrate_images_no_images_no_local_images_field`. The "no duplicates on rerun" case is in Task 7's `test_hydrate_images_idempotent_across_runs`. The "failed downloads not recorded" case is in Task 8's `test_hydrate_images_per_image_failure_nonfatal`. All covered.
- **Spec §4.2 (filename format):** `filename_for_url` is unchanged from v0.1 (kept per Task 11's "keep these symbols" list). The test `test_filename_for_url_deterministic` named in the spec isn't strictly necessary because the existing v0.1 helper already has implicit coverage via every test that compares against `filename_for_url(url)`; but for completeness the spec called it out — I've folded its semantics into the assertions in Tasks 5, 6, and 7 rather than a standalone test, which I think is sufficient. If a strict standalone test is wanted, add it to Task 5's test file.
- **Spec §4.3 (mutation rules):** Task 5's `test_hydrate_images_preserves_existing_fields` covers "existing fields never modified." Atomic write covered in Task 9. Pretty-print + sort_keys is in the implementation in Task 5.
- **Spec §5.1 (kept/removed/added symbols):** Task 11 removes the right symbols; Tasks 5 and 9 add the new ones; the helper in Task 3 is the private one mentioned in the spec.
- **Spec §5.2 (CLI changes):** Task 10. Covered.
- **Spec §5.3 (test list):** All ten tests called out in the spec map to tests in this plan. Cross-walk:
  - `test_hydrate_images_default_processes_all` → Task 5's `test_hydrate_images_downloads_all_entries`.
  - `test_hydrate_images_uris_filter` → Task 6's `test_hydrate_images_uris_filter_processes_only_listed`.
  - `test_hydrate_images_uris_file_strips_comments_and_blanks` → Task 4's `test_load_uris_strips_comments_and_blanks`.
  - `test_hydrate_images_unknown_uri_silently_skipped` → Task 6's `test_hydrate_images_uris_unknown_uri_silently_skipped`.
  - `test_hydrate_images_idempotent_existing_file` → Task 7's `test_hydrate_images_skips_existing_file`.
  - `test_hydrate_images_idempotent_inventory_field` → Task 7's `test_hydrate_images_idempotent_across_runs`.
  - `test_hydrate_images_per_image_failure_nonfatal` → Task 8's `test_hydrate_images_per_image_failure_nonfatal`.
  - `test_hydrate_images_no_images_no_field` → Task 5's `test_hydrate_images_no_images_no_local_images_field`.
  - `test_hydrate_images_atomic_write` → Task 9's `test_hydrate_images_atomic_write_via_tmp_file`.
  - `test_filename_for_url_deterministic` → folded into Tasks 5/6/7 assertions (see note above).
- **Spec §5.4 (version bump):** Task 12.
- **Spec §5.5 (README):** Task 13. No migration banner per the user's later edit.
- **Spec §6 (stories-of-47 migration):** Out of scope for this plan (separate repo). Task 14 step 7 explicitly hands off without tagging the release.
- **Spec §7 (test plan / release gates):** Task 14 covers §§7.1, 7.2, 7.3, 7.4. §§7.5 and 7.6 are explicitly deferred to the stories-of-47 effort and the post-release validation.
- **Spec §8 (out of scope):** Plan respects all YAGNI items — no sharded layout, no per-image granularity, no compat shim.

No placeholders. No "TBD" outside the intentional spec deferral noted in §6.2 of the spec (which is stories-of-47's problem). Type signatures are consistent: `hydrate_images(inventory_path: Path, out_dir: Path, *, uris: set[str] | None = None, user_agent: str = DEFAULT_USER_AGENT) -> tuple[int, int, int, int]` is the same shape across Tasks 5, 6, 7, 8, 9, 10. Method names match (`_iter_image_urls`, `_load_uris`, `hydrate_images`).
