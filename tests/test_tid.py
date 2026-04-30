"""Tests for tid.decode_tid_to_iso / rkey_of."""
from __future__ import annotations

import pytest

from bsky_saves.tid import decode_tid_micros, decode_tid_to_iso, rkey_of


def test_rkey_of_extracts_last_segment():
    assert rkey_of("at://did:plc:abc/app.bsky.feed.post/3lyq4m4yykc2u") == "3lyq4m4yykc2u"


def test_decode_tid_invalid_length_raises():
    with pytest.raises(ValueError):
        decode_tid_micros("short")


def test_decode_tid_invalid_char_raises():
    # '1' is not in the alphabet (alphabet starts at '2').
    with pytest.raises(ValueError):
        decode_tid_micros("1bcdefghijklm")


def test_decode_tid_to_iso_format():
    """Smoke test: a real BlueSky rkey decodes to a ISO-8601 UTC string."""
    iso = decode_tid_to_iso("3lyq4m4yykc2u")
    assert iso.endswith("Z")
    assert "T" in iso
    assert iso.startswith("20")
