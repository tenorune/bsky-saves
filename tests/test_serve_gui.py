"""Unit tests for bsky_saves._gui_serve."""
from __future__ import annotations

from pathlib import Path

import pytest

from bsky_saves._gui_serve import (
    GuiNotInstalledError,
    content_type_for,
    resolve_gui_root,
)


def test_resolve_gui_root_returns_path_when_index_present(tmp_path, monkeypatch):
    gui_dir = tmp_path / "_gui"
    gui_dir.mkdir()
    (gui_dir / "index.html").write_text("<html>x</html>")

    # Monkeypatch the package-relative path resolution.
    monkeypatch.setattr("bsky_saves._gui_serve._gui_root_path", lambda: gui_dir)
    assert resolve_gui_root() == gui_dir


def test_resolve_gui_root_raises_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("bsky_saves._gui_serve._gui_root_path", lambda: tmp_path / "nope")
    with pytest.raises(GuiNotInstalledError):
        resolve_gui_root()


def test_resolve_gui_root_raises_when_index_missing(tmp_path, monkeypatch):
    gui_dir = tmp_path / "_gui"
    gui_dir.mkdir()
    # No index.html.
    monkeypatch.setattr("bsky_saves._gui_serve._gui_root_path", lambda: gui_dir)
    with pytest.raises(GuiNotInstalledError):
        resolve_gui_root()


def test_content_type_html():
    assert content_type_for(Path("index.html")) == "text/html; charset=utf-8"


def test_content_type_javascript():
    assert content_type_for(Path("assets/x.js")) == "application/javascript"


def test_content_type_css():
    assert content_type_for(Path("assets/x.css")) == "text/css; charset=utf-8"


def test_content_type_webmanifest():
    assert content_type_for(Path("manifest.webmanifest")) == "application/manifest+json"


def test_content_type_png():
    assert content_type_for(Path("icons/x.png")) == "image/png"


def test_content_type_svg():
    assert content_type_for(Path("icons/x.svg")) == "image/svg+xml"


def test_content_type_ico():
    assert content_type_for(Path("favicon.ico")) == "image/vnd.microsoft.icon"


def test_content_type_unknown_returns_octet_stream():
    assert content_type_for(Path("mystery.xyz")) == "application/octet-stream"
