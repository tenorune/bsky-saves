"""Unit tests for bsky_saves._io.atomic_write_inventory."""
from __future__ import annotations

import json

from bsky_saves._io import atomic_write_inventory


def test_atomic_write_inventory_writes_expected_content(tmp_path):
    target = tmp_path / "inv.json"
    inventory = {"saves": [{"uri": "at://example", "saved_at": "2026-05-12T00:00:00Z"}]}

    atomic_write_inventory(target, inventory)

    written = target.read_text(encoding="utf-8")
    # Trailing newline is part of the contract.
    assert written.endswith("\n")
    assert json.loads(written) == inventory
    # JSON is formatted (indented), sort_keys=True.
    assert "  " in written  # indent
    # Keys are sorted: "saves" is the only top-level key, so check inside.
    save = json.loads(written)["saves"][0]
    keys = list(save.keys())
    assert keys == sorted(keys)


def test_atomic_write_inventory_leaves_no_tmp_sidecar(tmp_path):
    target = tmp_path / "inv.json"
    atomic_write_inventory(target, {"saves": []})

    sidecar = target.with_suffix(target.suffix + ".tmp")
    assert not sidecar.exists()


def test_atomic_write_inventory_overwrites_existing(tmp_path):
    target = tmp_path / "inv.json"
    target.write_text('{"saves": [{"uri": "old"}]}\n', encoding="utf-8")

    atomic_write_inventory(target, {"saves": [{"uri": "new"}]})

    assert json.loads(target.read_text(encoding="utf-8"))["saves"][0]["uri"] == "new"
