"""Static-file serving for `bsky-saves serve --gui`.

All static-file logic lives here so `serve.py` stays focused on the JSON API.
The dispatcher in `serve.py` calls `serve_static_or_spa` (added in Task 6) as
one branch off the existing route table.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlsplit


class GuiNotInstalledError(Exception):
    """The bundled GUI tarball is missing or empty.

    Raised when `bsky-saves serve --gui` is requested but `src/bsky_saves/_gui/`
    either does not exist or lacks an `index.html`. The caller (run_serve)
    should print an actionable error and exit with code 2.
    """


def _gui_root_path() -> Path:
    """Return the absolute path to the package-bundled _gui/ directory.

    Wrapped in a thin function so tests can monkeypatch it.
    """
    return Path(__file__).resolve().parent / "_gui"


def resolve_gui_root() -> Path:
    """Return the populated _gui/ directory, or raise GuiNotInstalledError."""
    root = _gui_root_path()
    if not root.exists():
        raise GuiNotInstalledError(f"{root} is missing or empty")
    if not (root / "index.html").is_file():
        raise GuiNotInstalledError(f"{root}/index.html not found")
    return root


_CSP = (
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

# Documented API paths from serve.py's ROUTES table. Listed here for SPA
# fallback decisions: if a GET request matches one of these and isn't a real
# file in _gui/, defer to the caller's 404 path rather than serving index.html.
# This list MUST be kept in sync with serve.py's ROUTES.
_API_PATHS = frozenset({
    "/ping",
    "/fetch-image",
    "/extract-article",
    "/fetch",
    "/enrich",
    "/hydrate-threads",
})

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/vnd.microsoft.icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
}


def content_type_for(path: Path) -> str:
    """Best-effort Content-Type for a file path; falls back to octet-stream."""
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _cache_control_for(rel_path: str, *, is_spa_fallback: bool) -> str:
    """Return the Cache-Control value for a served file.

    - SPA fallback or index.html: no-store (must always revalidate).
    - /assets/*: immutable (Vite-hashed filenames).
    - Everything else: no-cache (revalidate per request).
    """
    if is_spa_fallback or rel_path == "index.html":
        return "no-store"
    if rel_path.startswith("assets/"):
        return "public, max-age=31536000, immutable"
    return "no-cache"


def serve_static_or_spa(handler, request_path: str, gui_root: Path) -> bool:
    """Try to serve a static file from gui_root for the given request path.

    Returns True if a response was sent (200 with file bytes, or 404). Returns
    False to defer to the caller (used in Task 7 for API-prefix paths that
    should yield a JSON 404 rather than an SPA fallback).

    Args:
        handler: A BaseHTTPRequestHandler-like object with send_response,
            send_header, end_headers, and a `wfile` attribute supporting write.
        request_path: The request URL path (may contain a query string).
        gui_root: The resolved _gui/ directory.
    """
    # Strip query string; URL-decode.
    parsed_path = urlsplit(request_path).path
    decoded = unquote(parsed_path)

    # Resolve candidate path. Root → index.html.
    rel = decoded.lstrip("/")
    if rel == "":
        rel = "index.html"

    candidate = (gui_root / rel).resolve()
    gui_root_resolved = gui_root.resolve()
    # Path-traversal defence: candidate must be under gui_root (or be gui_root itself).
    if not candidate.is_relative_to(gui_root_resolved):
        _send_404(handler)
        return True

    if candidate.is_file():
        _send_file(handler, candidate, rel_path=rel, is_spa_fallback=False)
        return True

    # File doesn't exist. Two cases:
    # 1. Documented API path: defer (False) — caller sends JSON 404.
    # 2. SPA route: serve index.html (200) so the GUI's router takes over.
    if decoded in _API_PATHS:
        return False

    index = gui_root / "index.html"
    _send_file(handler, index, rel_path="index.html", is_spa_fallback=True)
    return True


def _send_file(
    handler,
    path: Path,
    *,
    rel_path: str,
    is_spa_fallback: bool = False,
) -> None:
    body = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type_for(path))
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Cache-Control", _cache_control_for(rel_path, is_spa_fallback=is_spa_fallback))
    handler.send_header("Content-Security-Policy", _CSP)
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Cross-Origin-Opener-Policy", "same-origin")
    handler.end_headers()
    if getattr(handler, "command", "GET") != "HEAD":
        handler.wfile.write(body)


def _send_404(handler) -> None:
    handler.send_response(404)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", "9")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    if getattr(handler, "command", "GET") != "HEAD":
        handler.wfile.write(b"not found")
