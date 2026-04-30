"""Decode AT Protocol Time-based IDs (TIDs) to UTC timestamps.

A TID is a 13-character base32-sortable string. Decoded as a 64-bit
two's-complement integer it has this layout (per the AT Protocol spec):

  bit 63       = 0 (always)
  bits 62..10  = 53-bit microseconds since the Unix epoch
  bits 9..0    = 10-bit clock identifier

Encoded as 13 base32-sortable chars (5 bits each = 65 bits), the high bit
of the leading char is unused padding.
"""
from __future__ import annotations

from datetime import datetime, timezone

ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"
INDEX = {c: i for i, c in enumerate(ALPHABET)}


def decode_tid_micros(rkey: str) -> int:
    if len(rkey) != 13:
        raise ValueError(f"TID must be 13 chars, got {len(rkey)}: {rkey!r}")
    n = 0
    for c in rkey:
        if c not in INDEX:
            raise ValueError(f"invalid TID char: {c!r} in {rkey!r}")
        n = (n << 5) | INDEX[c]
    return (n >> 10) & ((1 << 53) - 1)


def decode_tid_to_iso(rkey: str) -> str:
    micros = decode_tid_micros(rkey)
    return datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rkey_of(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]
