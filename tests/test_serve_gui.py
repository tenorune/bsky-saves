"""Unit tests for bsky_saves._gui_serve."""
from __future__ import annotations

from pathlib import Path

import pytest

from bsky_saves._gui_serve import (
    GuiNotInstalledError,
    content_type_for,
    resolve_gui_root,
)


class _StubHandler:
    """Minimal handler stub for testing serve_static_or_spa.

    Captures send_response / send_header / wfile.write calls so tests can
    inspect what the function would have sent over the wire.
    """

    def __init__(self):
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body: bytes = b""
        self.command: str = "GET"  # default; tests can override
        self._json_errors: list[tuple[int, str]] = []

    def send_response(self, code: int) -> None:
        self.status = code

    def send_header(self, name: str, value: str) -> None:
        self.headers[name] = value

    def end_headers(self) -> None:
        pass

    @property
    def wfile(self):
        outer = self

        class _W:
            def write(self_inner, data: bytes):
                outer.body += data

        return _W()

    def _send_json_error(self, code: int, msg: str) -> None:
        self._json_errors.append((code, msg))
        self.status = code


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


from bsky_saves._gui_serve import serve_static_or_spa


def _populate_gui_root(tmp_path):
    gui = tmp_path / "_gui"
    gui.mkdir()
    (gui / "index.html").write_bytes(b"<html>root</html>")
    (gui / "assets").mkdir()
    (gui / "assets" / "main-abc123.js").write_bytes(b"console.log(1);")
    return gui


def test_serve_static_or_spa_serves_existing_file(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    result = serve_static_or_spa(h, "/assets/main-abc123.js", gui)
    assert result is True
    assert h.status == 200
    assert h.body == b"console.log(1);"
    assert h.headers["Content-Type"] == "application/javascript"


def test_serve_static_or_spa_serves_index_at_root(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    result = serve_static_or_spa(h, "/", gui)
    assert result is True
    assert h.status == 200
    assert h.body == b"<html>root</html>"


def test_serve_static_or_spa_rejects_path_traversal(tmp_path):
    gui = _populate_gui_root(tmp_path)
    (tmp_path / "secret.txt").write_bytes(b"SECRET")
    h = _StubHandler()
    result = serve_static_or_spa(h, "/../secret.txt", gui)
    assert result is True
    assert h.status == 404
    assert b"SECRET" not in h.body


def test_serve_static_or_spa_404_for_missing_file(tmp_path):
    """A path that doesn't exist returns 404. Task 7 changes this for
    non-API-prefix paths to serve index.html (SPA fallback) instead."""
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    result = serve_static_or_spa(h, "/missing.txt", gui)
    assert result is True
    assert h.status == 404
