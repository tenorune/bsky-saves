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


def test_serve_static_or_spa_returns_false_for_api_path(tmp_path):
    """A path matching a documented API route defers (False) when no static
    file with that name exists. Caller sends JSON 404 via existing path."""
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    result = serve_static_or_spa(h, "/fetch", gui)
    assert result is False
    assert h.status is None  # no response sent


def test_serve_static_or_spa_spa_fallback_for_non_existent_route(tmp_path):
    """A path that doesn't exist and isn't an API prefix gets index.html (SPA)."""
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    result = serve_static_or_spa(h, "/some/spa/route", gui)
    assert result is True
    assert h.status == 200
    assert h.body == b"<html>root</html>"


def test_serve_static_or_spa_defers_all_documented_api_paths(tmp_path):
    """Each documented API path should defer (return False) when no static
    file with that exact name exists."""
    gui = _populate_gui_root(tmp_path)
    for api_path in [
        "/ping",
        "/fetch-image",
        "/extract-article",
        "/fetch",
        "/enrich",
        "/hydrate-threads",
    ]:
        h = _StubHandler()
        result = serve_static_or_spa(h, api_path, gui)
        assert result is False, f"expected deferral for {api_path}"
        assert h.status is None, f"expected no response for {api_path}"


# Expected CSP value (from MVP spec §4.6). Pin exactly.
_EXPECTED_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'wasm-unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self'; "
    "connect-src 'self' https: http://127.0.0.1:* http://localhost:*; "
    "worker-src 'self' blob:; "
    "manifest-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def test_serve_static_or_spa_assets_get_immutable_cache(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/assets/main-abc123.js", gui)
    assert h.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_serve_static_or_spa_index_gets_no_store(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/", gui)
    assert h.headers["Cache-Control"] == "no-store"


def test_serve_static_or_spa_spa_fallback_gets_no_store(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/some/spa/route", gui)
    assert h.headers["Cache-Control"] == "no-store"


def test_serve_static_or_spa_other_static_gets_no_cache(tmp_path):
    gui = _populate_gui_root(tmp_path)
    (gui / "manifest.webmanifest").write_bytes(b'{"name":"x"}')
    h = _StubHandler()
    serve_static_or_spa(h, "/manifest.webmanifest", gui)
    assert h.headers["Cache-Control"] == "no-cache"


def test_serve_static_or_spa_sends_csp(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/", gui)
    assert h.headers["Content-Security-Policy"] == _EXPECTED_CSP


def test_serve_static_or_spa_sends_x_frame_options(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/", gui)
    assert h.headers["X-Frame-Options"] == "DENY"


def test_serve_static_or_spa_sends_referrer_policy(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/", gui)
    assert h.headers["Referrer-Policy"] == "no-referrer"


def test_serve_static_or_spa_sends_coop(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/", gui)
    assert h.headers["Cross-Origin-Opener-Policy"] == "same-origin"


def test_serve_static_or_spa_head_sends_headers_no_body(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    h.command = "HEAD"
    serve_static_or_spa(h, "/assets/main-abc123.js", gui)
    assert h.status == 200
    assert h.headers["Content-Type"] == "application/javascript"
    assert h.headers["Content-Length"] == str(len(b"console.log(1);"))
    assert h.body == b""  # HEAD writes no body


def test_serve_static_or_spa_sends_nosniff(tmp_path):
    """X-Content-Type-Options: nosniff applies uniformly to API AND static
    responses (spec §5.6)."""
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/", gui)
    assert h.headers["X-Content-Type-Options"] == "nosniff"


def test_serve_static_or_spa_assets_send_nosniff(tmp_path):
    """nosniff also applies to /assets/* responses."""
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/assets/main-abc123.js", gui)
    assert h.headers["X-Content-Type-Options"] == "nosniff"
