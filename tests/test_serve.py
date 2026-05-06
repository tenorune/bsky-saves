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
        "features": ["fetch-image", "extract-article", "fetch", "enrich", "hydrate-threads"],
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


# --- v0.4 helpers ---

from bsky_saves.serve import _validate_creds, _encode_cursor, _decode_cursor


def test_validate_creds_returns_dict_with_pds_default_when_pds_omitted():
    result = _validate_creds({"handle": "alice.bsky.social", "app_password": "xxxx"})
    assert result == {
        "handle": "alice.bsky.social",
        "app_password": "xxxx",
        "pds": "https://bsky.social",
    }


def test_validate_creds_returns_dict_with_explicit_pds():
    result = _validate_creds({
        "handle": "alice.bsky.social",
        "app_password": "xxxx",
        "pds": "https://eurosky.social",
    })
    assert result["pds"] == "https://eurosky.social"


def test_validate_creds_returns_dict_with_pds_default_when_pds_empty_string():
    result = _validate_creds({
        "handle": "alice.bsky.social",
        "app_password": "xxxx",
        "pds": "",
    })
    assert result["pds"] == "https://bsky.social"


def test_validate_creds_returns_None_when_handle_missing():
    assert _validate_creds({"app_password": "xxxx"}) is None


def test_validate_creds_returns_None_when_app_password_missing():
    assert _validate_creds({"handle": "alice.bsky.social"}) is None


def test_validate_creds_returns_None_when_creds_is_None():
    assert _validate_creds(None) is None


def test_validate_creds_returns_None_when_creds_is_not_dict():
    assert _validate_creds("not a dict") is None
    assert _validate_creds([]) is None
    assert _validate_creds(42) is None


def test_encode_cursor_round_trips_through_decode():
    wrapped = _encode_cursor("pds:bookmark.getBookmarks", "upstream-cursor-abc")
    decoded = _decode_cursor(wrapped)
    assert decoded == {"v": 1, "endpoint": "pds:bookmark.getBookmarks", "upstream": "upstream-cursor-abc"}


def test_encode_cursor_handles_None_upstream():
    wrapped = _encode_cursor("appview:getActorBookmarks", None)
    decoded = _decode_cursor(wrapped)
    assert decoded == {"v": 1, "endpoint": "appview:getActorBookmarks", "upstream": None}


def test_decode_cursor_returns_None_for_garbage():
    assert _decode_cursor("not-base64!!!") is None
    assert _decode_cursor("") is None
    # Base64 of valid-looking JSON but missing required fields
    import base64, json
    bad_json = base64.urlsafe_b64encode(json.dumps({"foo": "bar"}).encode()).decode()
    assert _decode_cursor(bad_json) is None


def test_decode_cursor_returns_None_for_unknown_endpoint_id():
    import base64, json
    payload = base64.urlsafe_b64encode(
        json.dumps({"v": 1, "endpoint": "totally:unknown", "upstream": "x"}).encode()
    ).decode()
    assert _decode_cursor(payload) is None


def test_decode_cursor_returns_None_for_wrong_version():
    import base64, json
    payload = base64.urlsafe_b64encode(
        json.dumps({"v": 99, "endpoint": "pds:bookmark.getBookmarks", "upstream": "x"}).encode()
    ).decode()
    assert _decode_cursor(payload) is None


# --- /fetch endpoint ---

import httpx  # noqa: F811 (already imported above as _httpx_mod alias)
from bsky_saves import fetch as _fetch_mod


PDS_BASE_TEST = "https://bsky.social"


def _mock_fetch_create_session(handle="alice.bsky.social", did="did:plc:abc"):
    respx.post(f"{PDS_BASE_TEST}/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(
            200,
            json={
                "accessJwt": "fake-access",
                "refreshJwt": "fake-refresh",
                "did": did,
                "handle": handle,
            },
        )
    )


def _bookmark_record_for_fetch(uri: str, saved_at: str = "2026-04-12T18:31:00Z") -> dict:
    return {
        "subject": {"uri": uri},
        "createdAt": saved_at,
        "item": {
            "uri": uri,
            "indexedAt": saved_at,
            "record": {"text": "post body"},
            "author": {"handle": "x.bsky.social", "displayName": "X", "did": "did:plc:x"},
        },
    }


@respx.mock
def test_fetch_first_page_probes_and_returns_cursor():
    _mock_fetch_create_session()
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200,
            json={
                "bookmarks": [_bookmark_record_for_fetch("at://x/p/1")],
                "cursor": "upstream-cursor-page-2",
            },
        )
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"}},
        )
    assert status == 200
    payload = json.loads(body)
    assert len(payload["saves"]) == 1
    assert payload["saves"][0]["uri"] == "at://x/p/1"
    assert payload["cursor"] is not None
    decoded = _decode_cursor(payload["cursor"])
    assert decoded["endpoint"] == "pds:bookmark.getBookmarks"
    assert decoded["upstream"] == "upstream-cursor-page-2"


@respx.mock
def test_fetch_continuation_skips_probe_via_cursor():
    """Continuation cursor names a specific endpoint; daemon calls only that one."""
    _mock_fetch_create_session()
    pds_route = respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200,
            json={"bookmarks": [_bookmark_record_for_fetch("at://x/p/2")]},
        )
    )
    appview_route = respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.feed.getActorBookmarks").mock(
        return_value=httpx.Response(404, json={"error": "should-not-be-called"})
    )
    cursor = _encode_cursor("pds:bookmark.getBookmarks", "upstream-cursor-page-2")
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            body={
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
                "cursor": cursor,
            },
        )
    assert status == 200
    assert pds_route.called
    assert not appview_route.called


@respx.mock
def test_fetch_response_shape_matches_normalise_record():
    """Each saves[] entry has the exact field set produced by normalise_record."""
    _mock_fetch_create_session()
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200,
            json={"bookmarks": [_bookmark_record_for_fetch("at://x/p/1")]},
        )
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"}},
        )
    assert status == 200
    entry = json.loads(body)["saves"][0]
    assert set(entry.keys()) >= {"uri", "saved_at", "post_text", "embed", "author", "images"}
    assert entry["author"]["handle"] == "x.bsky.social"
    assert entry["author"]["display_name"] == "X"
    assert entry["author"]["did"] == "did:plc:x"


def test_fetch_invalid_cursor_returns_400():
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            body={
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
                "cursor": "not-a-valid-base64-cursor!!!",
            },
        )
    assert status == 400
    assert json.loads(body) == {"error": "invalid cursor"}


def test_fetch_missing_credentials_returns_400():
    with serve_in_background() as (port, _):
        status, _, body = _request(port, "/fetch", method="POST", body={})
    assert status == 400
    assert json.loads(body) == {"error": "missing credentials"}


@respx.mock
def test_fetch_pds_defaults_to_bsky_social_when_omitted():
    """Credentials without `pds` → daemon calls createSession against bsky.social."""
    create_session_route = respx.post(
        f"{PDS_BASE_TEST}/xrpc/com.atproto.server.createSession"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"accessJwt": "x", "refreshJwt": "y", "did": "did:plc:x", "handle": "alice.bsky.social"},
        )
    )
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(200, json={"bookmarks": []})
    )
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port,
            "/fetch",
            method="POST",
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"}},
        )
    assert status == 200
    assert create_session_route.called


@respx.mock
def test_fetch_createsession_failure_returns_401():
    respx.post(f"{PDS_BASE_TEST}/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(401, json={"error": "AuthenticationRequired"})
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "wrong"}},
        )
    assert status == 401
    payload = json.loads(body)
    assert "error" in payload
    assert "createSession failed" in payload["error"]


@respx.mock
def test_fetch_silent_fallback_on_endpoint_failure():
    """Continuation with a wrapped cursor whose named endpoint returns 5xx →
    daemon re-probes (cursor dropped) and returns next page from new winner."""
    _mock_fetch_create_session()
    cursor = _encode_cursor("pds:bookmark.getBookmarks", "upstream-x")
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        side_effect=[
            httpx.Response(500, json={"error": "ServerError"}),
            httpx.Response(
                200,
                json={"bookmarks": [_bookmark_record_for_fetch("at://x/p/fallback")]},
            ),
        ]
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            body={
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
                "cursor": cursor,
            },
        )
    assert status == 200
    payload = json.loads(body)
    assert len(payload["saves"]) == 1
    assert payload["saves"][0]["uri"] == "at://x/p/fallback"


@respx.mock
def test_fetch_no_more_pages_returns_null_cursor():
    _mock_fetch_create_session()
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200,
            json={"bookmarks": [_bookmark_record_for_fetch("at://x/p/1")]},
        )
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"}},
        )
    assert status == 200
    assert json.loads(body)["cursor"] is None


@respx.mock
def test_fetch_limit_clamping():
    """limit: 999 clamped to 100; limit: 0 clamped to 1."""
    _mock_fetch_create_session()
    seen_limits: list[int] = []

    def capture(request):
        seen_limits.append(int(request.url.params.get("limit", "0")))
        return httpx.Response(200, json={"bookmarks": []})

    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        side_effect=capture
    )
    with serve_in_background() as (port, _):
        _request(port, "/fetch", method="POST", body={
            "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
            "limit": 999,
        })
        _request(port, "/fetch", method="POST", body={
            "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
            "limit": 0,
        })
    assert seen_limits == [100, 1]


# --- /enrich endpoint ---


def test_enrich_decodes_post_created_at_for_each_uri():
    """Valid at-URIs with TID rkeys → enriched populated in input order."""
    uri1 = "at://did:plc:abc/app.bsky.feed.post/3jzfcijpj2z2a"
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/enrich",
            method="POST",
            body={"uris": [uri1]},
        )
    assert status == 200
    payload = json.loads(body)
    assert len(payload["enriched"]) == 1
    assert payload["enriched"][0]["uri"] == uri1
    assert isinstance(payload["enriched"][0]["post_created_at"], str)
    assert payload["enriched"][0]["post_created_at"]
    assert payload["errors"] == []


def test_enrich_invalid_uri_lands_in_errors():
    """Empty / non-string / malformed at-URI → errors[] with reason 'invalid at-uri'."""
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/enrich",
            method="POST",
            body={"uris": ["", "not-a-uri", 42]},
        )
    assert status == 200
    payload = json.loads(body)
    assert payload["enriched"] == []
    assert len(payload["errors"]) == 3
    for err in payload["errors"]:
        assert err["reason"] == "invalid at-uri"


def test_enrich_missing_uris_field_returns_400():
    with serve_in_background() as (port, _):
        status, _, body = _request(port, "/enrich", method="POST", body={})
    assert status == 400
    assert json.loads(body) == {"error": "missing uris"}


def test_enrich_empty_uris_list_returns_200_with_empty_arrays():
    with serve_in_background() as (port, _):
        status, _, body = _request(port, "/enrich", method="POST", body={"uris": []})
    assert status == 200
    assert json.loads(body) == {"enriched": [], "errors": []}


def test_enrich_credentials_field_is_ignored():
    """Body with credentials is accepted (no 400); credentials are unused."""
    uri1 = "at://did:plc:abc/app.bsky.feed.post/3jzfcijpj2z2a"
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/enrich",
            method="POST",
            body={
                "uris": [uri1],
                "credentials": {"handle": "x", "app_password": "y"},
            },
        )
    assert status == 200
    assert len(json.loads(body)["enriched"]) == 1


def test_enrich_mixed_valid_and_invalid():
    """Both arrays populated, input order preserved within each."""
    valid = "at://did:plc:abc/app.bsky.feed.post/3jzfcijpj2z2a"
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/enrich",
            method="POST",
            body={"uris": [valid, "", valid, "bogus"]},
        )
    assert status == 200
    payload = json.loads(body)
    assert len(payload["enriched"]) == 2
    assert payload["enriched"][0]["uri"] == valid
    assert payload["enriched"][1]["uri"] == valid
    assert len(payload["errors"]) == 2


# --- /hydrate-threads endpoint ---

import threading


def _thread_view_post(uri, did, text, replies=None):
    """Build a fetch_thread response that exercises collect_same_author_replies."""
    return {
        "thread": {
            "post": {
                "uri": uri,
                "author": {"did": did, "handle": "x.bsky.social"},
                "indexedAt": "2026-05-06T00:00:00Z",
                "record": {"text": text},
                "embed": {},
            },
            "replies": replies or [],
        }
    }


@respx.mock
def test_hydrate_threads_returns_threaded_in_input_order():
    _mock_fetch_create_session()
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json=_thread_view_post(
                req.url.params["uri"], "did:plc:x", "post text"
            ),
        )
    )
    uris = [
        "at://did:plc:x/app.bsky.feed.post/aaa",
        "at://did:plc:x/app.bsky.feed.post/bbb",
        "at://did:plc:x/app.bsky.feed.post/ccc",
    ]
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            body={
                "uris": uris,
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
            },
        )
    assert status == 200
    payload = json.loads(body)
    assert [t["uri"] for t in payload["threaded"]] == uris  # input order preserved
    assert payload["errors"] == []


@respx.mock
def test_hydrate_threads_thread_replies_uses_v4_chain_logic():
    """A reply tree where OP responds to other commenters yields no thread_replies
    (v0.3.1 chain-broken fix); a true self-thread chain yields the chain."""
    _mock_fetch_create_session()
    op_did = "did:plc:op"
    other_did = "did:plc:other"
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread").mock(
        return_value=httpx.Response(
            200,
            json={
                "thread": {
                    "post": {
                        "uri": "at://op/root",
                        "author": {"did": op_did, "handle": "op.bsky.social"},
                        "indexedAt": "2026-05-06T00:00:00Z",
                        "record": {"text": "root"},
                        "embed": {},
                    },
                    "replies": [
                        {
                            "post": {
                                "uri": "at://op/cont",
                                "author": {"did": op_did, "handle": "op.bsky.social"},
                                "indexedAt": "2026-05-06T00:01:00Z",
                                "record": {"text": "self continuation"},
                                "embed": {},
                            },
                            "replies": [],
                        },
                        {
                            "post": {
                                "uri": "at://other/c1",
                                "author": {"did": other_did, "handle": "other.bsky.social"},
                                "indexedAt": "2026-05-06T00:02:00Z",
                                "record": {"text": "comment"},
                                "embed": {},
                            },
                            "replies": [
                                {
                                    "post": {
                                        "uri": "at://op/reply-to-other",
                                        "author": {"did": op_did, "handle": "op.bsky.social"},
                                        "indexedAt": "2026-05-06T00:03:00Z",
                                        "record": {"text": "thank you"},
                                        "embed": {},
                                    },
                                    "replies": [],
                                }
                            ],
                        },
                    ],
                }
            },
        )
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            body={
                "uris": ["at://op/root"],
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
            },
        )
    assert status == 200
    entry = json.loads(body)["threaded"][0]
    reply_uris = [r["uri"] for r in entry["thread_replies"]]
    assert reply_uris == ["at://op/cont"]


@respx.mock
def test_hydrate_threads_per_uri_failure_lands_in_errors_with_diagnostic():
    """Concurrent execution means side_effect-list ordering is non-deterministic;
    use a URL-keyed mock that responds based on the requested ?uri= param."""
    _mock_fetch_create_session()

    def respond_by_uri(request):
        target = request.url.params.get("uri", "")
        if target == "at://x/p/1":
            return httpx.Response(404, json={"error": "NotFound"})
        return httpx.Response(
            200, json=_thread_view_post(target, "did:plc:x", "ok")
        )

    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread").mock(
        side_effect=respond_by_uri
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            body={
                "uris": ["at://x/p/1", "at://x/p/2"],
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
            },
        )
    assert status == 200
    payload = json.loads(body)
    assert len(payload["threaded"]) == 1
    assert payload["threaded"][0]["uri"] == "at://x/p/2"
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["uri"] == "at://x/p/1"
    assert "404" in payload["errors"][0]["reason"]


@respx.mock
def test_hydrate_threads_credentials_validated_via_create_session():
    """Mock observes daemon called createSession once with the request's credentials."""
    create_session_route = respx.post(
        f"{PDS_BASE_TEST}/xrpc/com.atproto.server.createSession"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"accessJwt": "x", "refreshJwt": "y", "did": "did:plc:x", "handle": "alice.bsky.social"},
        )
    )
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread").mock(
        return_value=httpx.Response(
            200, json=_thread_view_post("at://x/p/1", "did:plc:x", "ok")
        )
    )
    with serve_in_background() as (port, _):
        _request(
            port,
            "/hydrate-threads",
            method="POST",
            body={
                "uris": ["at://x/p/1"],
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
            },
        )
    assert create_session_route.called
    assert create_session_route.call_count == 1


@respx.mock
def test_hydrate_threads_invalid_credentials_returns_401():
    respx.post(f"{PDS_BASE_TEST}/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(401, json={"error": "AuthenticationRequired"})
    )
    upstream = respx.get(
        "https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread"
    ).mock(return_value=httpx.Response(200, json={}))
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            body={
                "uris": ["at://x/p/1"],
                "credentials": {"handle": "alice.bsky.social", "app_password": "wrong"},
            },
        )
    assert status == 401
    assert not upstream.called  # no upstream calls when creds invalid


@respx.mock
def test_hydrate_threads_uses_public_appview_unauthenticated():
    """Mock asserts the request to getPostThread had no Authorization header."""
    _mock_fetch_create_session()
    seen_auth_headers: list[str | None] = []

    def capture(request):
        seen_auth_headers.append(request.headers.get("Authorization"))
        return httpx.Response(
            200, json=_thread_view_post("at://x/p/1", "did:plc:x", "ok")
        )

    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread").mock(
        side_effect=capture
    )
    with serve_in_background() as (port, _):
        _request(
            port,
            "/hydrate-threads",
            method="POST",
            body={
                "uris": ["at://x/p/1"],
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
            },
        )
    assert seen_auth_headers == [None]


@respx.mock
def test_hydrate_threads_concurrency_caps_at_5():
    """20 URIs in input → mock observes at most 5 concurrent getPostThread calls."""
    _mock_fetch_create_session()
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    def capture(request):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            if in_flight > max_in_flight:
                max_in_flight = in_flight
        import time
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return httpx.Response(
            200,
            json=_thread_view_post(request.url.params["uri"], "did:plc:x", "ok"),
        )

    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread").mock(
        side_effect=capture
    )
    uris = [f"at://did:plc:x/app.bsky.feed.post/{i:04d}" for i in range(20)]
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            body={
                "uris": uris,
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
            },
        )
    assert status == 200
    assert max_in_flight <= 5


@respx.mock
def test_hydrate_threads_invalid_uri_in_input():
    _mock_fetch_create_session()
    upstream = respx.get(
        "https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread"
    ).mock(return_value=httpx.Response(200, json={}))
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            body={
                "uris": ["", 42, ""],
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
            },
        )
    assert status == 200
    payload = json.loads(body)
    assert payload["threaded"] == []
    assert len(payload["errors"]) == 3
    for err in payload["errors"]:
        assert err["reason"] == "invalid at-uri"
    assert not upstream.called


def test_hydrate_threads_missing_credentials_returns_400():
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            body={"uris": ["at://x/p/1"]},
        )
    assert status == 400
    assert json.loads(body) == {"error": "missing credentials"}


def test_hydrate_threads_missing_uris_returns_400():
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"}},
        )
    assert status == 400
    assert json.loads(body) == {"error": "missing uris"}
