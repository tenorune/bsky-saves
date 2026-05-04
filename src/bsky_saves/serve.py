"""bsky-saves serve — local HTTP helper daemon for bsky-saves-gui.

A 127.0.0.1-bound HTTP server that exposes a small set of endpoints the
browser-side bsky-saves-gui app can't reach directly because of CORS:
fetching image bytes from cdn.bsky.app and extracting article text from
arbitrary URLs.

Spec: docs/superpowers/specs/2026-05-04-bsky-saves-v0.3-serve-subcommand.md
External HTTP API contract:
https://github.com/tenorune/bsky-saves-gui/blob/main/docs/bsky-saves-serve-requirements.md
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlparse

import httpx

from . import __version__
from .images import DEFAULT_USER_AGENT as _IMAGE_USER_AGENT
from .images import TIMEOUT as _IMAGE_TIMEOUT


def _handle_ping(handler) -> None:
    handler._send_json(
        200,
        {
            "name": "bsky-saves",
            "version": __version__,
            "features": ["fetch-image", "extract-article"],
        },
    )


def _is_allowed_image_url(url: str) -> bool:
    """Hardcoded allowlist for /fetch-image: https + cdn.bsky.app or *.bsky.app."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = parsed.hostname or ""
    return host == "cdn.bsky.app" or host.endswith(".bsky.app")


def _handle_fetch_image(handler) -> None:
    body = handler._read_json_body()
    url = (body or {}).get("url")
    if not isinstance(url, str) or not url:
        handler._send_json_error(400, "missing url")
        return
    if not _is_allowed_image_url(url):
        handler._send_json_error(400, "url not allowed")
        return
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": _IMAGE_USER_AGENT, "Accept": "image/*"},
            follow_redirects=True,
            timeout=_IMAGE_TIMEOUT,
        )
    except Exception as e:
        handler._send_json_error(502, f"{type(e).__name__}: {str(e)[:200]}")
        return
    if r.status_code >= 400:
        handler._send_json_error(r.status_code, f"upstream {r.status_code}")
        return
    content_type = r.headers.get("Content-Type", "application/octet-stream")
    handler._send_bytes(r.status_code, content_type, r.content)


ROUTES: dict[tuple[str, str], Callable[["_HandlerLike"], None]] = {
    ("GET", "/ping"): _handle_ping,
    ("POST", "/fetch-image"): _handle_fetch_image,
}


class _HandlerLike:
    """Type stub for request handlers. The real type is the class returned by
    make_handler; this exists only to give the ROUTES table a clean Callable
    annotation without triggering a forward-reference dance."""


def make_handler(
    *,
    allow_origins: list[str],
    verbose: bool = False,
) -> type[BaseHTTPRequestHandler]:
    """Build a BaseHTTPRequestHandler subclass that closes over allow_origins
    and verbose. Returning a class (not an instance) is what BaseHTTPRequestHandler
    expects from ThreadingHTTPServer."""

    origins = list(allow_origins)

    class Handler(BaseHTTPRequestHandler):
        # Suppress the default "127.0.0.1 - - [...] GET /ping" log line; we
        # emit our own (or none) via _log_request based on the verbose flag.
        def log_message(self, format, *args):
            return

        def _log_request(self) -> None:
            if verbose:
                print(
                    f"bsky-saves: {self.command} {self.path}",
                    file=sys.stderr,
                )

        def _cors_headers(self) -> None:
            origin = self.headers.get("Origin", "")
            if origin and origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_json_error(self, code: int, error: str) -> None:
            self._send_json(code, {"error": error})

        def _send_bytes(
            self,
            code: int,
            content_type: str,
            body: bytes,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                return None
            if length <= 0:
                return None
            try:
                raw = self.rfile.read(length)
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None
            return parsed if isinstance(parsed, dict) else None

        def do_OPTIONS(self) -> None:
            self._log_request()
            self.send_response(204)
            self._cors_headers()
            self.end_headers()

        def do_GET(self) -> None:
            self._log_request()
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._log_request()
            self._dispatch("POST")

        def __getattr__(self, name: str):
            # BaseHTTPRequestHandler calls self.do_<METHOD>() and raises 501
            # if the method is not found. Override __getattr__ so that any
            # unrecognised do_* verb falls through to _dispatch (which returns
            # 404 for every path not in ROUTES).
            if name.startswith("do_"):
                method = name[3:]

                def _unknown_verb():
                    self._log_request()
                    self._dispatch(method)

                return _unknown_verb
            raise AttributeError(name)

        def _dispatch(self, method: str) -> None:
            handler = ROUTES.get((method, self.path))
            if handler is None:
                self._send_json_error(404, "not found")
                return
            handler(self)

    return Handler


def run_serve(
    *,
    port: int = 47826,
    allow_origins: list[str] | None = None,
    verbose: bool = False,
) -> int:
    """Start the daemon. Blocks until Ctrl-C. Returns an exit code."""
    origins = list(allow_origins or ["https://saves.lightseed.net"])
    handler_cls = make_handler(allow_origins=origins, verbose=verbose)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    except OSError as e:
        print(f"bsky-saves: failed to bind 127.0.0.1:{port}: {e}", file=sys.stderr)
        return 2
    print(
        f"bsky-saves serve listening on http://127.0.0.1:{port} "
        f"(origins: {', '.join(origins)})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0
