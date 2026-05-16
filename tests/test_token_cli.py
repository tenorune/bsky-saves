"""Tests for the `bsky-saves token` CLI subcommand."""
from __future__ import annotations

import re

from bsky_saves.cli import main


def test_token_prints_existing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    (tmp_path / "token").write_text("hardcoded-test-token\n", encoding="utf-8")
    rc = main(["token"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "hardcoded-test-token"


def test_token_lazy_generates_on_first_call(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    rc = main(["token"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", out), out
    assert (tmp_path / "token").exists()


def test_token_rotate_changes_value(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    rc1 = main(["token"])
    first = capsys.readouterr().out.strip()
    rc2 = main(["token", "--rotate"])
    second = capsys.readouterr().out.strip()
    assert rc1 == 0 and rc2 == 0
    assert first != second
    assert (tmp_path / "token").read_text(encoding="utf-8").strip() == second


def test_token_rotate_on_empty_state_generates(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    rc = main(["token", "--rotate"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", out), out
