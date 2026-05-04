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
