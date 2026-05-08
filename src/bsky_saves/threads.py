"""Hydrate inventory entries with thread_replies — same-author posts in
the thread descending from the bookmarked post.

For each save, calls ``app.bsky.feed.getPostThread`` on the public AT
Protocol AppView (no auth needed) and collects descendant posts whose
author DID matches the bookmarked post's author. Stored as
``thread_replies`` in the entry. Also walks any quoted-post target's
thread.

Idempotent: skips entries whose stored ``thread_schema_version`` matches
the current value, or marked with ``thread_fetch_error``.

Crash-safe: per-iteration atomic writes (added in v0.4.2) durably persist
each save's hydration to disk before moving to the next, so a process
killed mid-loop (Ctrl-C, ``Worker.terminate()``) preserves all completed
saves. The skip conditions above let a re-run resume from where it
stopped.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .normalize import extract_media

# Bump this when the thread_replies schema changes; entries whose stored
# thread_schema_version is below the current value are re-fetched on the
# next run.
#
# Schema versions:
#   v1 — initial: {uri, indexedAt, text}
#   v2 — added images
#   v3 — also walks the thread of a save's quoted_post
#   v4 — collect_same_author_replies tightened to only include posts that
#        form an unbroken same-author chain from the root, excluding the
#        OP's responses to other people's comments
THREAD_SCHEMA_VERSION = 4

DEFAULT_APPVIEW = "https://public.api.bsky.app"
DEFAULT_USER_AGENT = (
    "bsky-saves/0.1 (+https://github.com/tenorune/bsky-saves)"
)
RATE_LIMIT_SEC = 0.5
TIMEOUT = 30.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_inventory(inventory_path: Path, inv: dict) -> None:
    """Atomically write the inventory dict to disk via temp-file + os.replace.

    A process killed mid-write never leaves a corrupted JSON file — the
    rename is atomic on POSIX and Windows alike (os.replace overwrites
    destination cross-platform; os.rename has Windows-side quirks).
    """
    tmp = inventory_path.with_suffix(inventory_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(inv, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, inventory_path)


def fetch_thread(
    uri: str,
    *,
    appview: str = DEFAULT_APPVIEW,
    user_agent: str = DEFAULT_USER_AGENT,
) -> tuple[dict | None, str | None]:
    """Returns (thread_root, error). Exactly one is non-None."""
    try:
        r = httpx.get(
            f"{appview.rstrip('/')}/xrpc/app.bsky.feed.getPostThread",
            params={"uri": uri},
            headers={"User-Agent": user_agent},
            timeout=TIMEOUT,
        )
    except Exception as e:
        return None, f"fetch_error:{type(e).__name__}:{str(e)[:120]}"
    if r.status_code >= 400:
        return None, f"http_{r.status_code}"
    body = r.json()
    return body.get("thread"), None


def collect_same_author_replies(thread: dict, author_did: str) -> list[dict]:
    """Walk the thread tree depth-first, returning posts that form an
    unbroken same-author chain from the root.

    Includes posts authored by ``author_did`` whose direct parent in the
    thread tree is also authored by ``author_did`` (transitively up to the
    root). Excludes posts where the chain has been broken by a different
    author — typically the OP's responses to other people's comments. The
    walker stops descending once a different-author post is encountered, so
    same-author posts nested inside someone else's reply are not collected
    either.

    Each collected post has its embedded media extracted via extract_media.
    """
    out: list[dict] = []
    seen_uris: set[str] = set()

    def visit(node, in_chain: bool):
        if not isinstance(node, dict):
            return
        for reply in node.get("replies", []) or []:
            post = (reply or {}).get("post") or {}
            author = post.get("author", {})
            uri = post.get("uri", "")
            is_same_author = author.get("did") == author_did
            if in_chain and is_same_author and uri and uri not in seen_uris:
                record = post.get("record", {}) or {}
                embed_view = post.get("embed") or {}
                out.append(
                    {
                        "uri": uri,
                        "indexedAt": post.get("indexedAt", ""),
                        "text": record.get("text", ""),
                        "images": extract_media(embed_view),
                    }
                )
                seen_uris.add(uri)
                visit(reply, in_chain=True)
            # If the chain is broken at this reply (different author, or a
            # parent already had a different author), do NOT recurse — any
            # same-author posts below are not self-thread continuations.

    # The root is the bookmarked post by `author_did`, so its direct
    # children's parent-author IS author_did → start with in_chain=True.
    visit(thread, in_chain=True)
    return out


def hydrate_threads(
    inventory_path: Path,
    *,
    appview: str = DEFAULT_APPVIEW,
    user_agent: str = DEFAULT_USER_AGENT,
    limit: int | None = None,
) -> tuple[int, int, int]:
    """Hydrate saves with same-author thread descendants.

    Args:
        inventory_path: Path to the inventory JSON file.
        appview: AppView base URL for thread fetches.
        user_agent: User-Agent header for outbound HTTP requests.
        limit: Optional cap on the number of pending saves to process in
            this call. ``None`` (default) processes every pending save —
            byte-identical to pre-v0.4.3 behavior. A non-negative ``int``
            processes at most ``limit`` saves and returns; the next call
            resumes from where this one stopped via the existing
            skip-already-done conditions. Both succeeded and failed saves
            count toward ``limit``. ``limit=0`` is a valid no-op.

    Returns:
        ``(succeeded, failed, remaining)`` — ``remaining`` is the count of
        pending saves still un-hydrated when the function returned. The
        caller stops when ``remaining == 0``.

    Raises:
        ValueError: ``limit`` is negative or not ``None``/``int``.

    `fetched_at` semantics: stamped on the final write only when this call
    exhausted ``pending`` (i.e., ``remaining == 0``). A call that returns
    because it hit ``limit`` does NOT stamp, preserving the
    ``fetched_at`` = "a complete pass finished" invariant for CLI users.
    """
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative int or None")

    inv = json.loads(inventory_path.read_text(encoding="utf-8"))
    saves = inv["saves"]

    pending = []
    for s in saves:
        if (
            "thread_replies" in s
            and s.get("thread_schema_version") == THREAD_SCHEMA_VERSION
        ):
            continue
        if s.get("thread_fetch_error"):
            continue
        pending.append(s)

    if not pending:
        print("bsky-saves: nothing to hydrate", file=sys.stderr)
        return 0, 0, 0

    if limit == 0:
        # Valid no-op call. Don't stamp fetched_at (pending isn't exhausted).
        return 0, 0, len(pending)

    print(
        f"bsky-saves: {len(pending)} entries to hydrate threads"
        + (f" (limit={limit})" if limit is not None else ""),
        file=sys.stderr,
    )

    success = 0
    failed = 0
    found_any = 0
    quoted_walked = 0
    processed = 0
    for i, s in enumerate(pending, 1):
        if limit is not None and processed >= limit:
            break
        # try/finally so the per-iteration atomic flush runs whether the
        # body completes normally, hits a `continue` shortcut in the
        # quoted-post block, or raises. fetched_at is intentionally NOT
        # updated here — only the final post-loop write stamps it.
        try:
            uri = s["uri"]
            author_did = s["author"]["did"]
            print(f"  [{i}/{len(pending)}] {uri[:80]}", file=sys.stderr)
            thread, error = fetch_thread(uri, appview=appview, user_agent=user_agent)
            s["thread_fetched_at"] = _now_iso()
            if thread is not None:
                replies = collect_same_author_replies(thread, author_did)
                s["thread_replies"] = replies
                s["thread_schema_version"] = THREAD_SCHEMA_VERSION
                s.pop("thread_fetch_error", None)
                success += 1
                if replies:
                    found_any += 1
                print(f"    ok ({len(replies)} self-replies)", file=sys.stderr)
            else:
                s["thread_fetch_error"] = error
                s.pop("thread_replies", None)
                failed += 1
                print(f"    FAIL: {error}", file=sys.stderr)
            # Count the save as processed once its primary thread fetch is
            # done, regardless of success/failure. Doing it here (inside the
            # try, before the quoted-post block) means `continue` shortcuts
            # in the quoted-post handling don't bypass the counter.
            processed += 1
            time.sleep(RATE_LIMIT_SEC)

            quoted = s.get("quoted_post") or {}
            if not isinstance(quoted, dict):
                continue
            if quoted.get("unavailable"):
                continue
            quoted_uri = quoted.get("uri")
            quoted_did = (quoted.get("author") or {}).get("did")
            if not quoted_uri or not quoted_did:
                continue
            print(f"    quoted-post thread: {quoted_uri[:80]}", file=sys.stderr)
            qthread, qerror = fetch_thread(quoted_uri, appview=appview, user_agent=user_agent)
            if qthread is not None:
                qreplies = collect_same_author_replies(qthread, quoted_did)
                quoted["thread_replies"] = qreplies
                quoted.pop("thread_fetch_error", None)
                quoted_walked += 1
                print(f"      ok ({len(qreplies)} self-replies)", file=sys.stderr)
            else:
                quoted["thread_fetch_error"] = qerror
                print(f"      FAIL: {qerror}", file=sys.stderr)
            time.sleep(RATE_LIMIT_SEC)
        finally:
            _atomic_write_inventory(inventory_path, inv)

    remaining = len(pending) - processed
    if remaining == 0:
        # Pending exhausted in this call — stamp fetched_at to mark the
        # complete pass. Calls that returned because they hit `limit`
        # leave fetched_at alone, preserving the
        # "fetched_at = a complete pass finished" CLI invariant.
        inv["fetched_at"] = _now_iso()
    _atomic_write_inventory(inventory_path, inv)

    print(
        f"bsky-saves: {success} hydrated ({found_any} had self-replies, "
        f"{quoted_walked} quoted-post threads also walked), {failed} failed"
        + (f", {remaining} pending" if remaining else ""),
        file=sys.stderr,
    )
    return success, failed, remaining
