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
