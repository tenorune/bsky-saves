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


import httpx as _httpx_mod  # noqa: F401  (used implicitly by respx)

from bsky_saves.images import DEFAULT_USER_AGENT as _IMAGES_UA  # noqa: F401


@respx.mock
def test_fetch_image_happy_path():
    respx.get("https://cdn.bsky.app/img/x.jpg").respond(
        200, content=b"BYTES", headers={"Content-Type": "image/jpeg"}
    )
    with serve_in_background() as (port, _):
        status, headers, body = _request(
            port,
            "/fetch-image",
            method="POST",
            body={"url": "https://cdn.bsky.app/img/x.jpg"},
        )
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert body == b"BYTES"


@respx.mock
def test_fetch_image_subdomain_wildcard_allowed():
    respx.get("https://video.bsky.app/v.jpg").respond(
        200, content=b"V", headers={"Content-Type": "image/jpeg"}
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            body={"url": "https://video.bsky.app/v.jpg"},
        )
    assert status == 200
    assert body == b"V"


def test_fetch_image_bare_bsky_app_rejected():
    """Hostname is exactly 'bsky.app' — no leading dot, so subdomain rule doesn't match."""
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            body={"url": "https://bsky.app/img/x.jpg"},
        )
    assert status == 400
    assert json.loads(body) == {"error": "url not allowed"}


def test_fetch_image_lookalike_domain_rejected():
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            body={"url": "https://bskyapp.com/img/x.jpg"},
        )
    assert status == 400
    assert json.loads(body) == {"error": "url not allowed"}


def test_fetch_image_http_scheme_rejected():
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            body={"url": "http://cdn.bsky.app/img/x.jpg"},
        )
    assert status == 400
    assert json.loads(body) == {"error": "url not allowed"}


def test_fetch_image_missing_url_rejected():
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/fetch-image", method="POST", body={"not_url": "x"}
        )
    assert status == 400
    assert json.loads(body) == {"error": "missing url"}


@respx.mock
def test_fetch_image_upstream_4xx_passed_through():
    respx.get("https://cdn.bsky.app/img/missing.jpg").respond(404)
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            body={"url": "https://cdn.bsky.app/img/missing.jpg"},
        )
    assert status == 404
    assert json.loads(body) == {"error": "upstream 404"}


@respx.mock
def test_fetch_image_network_error_returns_502():
    import httpx
    respx.get("https://cdn.bsky.app/img/down.jpg").mock(
        side_effect=httpx.ConnectError("nope")
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            body={"url": "https://cdn.bsky.app/img/down.jpg"},
        )
    assert status == 502
    payload = json.loads(body)
    assert "error" in payload


@respx.mock
def test_extract_article_happy_path():
    html = (
        "<html><head><title>Hello</title></head><body><article>"
        + ("Body text. " * 30)
        + "</article></body></html>"
    )
    respx.get("https://example.com/a").respond(200, html=html)
    with serve_in_background() as (port, _):
        status, headers, body = _request(
            port,
            "/extract-article",
            method="POST",
            body={"url": "https://example.com/a"},
        )
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload["url"] == "https://example.com/a"
    assert payload["title"] == "Hello"
    assert "Body text." in payload["text"]
    assert "fetched_at" in payload
    assert "note" not in payload


@respx.mock
def test_extract_article_empty_body_returns_200_with_note():
    html = "<html><body><article>too short</article></body></html>"
    respx.get("https://example.com/short").respond(200, html=html)
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/extract-article",
            method="POST",
            body={"url": "https://example.com/short"},
        )
    assert status == 200
    payload = json.loads(body)
    assert payload["url"] == "https://example.com/short"
    assert payload["text"] == ""
    assert payload["note"] == "no extractable body"
    assert "fetched_at" in payload


def test_extract_article_disallowed_scheme():
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/extract-article",
            method="POST",
            body={"url": "file:///etc/passwd"},
        )
    assert status == 400
    assert json.loads(body) == {"error": "url scheme not allowed"}


def test_extract_article_missing_url():
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/extract-article", method="POST", body={"not_url": "x"}
        )
    assert status == 400
    assert json.loads(body) == {"error": "missing url"}


@respx.mock
def test_extract_article_upstream_5xx_passed_through():
    respx.get("https://example.com/down").respond(503)
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/extract-article",
            method="POST",
            body={"url": "https://example.com/down"},
        )
    assert status == 503
    assert json.loads(body) == {"error": "upstream 503"}


@respx.mock
def test_extract_article_network_error_returns_502():
    import httpx
    respx.get("https://example.com/x").mock(
        side_effect=httpx.ConnectError("dns"),
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/extract-article",
            method="POST",
            body={"url": "https://example.com/x"},
        )
    assert status == 502
    assert "error" in json.loads(body)


def test_allow_origin_override_replaces_default():
    """Custom allow_origins fully replaces the default list."""
    custom = "https://other.example"
    with serve_in_background(allow_origins=(custom,)) as (port, _):
        # Default origin must NOT be allowed.
        _, headers_default, _ = _request(
            port, "/ping", headers={"Origin": DEFAULT_ORIGIN}
        )
        assert "Access-Control-Allow-Origin" not in headers_default

        # Custom origin must be allowed.
        _, headers_custom, _ = _request(
            port, "/ping", headers={"Origin": custom}
        )
        assert headers_custom["Access-Control-Allow-Origin"] == custom


def test_multiple_allow_origins_all_allowed():
    a = "https://a.example"
    b = "https://b.example"
    with serve_in_background(allow_origins=(a, b)) as (port, _):
        _, h_a, _ = _request(port, "/ping", headers={"Origin": a})
        _, h_b, _ = _request(port, "/ping", headers={"Origin": b})
        _, h_other, _ = _request(
            port, "/ping", headers={"Origin": "https://c.example"}
        )
    assert h_a["Access-Control-Allow-Origin"] == a
    assert h_b["Access-Control-Allow-Origin"] == b
    assert "Access-Control-Allow-Origin" not in h_other


def test_verbose_logs_request_to_stderr(capfd):
    """With verbose=True, each request emits a 'bsky-saves: <method> <path>'
    line to stderr. (capfd captures fd-level output, which is where
    BaseHTTPRequestHandler's threads write.)"""
    with serve_in_background(verbose=True) as (port, _):
        _request(port, "/ping")
    err = capfd.readouterr().err
    assert "bsky-saves: GET /ping" in err


def test_default_silent_no_request_log(capfd):
    """Without verbose, no per-request stderr output."""
    with serve_in_background(verbose=False) as (port, _):
        _request(port, "/ping")
    err = capfd.readouterr().err
    assert "bsky-saves: GET /ping" not in err
