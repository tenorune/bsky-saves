"""Static-file serving for `bsky-saves serve --gui`.

All static-file logic lives here so `serve.py` stays focused on the JSON API.
The dispatcher in `serve.py` calls `serve_static_or_spa` (added in Task 6) as
one branch off the existing route table.
"""
from __future__ import annotations

from pathlib import Path


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
