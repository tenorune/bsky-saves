"""Enrich inventory with offline-derivable fields.

Currently:
- ``post_created_at`` decoded from each save's TID rkey.
- Cleans bogus ``article_published_at`` values (within ±1 day of
  ``article_fetched_at``, or after ``post_created_at``) — common when
  trafilatura defaults to "today" on metadata-poor hosts (YouTube, etc.).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from .tid import decode_tid_to_iso, rkey_of


def parse_iso(s):
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s + "T00:00:00+00:00")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz=None).replace(tzinfo=None)
    return dt


def enrich_inventory(inventory_path: Path, *, refresh: bool = False) -> dict:
    """Enrich the inventory file in place. Returns a stats dict.

    Skips the file write entirely when no entry was actually mutated, so a
    re-run on an already-enriched, already-clean inventory is a true no-op
    on disk.
    """
    inv = json.loads(inventory_path.read_text(encoding="utf-8"))
    saves = inv["saves"]
    added = 0
    skipped = 0
    failed = 0
    pub_dropped = 0
    changed = False

    for s in saves:
        if not s.get("post_created_at") or refresh:
            try:
                new_val = decode_tid_to_iso(rkey_of(s["uri"]))
                if s.get("post_created_at") != new_val:
                    s["post_created_at"] = new_val
                    changed = True
                added += 1
            except Exception as e:
                print(f"  failed: {s.get('uri')!r}: {e}", file=sys.stderr)
                failed += 1
        else:
            skipped += 1

        # Clean bogus article_published_at:
        #   (a) pub date is after the post date — the article can't have
        #       been published after the BlueSky post that links it.
        #   (b) pub date equals the fetch date (within 36h) — fall-back
        #       behaviour caught here for entries where the post date
        #       wasn't yet known.
        pub = parse_iso(s.get("article_published_at"))
        post = parse_iso(s.get("post_created_at"))
        fetched = parse_iso(s.get("article_fetched_at"))
        bogus = False
        if pub and post and pub > post:
            bogus = True
        elif pub and fetched and abs((pub - fetched).total_seconds()) < 86400 * 1.5:
            bogus = True
        if bogus:
            s.pop("article_published_at", None)
            pub_dropped += 1
            changed = True

    if changed:
        inventory_path.write_text(
            json.dumps(inv, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    stats = {
        "added": added,
        "skipped": skipped,
        "failed": failed,
        "pub_dropped": pub_dropped,
        "total": len(saves),
    }
    print(
        f"enriched: added={stats['added']} skipped={stats['skipped']} "
        f"failed={stats['failed']} pub_dropped={stats['pub_dropped']} "
        f"total={stats['total']}"
    )
    return stats
