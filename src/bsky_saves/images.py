"""Localize CDN image references in Markdown files.

Scans Markdown files for inline image references whose URL points at
``cdn.bsky.app``, downloads each image into ``<assets>/<slug>/`` using a
deterministic hash-based filename, and rewrites the Markdown body to use
the local root-relative path.

Idempotent: if a target file already exists locally, the URL is just
rewritten (no redundant download). Per-file slug is read from frontmatter
``slug:`` field, which is the convention this tool was extracted from
(Jekyll-style YAML frontmatter).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx


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


DEFAULT_USER_AGENT = (
    "bsky-saves/0.1 (+https://github.com/tenorune/bsky-saves)"
)
TIMEOUT = 30.0

# Markdown image syntax: ![alt](url). Captures the leading "![alt](" and
# trailing ")" so we can replace just the URL.
IMG_PATTERN = re.compile(
    r'(?P<head>!\[[^\]]*\]\()'
    r'(?P<url>https://cdn\.bsky\.app/[^)\s]+)'
    r'(?P<tail>\))'
)


def filename_for_url(url: str) -> str:
    """Deterministic filename: 16-hex-char SHA256 prefix + .jpg."""
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"img-{h}.jpg"


def slug_from_frontmatter(text: str) -> str | None:
    m = re.search(r"^slug:\s*(\S+)", text, re.MULTILINE)
    return m.group(1) if m else None


def download_to(url: str, dest: Path, *, user_agent: str = DEFAULT_USER_AGENT) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = httpx.get(
        url,
        headers={"User-Agent": user_agent, "Accept": "image/*"},
        follow_redirects=True,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    dest.write_bytes(r.content)


def localize_images(
    stories_dir: Path,
    assets_dir: Path,
    *,
    assets_url_prefix: str = "/assets/stories",
    user_agent: str = DEFAULT_USER_AGENT,
) -> tuple[int, int, int]:
    """Localize cdn.bsky.app image refs in every ``*.md`` under stories_dir.

    Downloads to ``<assets_dir>/<slug>/<filename>`` and rewrites refs to
    ``<assets_url_prefix>/<slug>/<filename>``.

    Returns (downloaded, rewritten, failed).
    """
    if not stories_dir.exists():
        print(f"bsky-saves: no {stories_dir} directory; nothing to do", file=sys.stderr)
        return 0, 0, 0

    total_downloaded = 0
    total_rewritten = 0
    total_failed = 0

    for story_path in sorted(stories_dir.glob("*.md")):
        text = story_path.read_text(encoding="utf-8")
        slug = slug_from_frontmatter(text)
        if not slug:
            continue

        new_parts: list[str] = []
        last_end = 0
        modified = False
        for match in IMG_PATTERN.finditer(text):
            url = match.group("url")
            fname = filename_for_url(url)
            local_path = assets_dir / slug / fname
            local_url = f"{assets_url_prefix.rstrip('/')}/{slug}/{fname}"

            if not local_path.exists():
                try:
                    download_to(url, local_path, user_agent=user_agent)
                    total_downloaded += 1
                    print(
                        f"  downloaded {url[:80]} -> {local_path}",
                        file=sys.stderr,
                    )
                except Exception as e:
                    total_failed += 1
                    print(
                        f"  FAIL {url[:80]}: {type(e).__name__}: {e}",
                        file=sys.stderr,
                    )
                    continue

            new_parts.append(text[last_end : match.start()])
            new_parts.append(match.group("head") + local_url + match.group("tail"))
            last_end = match.end()
            total_rewritten += 1
            modified = True

        if modified:
            new_parts.append(text[last_end:])
            story_path.write_text("".join(new_parts), encoding="utf-8")
            print(f"rewrote {story_path.name}", file=sys.stderr)

    print(
        f"bsky-saves: downloaded {total_downloaded}, "
        f"rewrote {total_rewritten} refs, {total_failed} failed",
        file=sys.stderr,
    )
    return total_downloaded, total_rewritten, total_failed


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
    tmp_path = inventory_path.with_suffix(inventory_path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(inv, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.rename(tmp_path, inventory_path)

    print(
        f"bsky-saves: processed {entries_processed} entries, "
        f"downloaded {downloaded} images, skipped {skipped} (already present), "
        f"{failed} failed",
        file=sys.stderr,
    )
    return entries_processed, downloaded, skipped, failed
