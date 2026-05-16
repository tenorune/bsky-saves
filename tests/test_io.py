"""Unit tests for bsky_saves._io."""
from __future__ import annotations

import json
import re
import sys

from bsky_saves._io import atomic_write_inventory, config_dir, read_or_create_token


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


# ---------------------------------------------------------------------------
# config_dir()
# ---------------------------------------------------------------------------


def test_config_dir_linux_default(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_dir() == tmp_path / ".config" / "bsky-saves"


def test_config_dir_linux_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_dir() == tmp_path / "xdg" / "bsky-saves"


def test_config_dir_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_dir() == tmp_path / "Library" / "Application Support" / "bsky-saves"


def test_config_dir_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    assert config_dir() == tmp_path / "AppData" / "Roaming" / "bsky-saves"


# ---------------------------------------------------------------------------
# read_or_create_token()
# ---------------------------------------------------------------------------


def test_read_or_create_token_lazy_creates(monkeypatch, tmp_path):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    token = read_or_create_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token), token
    assert (tmp_path / "token").read_text(encoding="utf-8").strip() == token


def test_read_or_create_token_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    first = read_or_create_token()
    second = read_or_create_token()
    assert first == second


def test_read_or_create_token_file_perms(monkeypatch, tmp_path):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    read_or_create_token()
    perms = (tmp_path / "token").stat().st_mode & 0o777
    if sys.platform != "win32":
        assert perms == 0o600, oct(perms)


def test_read_or_create_token_strips_trailing_newline(monkeypatch, tmp_path):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    (tmp_path / "token").write_text("preexisting-token-value\n", encoding="utf-8")
    assert read_or_create_token() == "preexisting-token-value"


def test_read_or_create_token_multiline_returns_first_line(monkeypatch, tmp_path):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    (tmp_path / "token").write_text("first-line\nsecond-line\n", encoding="utf-8")
    assert read_or_create_token() == "first-line"


def test_read_or_create_token_regenerates_on_empty_file(monkeypatch, tmp_path):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    (tmp_path / "token").write_text("", encoding="utf-8")
    token = read_or_create_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token), token
    # File should now contain the freshly-generated token.
    assert (tmp_path / "token").read_text(encoding="utf-8").strip() == token


def test_config_dir_windows_no_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows Path.home() uses USERPROFILE
    assert config_dir() == tmp_path / "AppData" / "Roaming" / "bsky-saves"
