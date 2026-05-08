"""Tests for bsky_saves.threads.collect_same_author_replies.

The function walks a thread tree and returns posts authored by the
bookmarked-post's author that form an unbroken same-author chain from
the root. Posts authored by the same author but reachable only through
a different-author parent (e.g., the OP replying to someone else's
comment) are NOT included — they're "comment responses," not
"self-thread continuation."
"""
from __future__ import annotations

from bsky_saves.threads import collect_same_author_replies


# DID shorthands used throughout these tests.
OP = "did:plc:op"
OTHER = "did:plc:other"


def _post(uri: str, did: str, text: str = "") -> dict:
    """Build a thread-view post node matching the BlueSky AppView shape."""
    return {
        "uri": uri,
        "author": {"did": did, "handle": "x.bsky.social"},
        "indexedAt": "2026-05-06T00:00:00Z",
        "record": {"text": text},
        "embed": {},
    }


def _node(post: dict, replies: list[dict] | None = None) -> dict:
    """Build a thread-view node ({post, replies}). Replies is a list of
    nested nodes (each itself has a `post` and optional `replies`)."""
    return {"post": post, "replies": replies or []}


def test_self_continuation_chain_is_collected():
    """Mary makes a 3-post self-thread; all three children are collected."""
    root = _node(
        _post("at://op/1", OP, "root"),
        [
            _node(_post("at://op/2", OP, "continuation 1")),
            _node(_post("at://op/3", OP, "continuation 2")),
        ],
    )
    out = collect_same_author_replies(root, OP)
    uris = [r["uri"] for r in out]
    assert uris == ["at://op/2", "at://op/3"]


def test_op_reply_to_other_comment_is_NOT_collected():
    """Other person comments on root; OP replies to that comment.
    The OP's reply is NOT part of the self-thread."""
    root = _node(
        _post("at://op/1", OP, "root"),
        [
            _node(
                _post("at://other/1", OTHER, "Beautiful!"),
                [
                    _node(_post("at://op/r1", OP, "Thank you!")),
                ],
            ),
        ],
    )
    out = collect_same_author_replies(root, OP)
    assert out == []


def test_chain_breaks_at_other_author_then_does_not_collect_below():
    """If the chain goes OP -> OTHER -> OP -> OP, only collect along the
    unbroken-from-root same-author chain. The OPs below OTHER are not
    self-thread continuations."""
    root = _node(
        _post("at://op/1", OP, "root"),
        [
            _node(
                _post("at://other/1", OTHER, "comment"),
                [
                    _node(
                        _post("at://op/below_other_1", OP, "Thank you!"),
                        [
                            _node(_post("at://op/below_other_2", OP, "and also...")),
                        ],
                    ),
                ],
            ),
        ],
    )
    out = collect_same_author_replies(root, OP)
    assert out == []


def test_mixed_tree_collects_only_unbroken_chain():
    """Realistic mixed shape: some self-continuations, lots of comment-responses."""
    root = _node(
        _post("at://op/1", OP, "photo"),
        [
            # Self-continuation #1 — collected.
            _node(
                _post("at://op/cont1", OP, "More context for the photo"),
                [
                    # OP continues continuing — collected.
                    _node(_post("at://op/cont2", OP, "Even more context")),
                ],
            ),
            # Person A comments — not collected.
            _node(
                _post("at://other/a", OTHER, "Beautiful!"),
                [
                    # OP replies to comment — NOT collected.
                    _node(_post("at://op/thx_a", OP, "Thank you A!")),
                ],
            ),
            # Person B comments — not collected.
            _node(
                _post("at://other/b", OTHER, "Wow"),
                [
                    _node(_post("at://op/thx_b", OP, "Thank you B!")),
                ],
            ),
        ],
    )
    out = collect_same_author_replies(root, OP)
    uris = sorted(r["uri"] for r in out)
    assert uris == ["at://op/cont1", "at://op/cont2"]


def test_dedup_via_seen_uris():
    """A duplicate same-author URI in the tree is not collected twice."""
    dup = _post("at://op/dup", OP, "dup")
    root = _node(
        _post("at://op/1", OP, "root"),
        [
            _node(dup),
            _node(dup),  # same URI repeated
        ],
    )
    out = collect_same_author_replies(root, OP)
    uris = [r["uri"] for r in out]
    assert uris == ["at://op/dup"]


def test_extracts_images_from_collected_post():
    """Images on a collected post are extracted via extract_media."""
    post_with_image = {
        "uri": "at://op/2",
        "author": {"did": OP, "handle": "x"},
        "indexedAt": "2026-05-06T00:00:00Z",
        "record": {"text": "with image"},
        "embed": {
            "$type": "app.bsky.embed.images#view",
            "images": [
                {
                    "fullsize": "https://cdn.bsky.app/img/x.jpg",
                    "thumb": "https://cdn.bsky.app/img/x_thumb.jpg",
                    "alt": "alt text",
                }
            ],
        },
    }
    root = _node(_post("at://op/1", OP, "root"), [_node(post_with_image)])
    out = collect_same_author_replies(root, OP)
    assert len(out) == 1
    assert out[0]["images"] == [
        {
            "kind": "image",
            "url": "https://cdn.bsky.app/img/x.jpg",
            "thumb": "https://cdn.bsky.app/img/x_thumb.jpg",
            "alt": "alt text",
        }
    ]


def test_empty_thread_returns_empty_list():
    root = _node(_post("at://op/1", OP), [])
    assert collect_same_author_replies(root, OP) == []


def test_only_other_authors_returns_empty():
    root = _node(
        _post("at://op/1", OP),
        [
            _node(_post("at://other/1", OTHER)),
            _node(_post("at://other/2", OTHER)),
        ],
    )
    assert collect_same_author_replies(root, OP) == []


# --- v0.4.2: incremental atomic writes during hydrate_threads ---

import json
import os
from pathlib import Path

import pytest

from bsky_saves import threads as _threads_mod
from bsky_saves.threads import hydrate_threads, THREAD_SCHEMA_VERSION


def _make_inventory(*entries: dict) -> dict:
    """Minimal inventory wrapping for hydrate_threads tests."""
    return {"fetched_at": "2026-04-12T00:00:00Z", "saves": list(entries)}


def _make_pending_save(uri: str, did: str = OP) -> dict:
    """A save that hydrate_threads's pending filter will pick up (no
    thread_replies / no thread_fetch_error)."""
    return {
        "uri": uri,
        "author": {"did": did, "handle": "x.bsky.social"},
        "saved_at": "2026-04-12T00:00:00Z",
    }


def _silence_rate_limit(monkeypatch):
    """time.sleep dominates loop runtime; make it a no-op for tests."""
    monkeypatch.setattr(_threads_mod.time, "sleep", lambda *a, **kw: None)


def test_per_iteration_flush_writes_inventory_after_each_save(
    tmp_path, monkeypatch
):
    """Each save is persisted to disk before the next iteration starts."""
    _silence_rate_limit(monkeypatch)

    inv = _make_inventory(
        _make_pending_save("at://x/1"),
        _make_pending_save("at://x/2"),
        _make_pending_save("at://x/3"),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    snapshots: list[list[str]] = []

    def fake_fetch(uri, **kwargs):
        # On each call, snapshot which URIs already have thread_replies on disk.
        on_disk = json.loads(inv_path.read_text(encoding="utf-8"))
        completed = [
            s["uri"] for s in on_disk["saves"] if "thread_replies" in s
        ]
        snapshots.append(completed)
        return {
            "post": {
                "uri": uri,
                "author": {"did": OP, "handle": "x"},
                "indexedAt": "2026-04-12T00:00:00Z",
                "record": {"text": ""},
                "embed": {},
            },
            "replies": [],
        }, None

    monkeypatch.setattr(_threads_mod, "fetch_thread", fake_fetch)

    success, failed, remaining = hydrate_threads(inv_path)
    assert (success, failed, remaining) == (3, 0, 0)

    # When call N starts, calls 1..N-1 should already be on disk.
    assert snapshots == [
        [],                                      # before save 1
        ["at://x/1"],                            # before save 2
        ["at://x/1", "at://x/2"],                # before save 3
    ]


def test_crash_partway_preserves_completed_saves(tmp_path, monkeypatch):
    """If fetch_thread raises mid-loop, completed saves are durably persisted."""
    _silence_rate_limit(monkeypatch)

    inv = _make_inventory(
        _make_pending_save("at://x/1"),
        _make_pending_save("at://x/2"),  # crash here
        _make_pending_save("at://x/3"),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    call_count = {"n": 0}

    def crashing_fetch(uri, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated mid-loop crash")
        return {
            "post": {
                "uri": uri,
                "author": {"did": OP, "handle": "x"},
                "indexedAt": "2026-04-12T00:00:00Z",
                "record": {"text": ""},
                "embed": {},
            },
            "replies": [],
        }, None

    monkeypatch.setattr(_threads_mod, "fetch_thread", crashing_fetch)

    with pytest.raises(RuntimeError, match="simulated mid-loop crash"):
        hydrate_threads(inv_path)

    # On disk: save 1 fully hydrated, save 2 untouched (crash before any
    # mutation in the body), save 3 untouched.
    on_disk = json.loads(inv_path.read_text(encoding="utf-8"))
    by_uri = {s["uri"]: s for s in on_disk["saves"]}

    assert "thread_replies" in by_uri["at://x/1"]
    assert by_uri["at://x/1"]["thread_schema_version"] == THREAD_SCHEMA_VERSION

    # Save 2 had no mutations — fetch_thread raised before s["thread_fetched_at"]
    # was assigned. The try/finally flushes whatever state exists.
    assert "thread_fetched_at" not in by_uri["at://x/2"]
    assert "thread_replies" not in by_uri["at://x/2"]

    # Save 3 was never touched (loop exited via the exception).
    assert "thread_fetched_at" not in by_uri["at://x/3"]
    assert "thread_replies" not in by_uri["at://x/3"]


def test_fetched_at_only_updated_on_final_write(tmp_path, monkeypatch):
    """Per-iteration flushes do NOT touch fetched_at; only the post-loop write."""
    _silence_rate_limit(monkeypatch)

    original_fetched_at = "2026-04-12T00:00:00Z"
    inv = {
        "fetched_at": original_fetched_at,
        "saves": [
            _make_pending_save("at://x/1"),
            _make_pending_save("at://x/2"),
        ],
    }
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    seen_fetched_at_during_loop: list[str] = []

    def fake_fetch(uri, **kwargs):
        # Snapshot fetched_at as the loop sees it on disk.
        on_disk = json.loads(inv_path.read_text(encoding="utf-8"))
        seen_fetched_at_during_loop.append(on_disk["fetched_at"])
        return {
            "post": {
                "uri": uri,
                "author": {"did": OP, "handle": "x"},
                "indexedAt": "2026-04-12T00:00:00Z",
                "record": {"text": ""},
                "embed": {},
            },
            "replies": [],
        }, None

    monkeypatch.setattr(_threads_mod, "fetch_thread", fake_fetch)
    monkeypatch.setattr(
        _threads_mod, "_now_iso", lambda: "2026-05-07T12:00:00Z"
    )

    hydrate_threads(inv_path)

    # Each in-loop snapshot must show the ORIGINAL fetched_at; the iteration
    # flushes do NOT touch it.
    # (Save 1's snapshot is taken before any iteration write completes; saves
    # 2's snapshot is after save 1's iteration flush — must still be original.)
    assert seen_fetched_at_during_loop == [
        original_fetched_at,
        original_fetched_at,
    ]

    # Final post-loop write DOES update fetched_at.
    on_disk = json.loads(inv_path.read_text(encoding="utf-8"))
    assert on_disk["fetched_at"] == "2026-05-07T12:00:00Z"


def test_atomic_write_uses_os_replace(tmp_path, monkeypatch):
    """Each per-iteration flush + the final flush must go through os.replace
    (not a direct write_text overwrite)."""
    _silence_rate_limit(monkeypatch)

    inv = _make_inventory(
        _make_pending_save("at://x/1"),
        _make_pending_save("at://x/2"),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    def fake_fetch(uri, **kwargs):
        return {
            "post": {
                "uri": uri,
                "author": {"did": OP, "handle": "x"},
                "indexedAt": "2026-04-12T00:00:00Z",
                "record": {"text": ""},
                "embed": {},
            },
            "replies": [],
        }, None

    monkeypatch.setattr(_threads_mod, "fetch_thread", fake_fetch)

    rename_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        rename_calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr("bsky_saves.threads.os.replace", spy_replace)

    hydrate_threads(inv_path)

    # 2 pending saves → 2 per-iteration flushes + 1 final flush = 3 os.replace calls.
    assert len(rename_calls) == 3
    for src, dst in rename_calls:
        assert dst == str(inv_path)
        assert src.endswith(".tmp")


def test_resume_after_partial_progress(tmp_path, monkeypatch):
    """A subsequent hydrate_threads run picks up where a crashed run stopped.
    Saves that completed in the first run are skipped; the rest are retried."""
    _silence_rate_limit(monkeypatch)

    inv = _make_inventory(
        _make_pending_save("at://x/1"),
        _make_pending_save("at://x/2"),
        _make_pending_save("at://x/3"),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    # First run: crash on save 2. Save 1 persists; save 2 and 3 untouched.
    call_count = {"n": 0}

    def crashing_fetch(uri, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("crash")
        return {
            "post": {
                "uri": uri,
                "author": {"did": OP, "handle": "x"},
                "indexedAt": "2026-04-12T00:00:00Z",
                "record": {"text": ""},
                "embed": {},
            },
            "replies": [],
        }, None

    monkeypatch.setattr(_threads_mod, "fetch_thread", crashing_fetch)

    with pytest.raises(RuntimeError):
        hydrate_threads(inv_path)

    # Second run: replace fetch with a clean version, run again.
    visited: list[str] = []

    def clean_fetch(uri, **kwargs):
        visited.append(uri)
        return {
            "post": {
                "uri": uri,
                "author": {"did": OP, "handle": "x"},
                "indexedAt": "2026-04-12T00:00:00Z",
                "record": {"text": ""},
                "embed": {},
            },
            "replies": [],
        }, None

    monkeypatch.setattr(_threads_mod, "fetch_thread", clean_fetch)

    success, failed, remaining = hydrate_threads(inv_path)

    # Save 1 was already done — skipped. Saves 2 and 3 retried.
    assert (success, failed, remaining) == (2, 0, 0)
    assert visited == ["at://x/2", "at://x/3"]


# --- v0.4.3: caller-driven batching via the `limit` kwarg ---


def test_limit_one_processes_exactly_one_per_call(tmp_path, monkeypatch):
    """3 pending saves, three sequential calls with limit=1; each call
    processes exactly one. fetched_at only stamped on the final call."""
    _silence_rate_limit(monkeypatch)

    inv = _make_inventory(
        _make_pending_save("at://x/1"),
        _make_pending_save("at://x/2"),
        _make_pending_save("at://x/3"),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")
    original_fetched_at = inv["fetched_at"]

    visited: list[str] = []

    def fake_fetch(uri, **kwargs):
        visited.append(uri)
        return {
            "post": {
                "uri": uri,
                "author": {"did": OP, "handle": "x"},
                "indexedAt": "2026-04-12T00:00:00Z",
                "record": {"text": ""},
                "embed": {},
            },
            "replies": [],
        }, None

    monkeypatch.setattr(_threads_mod, "fetch_thread", fake_fetch)
    monkeypatch.setattr(
        _threads_mod, "_now_iso", lambda: "2026-05-08T12:00:00Z"
    )

    # Call 1: process at://x/1; remaining 2.
    s1, f1, r1 = hydrate_threads(inv_path, limit=1)
    assert (s1, f1, r1) == (1, 0, 2)
    assert visited == ["at://x/1"]
    on_disk = json.loads(inv_path.read_text(encoding="utf-8"))
    assert on_disk["fetched_at"] == original_fetched_at  # NOT stamped

    # Call 2: process at://x/2; remaining 1.
    s2, f2, r2 = hydrate_threads(inv_path, limit=1)
    assert (s2, f2, r2) == (1, 0, 1)
    assert visited == ["at://x/1", "at://x/2"]
    on_disk = json.loads(inv_path.read_text(encoding="utf-8"))
    assert on_disk["fetched_at"] == original_fetched_at  # NOT stamped yet

    # Call 3: process at://x/3; remaining 0; fetched_at stamped.
    s3, f3, r3 = hydrate_threads(inv_path, limit=1)
    assert (s3, f3, r3) == (1, 0, 0)
    assert visited == ["at://x/1", "at://x/2", "at://x/3"]
    on_disk = json.loads(inv_path.read_text(encoding="utf-8"))
    assert on_disk["fetched_at"] == "2026-05-08T12:00:00Z"  # stamped on final


def test_limit_zero_is_noop(tmp_path, monkeypatch):
    """limit=0 against non-empty pending → returns (0, 0, N), no fetches."""
    _silence_rate_limit(monkeypatch)

    inv = _make_inventory(
        _make_pending_save("at://x/1"),
        _make_pending_save("at://x/2"),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")
    original_fetched_at = inv["fetched_at"]

    fetch_calls = {"n": 0}

    def fake_fetch(uri, **kwargs):
        fetch_calls["n"] += 1
        return {}, None

    monkeypatch.setattr(_threads_mod, "fetch_thread", fake_fetch)

    success, failed, remaining = hydrate_threads(inv_path, limit=0)
    assert (success, failed, remaining) == (0, 0, 2)
    assert fetch_calls["n"] == 0

    # No-op MUST NOT touch fetched_at.
    on_disk = json.loads(inv_path.read_text(encoding="utf-8"))
    assert on_disk["fetched_at"] == original_fetched_at


def test_limit_zero_with_empty_pending_returns_zero_remaining(tmp_path):
    """limit=0 when nothing is pending also returns (0, 0, 0)."""
    inv = _make_inventory(
        # Already-hydrated save (excluded by the pending filter).
        {
            "uri": "at://x/done",
            "author": {"did": OP, "handle": "x"},
            "saved_at": "2026-04-12T00:00:00Z",
            "thread_replies": [],
            "thread_schema_version": THREAD_SCHEMA_VERSION,
            "thread_fetched_at": "2026-05-01T00:00:00Z",
        },
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    success, failed, remaining = hydrate_threads(inv_path, limit=0)
    assert (success, failed, remaining) == (0, 0, 0)


def test_limit_greater_than_pending_processes_all(tmp_path, monkeypatch):
    """limit > len(pending) is equivalent to limit=None — exhausts pending,
    stamps fetched_at, returns remaining=0."""
    _silence_rate_limit(monkeypatch)

    inv = _make_inventory(
        _make_pending_save("at://x/1"),
        _make_pending_save("at://x/2"),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    def fake_fetch(uri, **kwargs):
        return {
            "post": {
                "uri": uri,
                "author": {"did": OP, "handle": "x"},
                "indexedAt": "2026-04-12T00:00:00Z",
                "record": {"text": ""},
                "embed": {},
            },
            "replies": [],
        }, None

    monkeypatch.setattr(_threads_mod, "fetch_thread", fake_fetch)
    monkeypatch.setattr(
        _threads_mod, "_now_iso", lambda: "2026-05-08T12:00:00Z"
    )

    success, failed, remaining = hydrate_threads(inv_path, limit=100)
    assert (success, failed, remaining) == (2, 0, 0)
    on_disk = json.loads(inv_path.read_text(encoding="utf-8"))
    assert on_disk["fetched_at"] == "2026-05-08T12:00:00Z"  # stamped


def test_limit_counts_failed_items(tmp_path, monkeypatch):
    """All-failure run: failed items count toward limit so a caller's batch
    loop terminates rather than spinning forever on the same items."""
    _silence_rate_limit(monkeypatch)

    inv = _make_inventory(
        _make_pending_save("at://x/1"),
        _make_pending_save("at://x/2"),
        _make_pending_save("at://x/3"),
    )
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    def all_fail_fetch(uri, **kwargs):
        return None, "http_500"

    monkeypatch.setattr(_threads_mod, "fetch_thread", all_fail_fetch)

    success, failed, remaining = hydrate_threads(inv_path, limit=2)
    # 2 attempts, both failed; 1 still untouched in pending.
    assert (success, failed, remaining) == (0, 2, 1)

    # Subsequent call with limit=2: the 2 already-failed saves are now
    # excluded by the pending filter (thread_fetch_error set), so only
    # the 3rd save is pending.
    success, failed, remaining = hydrate_threads(inv_path, limit=2)
    assert (success, failed, remaining) == (0, 1, 0)


def test_limit_negative_raises_value_error(tmp_path):
    inv = _make_inventory(_make_pending_save("at://x/1"))
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    with pytest.raises(ValueError, match="non-negative int or None"):
        hydrate_threads(inv_path, limit=-1)


def test_limit_non_int_raises_value_error(tmp_path):
    inv = _make_inventory(_make_pending_save("at://x/1"))
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    with pytest.raises(ValueError, match="non-negative int or None"):
        hydrate_threads(inv_path, limit="3")  # type: ignore[arg-type]


def test_limit_bool_rejected(tmp_path):
    """bool is a subclass of int in Python; reject it explicitly so
    `limit=True` doesn't silently mean `limit=1`."""
    inv = _make_inventory(_make_pending_save("at://x/1"))
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv), encoding="utf-8")

    with pytest.raises(ValueError, match="non-negative int or None"):
        hydrate_threads(inv_path, limit=True)  # type: ignore[arg-type]


def test_batched_run_matches_single_call_inventory(tmp_path, monkeypatch):
    """The final inventory after N sequential limit=1 calls is identical to
    the inventory after a single limit=None call (modulo timestamps which
    we control)."""
    _silence_rate_limit(monkeypatch)

    def fake_fetch(uri, **kwargs):
        return {
            "post": {
                "uri": uri,
                "author": {"did": OP, "handle": "x"},
                "indexedAt": "2026-04-12T00:00:00Z",
                "record": {"text": ""},
                "embed": {},
            },
            "replies": [],
        }, None

    monkeypatch.setattr(_threads_mod, "fetch_thread", fake_fetch)
    monkeypatch.setattr(
        _threads_mod, "_now_iso", lambda: "2026-05-08T12:00:00Z"
    )

    # Single-call run.
    inv_a = _make_inventory(
        _make_pending_save("at://x/1"),
        _make_pending_save("at://x/2"),
        _make_pending_save("at://x/3"),
    )
    path_a = tmp_path / "single.json"
    path_a.write_text(json.dumps(inv_a), encoding="utf-8")
    hydrate_threads(path_a)
    single_run = json.loads(path_a.read_text(encoding="utf-8"))

    # Batched run, limit=1 three times.
    inv_b = _make_inventory(
        _make_pending_save("at://x/1"),
        _make_pending_save("at://x/2"),
        _make_pending_save("at://x/3"),
    )
    path_b = tmp_path / "batched.json"
    path_b.write_text(json.dumps(inv_b), encoding="utf-8")
    while True:
        _, _, remaining = hydrate_threads(path_b, limit=1)
        if remaining == 0:
            break
    batched_run = json.loads(path_b.read_text(encoding="utf-8"))

    # Per-save mutations must match exactly.
    saves_a = {s["uri"]: s for s in single_run["saves"]}
    saves_b = {s["uri"]: s for s in batched_run["saves"]}
    for uri in saves_a:
        assert saves_a[uri] == saves_b[uri], f"mismatch on {uri}"
    assert single_run["fetched_at"] == batched_run["fetched_at"]
