"""Hydrate inventory entries with article_text and article_published_at.

Iterates the inventory for entries whose embed.url has not yet been fetched,
downloads the article HTML, extracts the main text and the publication date
via trafilatura, and writes the result back into the entry's ``article_text``
and (if extractable) ``article_published_at`` fields.

Idempotent: entries with ``article_text`` already populated are skipped
unless they're missing ``article_published_at`` AND ``refresh_dates=True``.
Failed fetches are marked with ``article_fetch_error`` so subsequent runs
don't pointlessly re-hit them.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import trafilatura

DEFAULT_USER_AGENT = (
    "bsky-saves/0.1 (+https://github.com/tenorune/bsky-saves)"
)
RATE_LIMIT_SEC = 1.0
TIMEOUT = 30.0
MIN_EXTRACT_CHARS = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_article(url: str, *, user_agent: str = DEFAULT_USER_AGENT) -> tuple[dict | None, str | None]:
    """Return (result, error). result is {"text": str, "date": str|None}."""
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.8"},
            follow_redirects=True,
            timeout=TIMEOUT,
        )
    except Exception as e:
        return None, f"fetch_error:{type(e).__name__}:{str(e)[:120]}"

    if r.status_code >= 400:
        return None, f"http_{r.status_code}"

    extracted = trafilatura.bare_extraction(
        r.text,
        include_comments=False,
        include_tables=False,
        favor_recall=True,
        with_metadata=True,
    )
    if extracted is None:
        return None, "extraction_failed"

    text = getattr(extracted, "text", None) if not isinstance(extracted, dict) else extracted.get("text")
    date = getattr(extracted, "date", None) if not isinstance(extracted, dict) else extracted.get("date")

    if not text or len(text.strip()) < MIN_EXTRACT_CHARS:
        return None, "extraction_too_short_or_empty"
    return {"text": text.strip(), "date": date}, None


def hydrate_articles(
    inventory_path: Path,
    *,
    refresh_dates: bool = False,
    user_agent: str = DEFAULT_USER_AGENT,
) -> tuple[int, int]:
    """Hydrate every external-link entry in the inventory. Returns (success, failed)."""
    inv = json.loads(inventory_path.read_text(encoding="utf-8"))
    saves = inv["saves"]

    pending = []
    for s in saves:
        embed = s.get("embed") or {}
        if embed.get("type") != "external":
            continue
        url = embed.get("url")
        if not url:
            continue
        already_text = bool(s.get("article_text"))
        already_date = bool(s.get("article_published_at"))
        if already_text and (already_date or not refresh_dates):
            continue
        if s.get("article_fetch_error") and not refresh_dates:
            continue
        pending.append(s)

    if not pending:
        print("bsky-saves: nothing to hydrate", file=sys.stderr)
        return 0, 0

    print(
        f"bsky-saves: {len(pending)} article entries to fetch (refresh_dates={refresh_dates})",
        file=sys.stderr,
    )

    success = 0
    failed = 0
    for i, s in enumerate(pending, 1):
        url = s["embed"]["url"]
        print(f"  [{i}/{len(pending)}] {url[:100]}", file=sys.stderr)
        result, error = fetch_article(url, user_agent=user_agent)
        s["article_fetched_at"] = _now_iso()
        if result:
            if not s.get("article_text"):
                s["article_text"] = result["text"]
            if result.get("date"):
                s["article_published_at"] = result["date"]
            s.pop("article_fetch_error", None)
            success += 1
            print(
                f"    ok (text={len(result['text'])} chars, date={result.get('date') or '—'})",
                file=sys.stderr,
            )
        else:
            if not s.get("article_text"):
                s["article_fetch_error"] = error
            failed += 1
            print(f"    FAIL: {error}", file=sys.stderr)
        time.sleep(RATE_LIMIT_SEC)

    inv["fetched_at"] = _now_iso()
    inventory_path.write_text(
        json.dumps(inv, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(
        f"bsky-saves: hydrated {success}, failed {failed}",
        file=sys.stderr,
    )
    return success, failed
