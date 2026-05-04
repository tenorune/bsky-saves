"""Integration tests for bsky_saves.serve.

Each test boots the server in a daemon thread on an ephemeral port via the
serve_in_background context manager, exercises endpoints with stdlib
urllib.request, and tears the server down at context exit.
"""
from __future__ import annotations

import contextlib
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import respx

from bsky_saves import serve


DEFAULT_ORIGIN = "https://saves.lightseed.net"


@contextlib.contextmanager
def serve_in_background(allow_origins=(DEFAULT_ORIGIN,), verbose=False):
    """Boot the daemon in a daemon thread on an ephemeral port; yield (port, server)."""
    handler_cls = serve.make_handler(
        allow_origins=list(allow_origins), verbose=verbose
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(port, path, *, method="GET", headers=None, body=None):
    """Stdlib urllib request helper. Returns (status, headers_dict, body_bytes)."""
    req_headers = dict(headers or {})
    if isinstance(body, dict):
        body = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        headers=req_headers,
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_unknown_path_returns_404():
    with serve_in_background() as (port, _):
        status, _, body = _request(port, "/admin")
    assert status == 404
    assert json.loads(body) == {"error": "not found"}


def test_unknown_method_returns_404():
    with serve_in_background() as (port, _):
        status, _, body = _request(port, "/ping", method="DELETE")
    assert status == 404
    assert json.loads(body) == {"error": "not found"}


def test_ping_returns_name_version_features():
    from bsky_saves import __version__
    with serve_in_background() as (port, _):
        status, headers, body = _request(port, "/ping")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload == {
        "name": "bsky-saves",
        "version": __version__,
        "features": ["fetch-image", "extract-article"],
    }


def test_options_preflight_returns_204_with_cors():
    with serve_in_background() as (port, _):
        status, headers, body = _request(
            port,
            "/fetch-image",
            method="OPTIONS",
            headers={"Origin": DEFAULT_ORIGIN},
        )
    assert status == 204
    assert body == b""
    assert headers["Access-Control-Allow-Origin"] == DEFAULT_ORIGIN
    assert headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
    assert headers["Access-Control-Allow-Headers"] == "Content-Type"
    assert headers["Access-Control-Max-Age"] == "600"


def test_cors_allowed_origin_echoed_on_normal_response():
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port, "/ping", headers={"Origin": DEFAULT_ORIGIN}
        )
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == DEFAULT_ORIGIN


def test_cors_disallowed_origin_omits_allow_origin_header():
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port, "/ping", headers={"Origin": "https://evil.example"}
        )
    assert status == 200
    assert "Access-Control-Allow-Origin" not in headers
    # Other CORS headers still present (CORS is a browser-side mechanism;
    # absence of Allow-Origin is what fails the request closed).
    assert headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"


def test_cors_no_origin_header_request_succeeds():
    """curl-style requests have no Origin and are allowed (no CORS to apply)."""
    with serve_in_background() as (port, _):
        status, headers, body = _request(port, "/ping")
    assert status == 200
    # No Allow-Origin header (no Origin header to echo).
    assert "Access-Control-Allow-Origin" not in headers
    payload = json.loads(body)
    assert payload["name"] == "bsky-saves"


def test_cors_404_response_still_carries_cors_headers():
    """Error responses must also include CORS headers so browsers can read them."""
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port, "/admin", headers={"Origin": DEFAULT_ORIGIN}
        )
    assert status == 404
    assert headers["Access-Control-Allow-Origin"] == DEFAULT_ORIGIN
