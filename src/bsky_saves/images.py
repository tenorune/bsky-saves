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
import re
import sys
from pathlib import Path

import httpx

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
