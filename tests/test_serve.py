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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import respx

from bsky_saves import serve


DEFAULT_ORIGIN = "https://saves.lightseed.net"


@contextlib.contextmanager
def serve_in_background(allow_origins=(), verbose=False, gui=False):
    """Boot the daemon in a daemon thread on an ephemeral port; yield (port, server).

    allow_origins is the list of *additional* origins (equivalent to repeated
    --allow-origin flags). The default allowlist (_default_origins) is always
    prepended, matching run_serve behaviour after spec §4.4 additive change.

    gui=True passes the resolved gui_root to make_handler, enabling static-file
    serving. Tests that use gui=True should monkeypatch _gui_serve._gui_root_path
    before entering this context manager.
    """
    # Bind port=0 first so the OS assigns an ephemeral port, then build the
    # handler with the actual port so Host-header validation works correctly.
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    full_origins = serve._default_origins(port) + list(allow_origins)
    if gui:
        from bsky_saves._gui_serve import resolve_gui_root
        gui_root = resolve_gui_root()
    else:
        gui_root = None
    handler_cls = serve.make_handler(
        port=port, allow_origins=full_origins, verbose=verbose, gui_root=gui_root
    )
    server.RequestHandlerClass = handler_cls
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


TEST_TOKEN = "test-session-token-please-ignore-aaaaaaaaaaa"


@pytest.fixture
def paired_helper(monkeypatch, tmp_path):
    """Configure the helper to use a known test token. Yields the token
    string so tests can include it in Authorization headers.

    Monkeypatches _io.config_dir to a per-test temp dir and writes the
    token there. After test teardown, monkeypatch reverts both.
    """
    cdir = tmp_path / "bsky-saves"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "token").write_text(TEST_TOKEN + "\n", encoding="utf-8")
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: cdir)
    yield TEST_TOKEN


def _auth_headers(token: str, extra: dict | None = None) -> dict:
    """Build a request headers dict that includes the paired Authorization."""
    headers = {"Authorization": f"Bearer {token}"}
    if extra:
        headers.update(extra)
    return headers


def test_unknown_path_returns_404(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(port, "/admin", headers=_auth_headers(paired_helper))
    assert status == 404
    assert json.loads(body) == {"error": "not found"}


def test_unknown_method_returns_404(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(port, "/ping", method="DELETE", headers=_auth_headers(paired_helper))
    assert status == 404
    assert json.loads(body) == {"error": "not found"}


def test_ping_returns_full_shape(monkeypatch):
    """Asserts the full /ping response shape. gui_bundled is forced to None
    here so the assertion is deterministic regardless of whether the test
    environment has src/bsky_saves/_gui/.gui-version populated (which it
    does in verify.yml CI after fetch_gui.py runs, but not in a bare dev
    checkout). The "marker present" path is covered by the test below."""
    from bsky_saves import __version__
    monkeypatch.setattr(serve, "_bundled_gui_version", lambda: None)
    with serve_in_background() as (port, _):
        status, headers, body = _request(port, "/ping")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload == {
        "name": "bsky-saves",
        "version": __version__,
        "protocol": "2",
        "gui_bundled": None,
        "features": ["fetch-image", "extract-article", "fetch", "enrich", "hydrate-threads", "auth-check", "jwt-credentials"],
    }


def test_ping_includes_gui_bundled_when_marker_present(monkeypatch):
    monkeypatch.setattr(serve, "_bundled_gui_version", lambda: "1.2.3")
    with serve_in_background() as (port, _):
        status, _, body = _request(port, "/ping")
    assert status == 200
    assert json.loads(body)["gui_bundled"] == "1.2.3"


def test_bundled_gui_version_reads_marker_file(tmp_path, monkeypatch):
    """Direct test of the helper that powers /ping's gui_bundled field.
    Monkeypatches _gui_serve._gui_root_path so the lookup hits a temp dir.
    The marker format is `{version}\\n{sha256}\\n` (written by
    scripts/fetch_gui.py); only the first line is the version.
    """
    from bsky_saves import _gui_serve
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: tmp_path)
    assert serve._bundled_gui_version() is None
    marker = tmp_path / ".gui-version"
    marker.write_text(
        "0.6.0\ne47e0c416c6d353b55e211bb0ea55b0c5a4be9d0f46f925cd99100653a3151ba\n",
        encoding="utf-8",
    )
    assert serve._bundled_gui_version() == "0.6.0"
    marker.write_text("", encoding="utf-8")
    assert serve._bundled_gui_version() is None


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
    assert headers["Access-Control-Allow-Headers"] == "Content-Type, Authorization"
    assert headers["Access-Control-Max-Age"] == "600"


def test_cors_allowed_origin_echoed_on_normal_response():
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port, "/ping", headers={"Origin": DEFAULT_ORIGIN}
        )
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == DEFAULT_ORIGIN


def test_cors_disallowed_origin_returns_403():
    """Non-allowlisted origins receive an explicit 403, not just a missing
    Allow-Origin header."""
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/ping", headers={"Origin": "https://evil.example"}
        )
    assert status == 403
    assert json.loads(body) == {"error": "Origin not allowed"}


def test_cors_no_origin_header_request_succeeds():
    """curl-style requests have no Origin and are allowed (no CORS to apply)."""
    with serve_in_background() as (port, _):
        status, headers, body = _request(port, "/ping")
    assert status == 200
    # No Allow-Origin header (no Origin header to echo).
    assert "Access-Control-Allow-Origin" not in headers
    payload = json.loads(body)
    assert payload["name"] == "bsky-saves"


def test_cors_404_response_still_carries_cors_headers(paired_helper):
    """Error responses must also include CORS headers so browsers can read them."""
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port, "/admin",
            headers=_auth_headers(paired_helper, {"Origin": DEFAULT_ORIGIN}),
        )
    assert status == 404
    assert headers["Access-Control-Allow-Origin"] == DEFAULT_ORIGIN


def test_401_response_carries_cors_headers(paired_helper):
    """A 401 from auth rejection must carry Access-Control-Allow-Origin when
    the Origin is in the allowlist, so the browser can read the body and
    surface a pairing prompt instead of just seeing a CORS error."""
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port, "/fetch-image",
            method="POST",
            headers={"Origin": DEFAULT_ORIGIN},  # No Authorization → 401
            body={"url": "https://cdn.bsky.app/x"},
        )
    assert status == 401
    assert headers.get("Access-Control-Allow-Origin") == DEFAULT_ORIGIN


import httpx as _httpx_mod  # noqa: F401  (used implicitly by respx)

from bsky_saves.images import DEFAULT_USER_AGENT as _IMAGES_UA  # noqa: F401


@respx.mock
def test_fetch_image_happy_path(paired_helper):
    respx.get("https://cdn.bsky.app/img/x.jpg").respond(
        200, content=b"BYTES", headers={"Content-Type": "image/jpeg"}
    )
    with serve_in_background() as (port, _):
        status, headers, body = _request(
            port,
            "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://cdn.bsky.app/img/x.jpg"},
        )
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert body == b"BYTES"


@respx.mock
def test_fetch_image_subdomain_wildcard_allowed(paired_helper):
    respx.get("https://video.bsky.app/v.jpg").respond(
        200, content=b"V", headers={"Content-Type": "image/jpeg"}
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://video.bsky.app/v.jpg"},
        )
    assert status == 200
    assert body == b"V"


def test_fetch_image_bare_bsky_app_rejected(paired_helper):
    """Hostname is exactly 'bsky.app' — no leading dot, so subdomain rule doesn't match."""
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://bsky.app/img/x.jpg"},
        )
    assert status == 400
    assert json.loads(body) == {"error": "url not allowed"}


def test_fetch_image_lookalike_domain_rejected(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://bskyapp.com/img/x.jpg"},
        )
    assert status == 400
    assert json.loads(body) == {"error": "url not allowed"}


def test_fetch_image_http_scheme_rejected(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "http://cdn.bsky.app/img/x.jpg"},
        )
    assert status == 400
    assert json.loads(body) == {"error": "url not allowed"}


def test_fetch_image_missing_url_rejected(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/fetch-image", method="POST",
            headers=_auth_headers(paired_helper),
            body={"not_url": "x"},
        )
    assert status == 400
    assert json.loads(body) == {"error": "missing url"}


@respx.mock
def test_fetch_image_upstream_4xx_passed_through(paired_helper):
    respx.get("https://cdn.bsky.app/img/missing.jpg").respond(404)
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://cdn.bsky.app/img/missing.jpg"},
        )
    assert status == 404
    assert json.loads(body) == {"error": "upstream 404"}


@respx.mock
def test_fetch_image_network_error_returns_502(paired_helper):
    import httpx
    respx.get("https://cdn.bsky.app/img/down.jpg").mock(
        side_effect=httpx.ConnectError("nope")
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://cdn.bsky.app/img/down.jpg"},
        )
    assert status == 502
    payload = json.loads(body)
    assert "error" in payload


@respx.mock
def test_extract_article_happy_path(paired_helper):
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
            headers=_auth_headers(paired_helper),
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
def test_extract_article_empty_body_returns_200_with_note(paired_helper):
    html = "<html><body><article>too short</article></body></html>"
    respx.get("https://example.com/short").respond(200, html=html)
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/extract-article",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://example.com/short"},
        )
    assert status == 200
    payload = json.loads(body)
    assert payload["url"] == "https://example.com/short"
    assert payload["text"] == ""
    assert payload["note"] == "no extractable body"
    assert "fetched_at" in payload


def test_extract_article_disallowed_scheme(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/extract-article",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "file:///etc/passwd"},
        )
    assert status == 400
    assert json.loads(body) == {"error": "url scheme not allowed"}


def test_extract_article_missing_url(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/extract-article", method="POST",
            headers=_auth_headers(paired_helper),
            body={"not_url": "x"},
        )
    assert status == 400
    assert json.loads(body) == {"error": "missing url"}


@respx.mock
def test_extract_article_upstream_5xx_passed_through(paired_helper):
    respx.get("https://example.com/down").respond(503)
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/extract-article",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://example.com/down"},
        )
    assert status == 503
    assert json.loads(body) == {"error": "upstream 503"}


@respx.mock
def test_extract_article_network_error_returns_502(paired_helper):
    import httpx
    respx.get("https://example.com/x").mock(
        side_effect=httpx.ConnectError("dns"),
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/extract-article",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://example.com/x"},
        )
    assert status == 502
    assert "error" in json.loads(body)


def test_allow_origin_additive_keeps_defaults():
    """Custom --allow-origin entries are added to (not replace) the default
    allowlist. The default origin (https://saves.lightseed.net) must still
    be allowed after passing --allow-origin."""
    custom = "https://custom.example"
    with serve_in_background(allow_origins=(custom,)) as (port, _):
        # Default origin still allowed.
        status_default, h_default, _ = _request(
            port, "/ping", headers={"Origin": DEFAULT_ORIGIN}
        )
        # Custom origin allowed.
        status_custom, h_custom, _ = _request(
            port, "/ping", headers={"Origin": custom}
        )
        # Unlisted origin still rejected.
        status_other, _, _ = _request(
            port, "/ping", headers={"Origin": "https://unlisted.example"}
        )
    assert status_default == 200
    assert h_default["Access-Control-Allow-Origin"] == DEFAULT_ORIGIN
    assert status_custom == 200
    assert h_custom["Access-Control-Allow-Origin"] == custom
    assert status_other == 403


def test_default_allowlist_includes_loopback_origins():
    """The default allowlist now includes http://127.0.0.1:<port> and
    http://localhost:<port> in addition to https://saves.lightseed.net."""
    with serve_in_background() as (port, _):
        loopback_v4 = f"http://127.0.0.1:{port}"
        loopback_dns = f"http://localhost:{port}"
        status_v4, h_v4, _ = _request(
            port, "/ping", headers={"Origin": loopback_v4}
        )
        status_dns, h_dns, _ = _request(
            port, "/ping", headers={"Origin": loopback_dns}
        )
    assert status_v4 == 200
    assert h_v4["Access-Control-Allow-Origin"] == loopback_v4
    assert status_dns == 200
    assert h_dns["Access-Control-Allow-Origin"] == loopback_dns


def test_multiple_allow_origins_all_allowed():
    a = "https://a.example"
    b = "https://b.example"
    with serve_in_background(allow_origins=(a, b)) as (port, _):
        status_a, h_a, _ = _request(port, "/ping", headers={"Origin": a})
        status_b, h_b, _ = _request(port, "/ping", headers={"Origin": b})
        status_c, _, body_c = _request(
            port, "/ping", headers={"Origin": "https://c.example"}
        )
    assert status_a == 200
    assert h_a["Access-Control-Allow-Origin"] == a
    assert status_b == 200
    assert h_b["Access-Control-Allow-Origin"] == b
    assert status_c == 403
    assert json.loads(body_c) == {"error": "Origin not allowed"}


def test_options_preflight_from_disallowed_origin_returns_403():
    """Spec §4.4: OPTIONS from disallowed origin also returns 403, not 204."""
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="OPTIONS",
            headers={"Origin": "https://evil.example"},
        )
    assert status == 403
    assert json.loads(body) == {"error": "Origin not allowed"}


def test_options_preflight_from_allowed_origin_returns_204():
    """Allowed origin still gets the 204 with echoed Allow-Origin."""
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port,
            "/fetch-image",
            method="OPTIONS",
            headers={"Origin": DEFAULT_ORIGIN},
        )
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == DEFAULT_ORIGIN


def test_ping_origin_disallowed_returns_403():
    """Spec §5.1 (post-2026-05-12 revision): /ping enforces Origin like every
    other endpoint."""
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/ping", headers={"Origin": "https://attacker.example"}
        )
    assert status == 403


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
        "variant": "app_password",
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
def test_fetch_first_page_probes_and_returns_cursor(paired_helper):
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
            headers=_auth_headers(paired_helper),
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
def test_fetch_continuation_skips_probe_via_cursor(paired_helper):
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
            headers=_auth_headers(paired_helper),
            body={
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
                "cursor": cursor,
            },
        )
    assert status == 200
    assert pds_route.called
    assert not appview_route.called


@respx.mock
def test_fetch_response_shape_matches_normalise_record(paired_helper):
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
            headers=_auth_headers(paired_helper),
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"}},
        )
    assert status == 200
    entry = json.loads(body)["saves"][0]
    assert set(entry.keys()) >= {"uri", "saved_at", "post_text", "embed", "author", "images"}
    assert entry["author"]["handle"] == "x.bsky.social"
    assert entry["author"]["display_name"] == "X"
    assert entry["author"]["did"] == "did:plc:x"


@respx.mock
def test_fetch_propagates_subject_status_for_dead_post(paired_helper):
    """A bookmark whose item is a notFoundPost comes back with
    subject_status == 'not_found' in the /fetch response."""
    _mock_fetch_create_session()
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200,
            json={
                "bookmarks": [
                    {
                        "subject": {"uri": "at://x/p/dead"},
                        "createdAt": "2026-04-12T18:31:00Z",
                        "item": {
                            "$type": "app.bsky.feed.defs#notFoundPost",
                            "uri": "at://x/p/dead",
                            "notFound": True,
                        },
                    }
                ]
            },
        )
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"}},
        )
    assert status == 200
    entry = json.loads(body)["saves"][0]
    assert entry["subject_status"] == "not_found"


def test_fetch_invalid_cursor_returns_400(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
                "cursor": "not-a-valid-base64-cursor!!!",
            },
        )
    assert status == 400
    assert json.loads(body) == {"error": "invalid cursor"}


def test_fetch_missing_credentials_returns_400(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(port, "/fetch", method="POST",
                                   headers=_auth_headers(paired_helper), body={})
    assert status == 400
    assert json.loads(body) == {"error": "missing credentials"}


@respx.mock
def test_fetch_pds_defaults_to_bsky_social_when_omitted(paired_helper):
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
            headers=_auth_headers(paired_helper),
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"}},
        )
    assert status == 200
    assert create_session_route.called


@respx.mock
def test_fetch_createsession_failure_returns_401(paired_helper):
    respx.post(f"{PDS_BASE_TEST}/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(401, json={"error": "AuthenticationRequired"})
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "wrong"}},
        )
    assert status == 401
    payload = json.loads(body)
    assert "error" in payload
    assert "createSession failed" in payload["error"]


@respx.mock
def test_upstream_cause_401_does_not_include_www_authenticate(paired_helper):
    """An upstream-PDS 401 (createSession rejected by the PDS) is
    structurally distinct from a pairing-401 (bsky-saves rejecting the
    GUI's Bearer token). The pairing-401 carries WWW-Authenticate; the
    upstream-401 must NOT — that's the signal the GUI uses to decide
    whether to surface pairing recovery vs. existing upstream handling."""
    respx.post(f"{PDS_BASE_TEST}/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(401, json={"error": "AuthenticationRequired"})
    )
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "wrong"}},
        )
    assert status == 401
    assert headers.get("WWW-Authenticate") is None, (
        f"upstream-cause 401 must not carry WWW-Authenticate; got {headers.get('WWW-Authenticate')!r}"
    )


@respx.mock
def test_fetch_silent_fallback_on_endpoint_failure(paired_helper):
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
            headers=_auth_headers(paired_helper),
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
def test_fetch_no_more_pages_returns_null_cursor(paired_helper):
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
            headers=_auth_headers(paired_helper),
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"}},
        )
    assert status == 200
    assert json.loads(body)["cursor"] is None


@respx.mock
def test_fetch_limit_clamping(paired_helper):
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
        _request(port, "/fetch", method="POST",
                 headers=_auth_headers(paired_helper),
                 body={
                     "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
                     "limit": 999,
                 })
        _request(port, "/fetch", method="POST",
                 headers=_auth_headers(paired_helper),
                 body={
                     "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
                     "limit": 0,
                 })
    assert seen_limits == [100, 1]


# --- /enrich endpoint ---


def test_enrich_decodes_post_created_at_for_each_uri(paired_helper):
    """Valid at-URIs with TID rkeys → enriched populated in input order."""
    uri1 = "at://did:plc:abc/app.bsky.feed.post/3jzfcijpj2z2a"
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/enrich",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"uris": [uri1]},
        )
    assert status == 200
    payload = json.loads(body)
    assert len(payload["enriched"]) == 1
    assert payload["enriched"][0]["uri"] == uri1
    assert isinstance(payload["enriched"][0]["post_created_at"], str)
    assert payload["enriched"][0]["post_created_at"]
    assert payload["errors"] == []


def test_enrich_invalid_uri_lands_in_errors(paired_helper):
    """Empty / non-string / malformed at-URI → errors[] with reason 'invalid at-uri'."""
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/enrich",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"uris": ["", "not-a-uri", 42]},
        )
    assert status == 200
    payload = json.loads(body)
    assert payload["enriched"] == []
    assert len(payload["errors"]) == 3
    for err in payload["errors"]:
        assert err["reason"] == "invalid at-uri"


def test_enrich_missing_uris_field_returns_400(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(port, "/enrich", method="POST",
                                   headers=_auth_headers(paired_helper), body={})
    assert status == 400
    assert json.loads(body) == {"error": "missing uris"}


def test_enrich_empty_uris_list_returns_200_with_empty_arrays(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(port, "/enrich", method="POST",
                                   headers=_auth_headers(paired_helper), body={"uris": []})
    assert status == 200
    assert json.loads(body) == {"enriched": [], "errors": []}


def test_enrich_credentials_field_is_ignored(paired_helper):
    """Body with credentials is accepted (no 400); credentials are unused."""
    uri1 = "at://did:plc:abc/app.bsky.feed.post/3jzfcijpj2z2a"
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/enrich",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "uris": [uri1],
                "credentials": {"handle": "x", "app_password": "y"},
            },
        )
    assert status == 200
    assert len(json.loads(body)["enriched"]) == 1


def test_enrich_mixed_valid_and_invalid(paired_helper):
    """Both arrays populated, input order preserved within each."""
    valid = "at://did:plc:abc/app.bsky.feed.post/3jzfcijpj2z2a"
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/enrich",
            method="POST",
            headers=_auth_headers(paired_helper),
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
def test_hydrate_threads_returns_threaded_in_input_order(paired_helper):
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
            headers=_auth_headers(paired_helper),
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
def test_hydrate_threads_thread_replies_uses_v4_chain_logic(paired_helper):
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
            headers=_auth_headers(paired_helper),
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
def test_hydrate_threads_per_uri_failure_lands_in_errors_with_diagnostic(paired_helper):
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
            headers=_auth_headers(paired_helper),
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
def test_hydrate_threads_credentials_validated_via_create_session(paired_helper):
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
            headers=_auth_headers(paired_helper),
            body={
                "uris": ["at://x/p/1"],
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
            },
        )
    assert create_session_route.called
    assert create_session_route.call_count == 1


@respx.mock
def test_hydrate_threads_invalid_credentials_returns_401(paired_helper):
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
            headers=_auth_headers(paired_helper),
            body={
                "uris": ["at://x/p/1"],
                "credentials": {"handle": "alice.bsky.social", "app_password": "wrong"},
            },
        )
    assert status == 401
    assert not upstream.called  # no upstream calls when creds invalid


@respx.mock
def test_hydrate_threads_uses_public_appview_unauthenticated(paired_helper):
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
            headers=_auth_headers(paired_helper),
            body={
                "uris": ["at://x/p/1"],
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
            },
        )
    assert seen_auth_headers == [None]


@respx.mock
def test_hydrate_threads_concurrency_caps_at_5(paired_helper):
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
            headers=_auth_headers(paired_helper),
            body={
                "uris": uris,
                "credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"},
            },
        )
    assert status == 200
    assert max_in_flight <= 5


@respx.mock
def test_hydrate_threads_invalid_uri_in_input(paired_helper):
    _mock_fetch_create_session()
    upstream = respx.get(
        "https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread"
    ).mock(return_value=httpx.Response(200, json={}))
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            headers=_auth_headers(paired_helper),
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


def test_hydrate_threads_missing_credentials_returns_400(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"uris": ["at://x/p/1"]},
        )
    assert status == 400
    assert json.loads(body) == {"error": "missing credentials"}


def test_hydrate_threads_missing_uris_returns_400(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"credentials": {"handle": "alice.bsky.social", "app_password": "xxxx"}},
        )
    assert status == 400
    assert json.loads(body) == {"error": "missing uris"}


# --- v0.4.1: JWT-pair credentials path on /fetch ---


@respx.mock
def test_fetch_jwt_path_happy_no_refresh(paired_helper):
    """Valid JWT pair, accessJwt still good — no refresh, no rotated_credentials."""
    # NOTE: NO _mock_fetch_create_session — JWT path skips createSession.
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
            headers=_auth_headers(paired_helper),
            body={
                "credentials": {
                    "access_jwt": "valid-access",
                    "refresh_jwt": "valid-refresh",
                    "did": "did:plc:abc",
                },
            },
        )
    assert status == 200
    payload = json.loads(body)
    assert len(payload["saves"]) == 1
    assert "rotated_credentials" not in payload


@respx.mock
def test_fetch_jwt_path_skips_createsession(paired_helper):
    """With a JWT pair, the daemon must NOT call createSession at all."""
    create_session_route = respx.post(
        f"{PDS_BASE_TEST}/xrpc/com.atproto.server.createSession"
    ).mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(200, json={"bookmarks": []})
    )
    with serve_in_background() as (port, _):
        _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "credentials": {
                    "access_jwt": "v",
                    "refresh_jwt": "r",
                    "did": "did:plc:x",
                },
            },
        )
    assert not create_session_route.called


@respx.mock
def test_fetch_jwt_path_uses_access_jwt_as_bearer(paired_helper):
    """Daemon sends access_jwt as the Bearer token on the upstream call."""
    seen_auth: list[str] = []

    def capture(request):
        seen_auth.append(request.headers.get("Authorization", ""))
        return httpx.Response(200, json={"bookmarks": []})

    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        side_effect=capture
    )
    with serve_in_background() as (port, _):
        _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "credentials": {
                    "access_jwt": "the-access-jwt",
                    "refresh_jwt": "the-refresh-jwt",
                    "did": "did:plc:x",
                },
            },
        )
    assert seen_auth == ["Bearer the-access-jwt"]


@respx.mock
def test_fetch_jwt_path_refresh_on_401_returns_rotated_credentials(paired_helper):
    """Direct-path: cursor-bearing call → endpoint returns 401 → daemon
    refreshes and retries with rotated tokens, returns rotated_credentials."""
    # bookmark.getBookmarks: first call (with old token) → 401, second call → 200.
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        side_effect=[
            httpx.Response(401, json={"error": "ExpiredToken"}),
            httpx.Response(
                200,
                json={"bookmarks": [_bookmark_record_for_fetch("at://x/p/1")]},
            ),
        ]
    )
    # refreshSession returns new pair.
    respx.post(f"{PDS_BASE_TEST}/xrpc/com.atproto.server.refreshSession").mock(
        return_value=httpx.Response(
            200,
            json={
                "accessJwt": "new-access",
                "refreshJwt": "new-refresh",
                "did": "did:plc:abc",
                "handle": "alice.bsky.social",
            },
        )
    )

    cursor = _encode_cursor("pds:bookmark.getBookmarks", "upstream-x")
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "credentials": {
                    "access_jwt": "expired-access",
                    "refresh_jwt": "valid-refresh",
                    "did": "did:plc:abc",
                },
                "cursor": cursor,
            },
        )
    assert status == 200
    payload = json.loads(body)
    assert len(payload["saves"]) == 1
    assert payload["rotated_credentials"] == {
        "access_jwt": "new-access",
        "refresh_jwt": "new-refresh",
        "did": "did:plc:abc",
    }


@respx.mock
def test_fetch_jwt_path_refresh_failure_returns_401_refresh_failed(paired_helper):
    """When refreshSession itself fails, return 401 with code: refresh_failed."""
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(401, json={"error": "ExpiredToken"})
    )
    respx.post(f"{PDS_BASE_TEST}/xrpc/com.atproto.server.refreshSession").mock(
        return_value=httpx.Response(400, json={"error": "ExpiredToken"})
    )
    cursor = _encode_cursor("pds:bookmark.getBookmarks", "upstream-x")
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "credentials": {
                    "access_jwt": "expired",
                    "refresh_jwt": "also-expired",
                    "did": "did:plc:abc",
                },
                "cursor": cursor,
            },
        )
    assert status == 401
    payload = json.loads(body)
    assert payload["error"] == "auth refresh failed"
    assert payload["code"] == "refresh_failed"


@respx.mock
def test_fetch_jwt_path_persistent_401_after_refresh(paired_helper):
    """Refresh succeeds but retry still gets 401 → upstream_rejected_after_refresh."""
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(401, json={"error": "AuthenticationRequired"})
    )
    respx.post(f"{PDS_BASE_TEST}/xrpc/com.atproto.server.refreshSession").mock(
        return_value=httpx.Response(
            200,
            json={
                "accessJwt": "new-access",
                "refreshJwt": "new-refresh",
                "did": "did:plc:abc",
                "handle": "alice.bsky.social",
            },
        )
    )
    cursor = _encode_cursor("pds:bookmark.getBookmarks", "upstream-x")
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "credentials": {
                    "access_jwt": "expired",
                    "refresh_jwt": "valid-refresh",
                    "did": "did:plc:abc",
                },
                "cursor": cursor,
            },
        )
    assert status == 401
    payload = json.loads(body)
    assert payload["error"] == "auth refresh failed"
    assert payload["code"] == "upstream_rejected_after_refresh"


@respx.mock
def test_fetch_jwt_path_non_401_failure_no_refresh(paired_helper):
    """Non-401 direct failure (e.g. 500) triggers silent fallback, NOT refresh.
    refreshSession should not be called at all."""
    refresh_route = respx.post(
        f"{PDS_BASE_TEST}/xrpc/com.atproto.server.refreshSession"
    ).mock(return_value=httpx.Response(200, json={}))
    # Named endpoint returns 500; fallback re-probe (cursor=None) succeeds via
    # the same endpoint URL on the next call.
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        side_effect=[
            httpx.Response(500, json={"error": "ServerError"}),  # direct call fails
            httpx.Response(  # re-probe call succeeds
                200,
                json={"bookmarks": [_bookmark_record_for_fetch("at://x/p/fallback")]},
            ),
        ]
    )
    cursor = _encode_cursor("pds:bookmark.getBookmarks", "upstream-x")
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "credentials": {
                    "access_jwt": "valid",
                    "refresh_jwt": "valid-refresh",
                    "did": "did:plc:abc",
                },
                "cursor": cursor,
            },
        )
    assert status == 200
    payload = json.loads(body)
    assert len(payload["saves"]) == 1
    assert payload["saves"][0]["uri"] == "at://x/p/fallback"
    assert "rotated_credentials" not in payload  # no refresh happened
    assert not refresh_route.called


@respx.mock
def test_fetch_jwt_path_probe_all_401_triggers_refresh(paired_helper):
    """First-call probe (cursor=None) — all 4 endpoints return 401 → refresh.
    PDS_BASE_TEST == APPVIEW_BASE_TEST in this fixture, so the same URL serves
    both PDS and AppView calls; we use side_effect to advance through them."""
    # bookmark.getBookmarks: pds (401), then post-refresh retry (200).
    # Plus appview:bookmark.getBookmarks (same URL): also 401 in the initial probe.
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        side_effect=[
            httpx.Response(401, json={"error": "ExpiredToken"}),  # pds
            httpx.Response(401, json={"error": "ExpiredToken"}),  # appview (same URL)
            httpx.Response(  # post-refresh retry probe — pds works
                200,
                json={"bookmarks": [_bookmark_record_for_fetch("at://x/p/post-refresh")]},
            ),
        ]
    )
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.feed.getActorBookmarks").mock(
        return_value=httpx.Response(401, json={"error": "ExpiredToken"})
    )
    respx.get(f"{PDS_BASE_TEST}/xrpc/com.atproto.repo.listRecords").mock(
        return_value=httpx.Response(401, json={"error": "ExpiredToken"})
    )
    respx.post(f"{PDS_BASE_TEST}/xrpc/com.atproto.server.refreshSession").mock(
        return_value=httpx.Response(
            200,
            json={
                "accessJwt": "new",
                "refreshJwt": "new-r",
                "did": "did:plc:abc",
                "handle": "alice.bsky.social",
            },
        )
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "credentials": {
                    "access_jwt": "expired",
                    "refresh_jwt": "valid",
                    "did": "did:plc:abc",
                },
            },
        )
    assert status == 200
    payload = json.loads(body)
    assert len(payload["saves"]) == 1
    assert payload["saves"][0]["uri"] == "at://x/p/post-refresh"
    assert "rotated_credentials" in payload


@respx.mock
def test_fetch_jwt_path_probe_all_non_401_no_refresh(paired_helper):
    """First-call probe — all endpoints return non-401 (e.g. 500/404).
    No refresh attempted; return 502."""
    refresh_route = respx.post(
        f"{PDS_BASE_TEST}/xrpc/com.atproto.server.refreshSession"
    ).mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(500, json={"error": "ServerError"})
    )
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.feed.getActorBookmarks").mock(
        return_value=httpx.Response(500, json={"error": "ServerError"})
    )
    respx.get(f"{PDS_BASE_TEST}/xrpc/com.atproto.repo.listRecords").mock(
        return_value=httpx.Response(500, json={"error": "ServerError"})
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "credentials": {
                    "access_jwt": "valid",
                    "refresh_jwt": "valid",
                    "did": "did:plc:abc",
                },
            },
        )
    assert status == 502
    assert not refresh_route.called


def test_fetch_missing_both_password_and_jwt_returns_400(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"credentials": {"handle": "alice.bsky.social"}},  # no app_password, no access_jwt
        )
    assert status == 400
    assert json.loads(body) == {"error": "missing credentials"}


def test_fetch_jwt_missing_did_returns_400(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "credentials": {
                    "access_jwt": "x",
                    "refresh_jwt": "y",
                    # no did
                }
            },
        )
    assert status == 400


# --- v0.4.1: _validate_creds direct unit tests ---


def test_validate_creds_jwt_path_returns_variant_jwt():
    result = _validate_creds({
        "access_jwt": "a",
        "refresh_jwt": "r",
        "did": "did:plc:x",
    })
    assert result is not None
    assert result["variant"] == "jwt"
    assert result["pds"] == "https://bsky.social"


def test_validate_creds_jwt_path_with_explicit_pds():
    result = _validate_creds({
        "access_jwt": "a",
        "refresh_jwt": "r",
        "did": "did:plc:x",
        "pds": "https://eurosky.social",
    })
    assert result["pds"] == "https://eurosky.social"


def test_validate_creds_jwt_path_missing_refresh_jwt_returns_None():
    assert _validate_creds({"access_jwt": "a", "did": "did:plc:x"}) is None


def test_validate_creds_jwt_path_missing_did_returns_None():
    assert _validate_creds({"access_jwt": "a", "refresh_jwt": "r"}) is None


def test_validate_creds_app_password_takes_priority_when_both_present():
    """If both app_password and access_jwt are sent, app_password wins."""
    result = _validate_creds({
        "handle": "alice.bsky.social",
        "app_password": "xxxx",
        "access_jwt": "a",
        "refresh_jwt": "r",
        "did": "did:plc:x",
    })
    assert result["variant"] == "app_password"


def test_validate_creds_app_password_path_returns_variant_app_password():
    """Existing app-password tests pass; the new variant field is set correctly."""
    result = _validate_creds({"handle": "alice.bsky.social", "app_password": "xxxx"})
    assert result["variant"] == "app_password"


# --- v0.4.1: JWT-pair credentials path on /hydrate-threads ---


@respx.mock
def test_hydrate_threads_jwt_path_skips_create_session(paired_helper):
    """JWT path: daemon must NOT call createSession at all."""
    create_session_route = respx.post(
        f"{PDS_BASE_TEST}/xrpc/com.atproto.server.createSession"
    ).mock(return_value=httpx.Response(200, json={}))
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread").mock(
        return_value=httpx.Response(
            200, json=_thread_view_post("at://x/p/1", "did:plc:x", "ok")
        )
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "uris": ["at://x/p/1"],
                "credentials": {
                    "access_jwt": "valid",
                    "refresh_jwt": "valid",
                    "did": "did:plc:abc",
                },
            },
        )
    assert status == 200
    assert not create_session_route.called
    payload = json.loads(body)
    assert len(payload["threaded"]) == 1


@respx.mock
def test_hydrate_threads_jwt_path_uses_public_appview_unauthenticated(paired_helper):
    """Under JWT path, the upstream getPostThread call has no Authorization header
    (just like the app-password path)."""
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
            headers=_auth_headers(paired_helper),
            body={
                "uris": ["at://x/p/1"],
                "credentials": {
                    "access_jwt": "the-access-jwt",
                    "refresh_jwt": "the-refresh-jwt",
                    "did": "did:plc:abc",
                },
            },
        )
    # No Authorization header on the upstream call (public AppView, anonymous).
    assert seen_auth_headers == [None]


@respx.mock
def test_hydrate_threads_jwt_path_no_validation_bogus_jwt_accepted(paired_helper):
    """JWT path: no JWT validation. A clearly-bogus JWT still goes through.
    The endpoint's upstream call doesn't use the JWT, so this isn't an issue."""
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread").mock(
        return_value=httpx.Response(
            200, json=_thread_view_post("at://x/p/1", "did:plc:x", "ok")
        )
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "uris": ["at://x/p/1"],
                "credentials": {
                    "access_jwt": "totally-bogus-not-a-real-jwt",
                    "refresh_jwt": "also-bogus",
                    "did": "did:plc:abc",
                },
            },
        )
    assert status == 200
    assert len(json.loads(body)["threaded"]) == 1


@respx.mock
def test_hydrate_threads_jwt_path_no_rotated_credentials_in_response(paired_helper):
    """/hydrate-threads never includes rotated_credentials (no upstream call
    could trigger refresh)."""
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread").mock(
        return_value=httpx.Response(
            200, json=_thread_view_post("at://x/p/1", "did:plc:x", "ok")
        )
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "uris": ["at://x/p/1"],
                "credentials": {
                    "access_jwt": "valid",
                    "refresh_jwt": "valid",
                    "did": "did:plc:abc",
                },
            },
        )
    payload = json.loads(body)
    assert "rotated_credentials" not in payload


def test_hydrate_threads_jwt_missing_did_returns_400(paired_helper):
    """JWT path requires did; without it, _validate_creds returns None →
    400 missing credentials."""
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "uris": ["at://x/p/1"],
                "credentials": {
                    "access_jwt": "x",
                    "refresh_jwt": "y",
                    # no did
                },
            },
        )
    assert status == 400


def test_hydrate_threads_neither_password_nor_jwt_returns_400(paired_helper):
    """If neither app_password nor access_jwt is present, return 400."""
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/hydrate-threads",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={
                "uris": ["at://x/p/1"],
                "credentials": {"handle": "alice.bsky.social"},
            },
        )
    assert status == 400
    assert json.loads(body) == {"error": "missing credentials"}


# --- v0.4.4: Host header validation (DNS-rebinding protection) ---


def test_host_loopback_with_correct_port_accepted():
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/ping", headers={"Host": f"127.0.0.1:{port}"}
        )
    assert status == 200


def test_host_localhost_with_correct_port_accepted():
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/ping", headers={"Host": f"localhost:{port}"}
        )
    assert status == 200


def test_host_unknown_domain_returns_421():
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/ping", headers={"Host": "evil.example.com"}
        )
    assert status == 421
    assert json.loads(body) == {"error": "misdirected request"}


def test_host_wrong_port_returns_421():
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/ping", headers={"Host": f"127.0.0.1:{port + 1}"}
        )
    assert status == 421


def test_host_ipv6_brackets_returns_421():
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/ping", headers={"Host": f"[::1]:{port}"}
        )
    assert status == 421


def test_host_trailing_dot_returns_421():
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/ping", headers={"Host": f"localhost.:{port}"}
        )
    assert status == 421


def test_body_at_cap_succeeds(paired_helper):
    """A body well under the 10 MB cap is processed normally."""
    payload = json.dumps({"uris": ["at://x"] * 1000}).encode("utf-8")
    assert len(payload) < 10 * 1024 * 1024
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port,
            "/enrich",
            method="POST",
            headers=_auth_headers(paired_helper, {
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            }),
            body=payload,
        )
    # /enrich tolerates invalid URIs and returns 200 with errors[].
    assert status == 200


def test_body_over_cap_returns_413(paired_helper):
    """A body over 10 MB is rejected with 413."""
    # ~11 MB of well-formed JSON. The exact byte count just needs to exceed
    # 10 * 1024 * 1024 = 10,485,760 bytes.
    import http.client
    payload = b'{"uris":[' + b'"x",' * 2_700_000 + b'"y"]}'
    assert len(payload) > 10 * 1024 * 1024
    with serve_in_background() as (port, _):
        # Use http.client directly so we can read the 413 response even if
        # the server closes the connection before we finish sending the body.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(
                "POST",
                "/enrich",
                body=payload,
                headers={
                    "Content-Type": "application/json",
                    "Origin": DEFAULT_ORIGIN,
                    "Host": f"127.0.0.1:{port}",
                    "Authorization": f"Bearer {paired_helper}",
                },
            )
            resp = conn.getresponse()
            status = resp.status
            body = resp.read()
        except (BrokenPipeError, ConnectionResetError):
            # Server closed after sending 413; read whatever was buffered.
            resp = conn.getresponse()
            status = resp.status
            body = resp.read()
        finally:
            conn.close()
    assert status == 413


def test_responses_include_nosniff_header():
    with serve_in_background() as (port, _):
        _, headers, _ = _request(port, "/ping")
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_responses_include_cache_control_no_store():
    with serve_in_background() as (port, _):
        _, headers, _ = _request(port, "/ping")
    assert headers["Cache-Control"] == "no-store"


def test_error_responses_include_security_headers(paired_helper):
    with serve_in_background() as (port, _):
        _, headers, _ = _request(port, "/does-not-exist", headers=_auth_headers(paired_helper))
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"


def test_options_preflight_includes_security_headers():
    with serve_in_background() as (port, _):
        _, headers, _ = _request(
            port,
            "/ping",
            method="OPTIONS",
            headers={"Origin": DEFAULT_ORIGIN},
        )
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"


def test_fetch_rejects_pds_pointing_at_loopback(paired_helper):
    body = {
        "credentials": {
            "handle": "user.bsky.social",
            "app_password": "xxxx-xxxx-xxxx-xxxx",
            "pds": "http://127.0.0.1:8080",
        }
    }
    with serve_in_background() as (port, _):
        status, _, body_resp = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper, {
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            }),
            body=body,
        )
    assert status == 400
    assert json.loads(body_resp) == {"error": "missing credentials"}


def test_fetch_rejects_pds_pointing_at_metadata_ip(paired_helper):
    body = {
        "credentials": {
            "handle": "user.bsky.social",
            "app_password": "xxxx-xxxx-xxxx-xxxx",
            "pds": "https://169.254.169.254",
        }
    }
    with serve_in_background() as (port, _):
        status, _, body_resp = _request(
            port,
            "/fetch",
            method="POST",
            headers=_auth_headers(paired_helper, {
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            }),
            body=body,
        )
    assert status == 400


def test_hydrate_threads_rejects_pds_pointing_at_private_ip(paired_helper):
    body = {
        "uris": ["at://example/post/1"],
        "credentials": {
            "handle": "user.bsky.social",
            "app_password": "xxxx-xxxx-xxxx-xxxx",
            "pds": "https://10.0.0.1",
        },
    }
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port,
            "/hydrate-threads",
            method="POST",
            headers=_auth_headers(paired_helper, {
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            }),
            body=body,
        )
    assert status == 400


@respx.mock
def test_fetch_image_follows_safe_redirect_to_bsky_cdn(paired_helper):
    # Set up a 302 within bsky.app → 200.
    respx.get("https://cdn.bsky.app/img/a.jpg").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://cdn.bsky.app/img/b.jpg"}
        )
    )
    respx.get("https://cdn.bsky.app/img/b.jpg").mock(
        return_value=httpx.Response(
            200, content=b"\xff\xd8\xff\xe0", headers={"Content-Type": "image/jpeg"}
        )
    )
    with serve_in_background() as (port, _):
        status, headers, body = _request(
            port,
            "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper, {
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            }),
            body=json.dumps(
                {"url": "https://cdn.bsky.app/img/a.jpg"}
            ).encode("utf-8"),
        )
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert body == b"\xff\xd8\xff\xe0"


@respx.mock
def test_fetch_image_rejects_redirect_to_non_bsky_host(paired_helper):
    respx.get("https://cdn.bsky.app/img/a.jpg").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://evil.example/x.jpg"}
        )
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper, {
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            }),
            body=json.dumps(
                {"url": "https://cdn.bsky.app/img/a.jpg"}
            ).encode("utf-8"),
        )
    assert status == 400
    assert json.loads(body) == {"error": "url not allowed"}


def test_log_request_escapes_control_chars(capsys):
    """_log_request must escape terminal control bytes via
    encode('ascii', 'backslashreplace') so a request with ESC bytes in the
    path can't reposition the operator's terminal cursor."""
    from bsky_saves.serve import make_handler

    HandlerCls = make_handler(port=1, allow_origins=[], verbose=True)

    # _log_request only reads self.command and self.path, so a minimal stub
    # works. Calling the unbound method directly avoids the BaseHTTPRequestHandler
    # initialization dance (sockets, request parsing, etc.).
    class _Stub:
        command = "GET"
        path = "/ping\x1b[2J"

    HandlerCls._log_request(_Stub())

    captured = capsys.readouterr()
    # The escape byte should appear as its escape sequence in stderr,
    # not as the raw control byte.
    assert "\\x1b" in captured.err
    assert "\x1b" not in captured.err


def test_extract_article_rejects_loopback_url_returns_400(paired_helper):
    """The HTTP endpoint returns 400 {"error":"url not allowed"} for SSRF-blocked
    URLs. The articles._extract_article helper returns
    "fetch_error:UnsafeURLError:..." and the handler maps it to 400, distinct
    from the generic 502 used for other fetch_error: variants."""
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/extract-article",
            method="POST",
            headers=_auth_headers(paired_helper, {
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            }),
            body=json.dumps({"url": "http://127.0.0.1/secret"}).encode("utf-8"),
        )
    assert status == 400
    assert json.loads(body) == {"error": "url not allowed"}


def test_extract_article_rejects_metadata_ip_returns_400(paired_helper):
    """AWS-style metadata IP gets the same 400 url-not-allowed treatment."""
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/extract-article",
            method="POST",
            headers=_auth_headers(paired_helper, {
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            }),
            body=json.dumps(
                {"url": "http://169.254.169.254/latest/meta-data/"}
            ).encode("utf-8"),
        )
    assert status == 400
    assert json.loads(body) == {"error": "url not allowed"}


def test_host_missing_returns_421():
    """Empty/missing Host header is rejected by the security gate.

    Python's http.client always sends a Host header, so we use raw socket
    I/O to construct an HTTP/1.0 request without one.
    """
    import socket
    with serve_in_background() as (port, _):
        sock = socket.create_connection(("127.0.0.1", port))
        try:
            sock.sendall(b"GET /ping HTTP/1.0\r\n\r\n")
            chunks = []
            while True:
                buf = sock.recv(4096)
                if not buf:
                    break
                chunks.append(buf)
            response = b"".join(chunks)
        finally:
            sock.close()
    first_line = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    assert " 421 " in first_line, f"Got: {first_line!r}"


# ---------------------------------------------------------------------------
# --gui flag: startup guard and make_handler gui_root parameter
# ---------------------------------------------------------------------------

def test_run_serve_with_gui_missing_returns_2(tmp_path, monkeypatch, capsys):
    """run_serve(gui=True) with empty _gui/ exits 2 with actionable message."""
    from bsky_saves.serve import run_serve
    from bsky_saves import _gui_serve

    # Point _gui_root_path at an empty directory.
    empty_gui = tmp_path / "_gui"
    empty_gui.mkdir()
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: empty_gui)

    exit_code = run_serve(port=0, gui=True)
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--gui requires" in captured.err
    assert "fetch_gui.py" in captured.err


def test_make_handler_accepts_gui_root_none():
    """make_handler should accept gui_root=None (the default behavior)."""
    from bsky_saves.serve import make_handler
    handler_cls = make_handler(port=1, allow_origins=["https://x"], gui_root=None)
    assert handler_cls is not None


def test_make_handler_accepts_gui_root_path(tmp_path):
    """make_handler accepts a populated _gui/ path."""
    from bsky_saves.serve import make_handler
    handler_cls = make_handler(
        port=1, allow_origins=["https://x"], gui_root=tmp_path
    )
    assert handler_cls is not None


# ---------------------------------------------------------------------------
# Dispatcher integration tests (GET/HEAD through _gui_serve)
# ---------------------------------------------------------------------------


def _populate_gui_for_serve_test(tmp_path):
    """Set up a minimal _gui/ that monkeypatched resolve_gui_root can return."""
    gui = tmp_path / "_gui"
    gui.mkdir()
    (gui / "index.html").write_bytes(b"<html>integration</html>")
    (gui / "assets").mkdir()
    (gui / "assets" / "main-deadbeef.js").write_bytes(b"console.log('integration');")
    return gui


def test_serve_with_gui_mounts_index_at_root(tmp_path, monkeypatch):
    gui = _populate_gui_for_serve_test(tmp_path)
    from bsky_saves import _gui_serve
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui)

    with serve_in_background(gui=True) as (port, _):
        status, headers, body = _request(port, "/")

    assert status == 200
    assert body == b"<html>integration</html>"
    assert headers["Content-Type"].startswith("text/html")
    assert headers["Cache-Control"] == "no-store"


def test_serve_with_gui_serves_assets(tmp_path, monkeypatch):
    gui = _populate_gui_for_serve_test(tmp_path)
    from bsky_saves import _gui_serve
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui)

    with serve_in_background(gui=True) as (port, _):
        status, headers, body = _request(port, "/assets/main-deadbeef.js")

    assert status == 200
    assert body == b"console.log('integration');"
    assert headers["Content-Type"] == "application/javascript"
    assert headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_serve_with_gui_spa_fallback(tmp_path, monkeypatch):
    gui = _populate_gui_for_serve_test(tmp_path)
    from bsky_saves import _gui_serve
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui)

    with serve_in_background(gui=True) as (port, _):
        status, _, body = _request(port, "/some/spa/route")

    assert status == 200
    assert body == b"<html>integration</html>"


def test_serve_with_gui_api_precedence(tmp_path, monkeypatch):
    """Even with --gui, /ping returns JSON, not HTML."""
    gui = _populate_gui_for_serve_test(tmp_path)
    from bsky_saves import _gui_serve
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui)

    with serve_in_background(gui=True) as (port, _):
        status, headers, body = _request(port, "/ping")

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert b"bsky-saves" in body


def test_serve_with_gui_post_to_root_is_404(tmp_path, monkeypatch, paired_helper):
    """POST / is not a real API route and shouldn't serve static files."""
    gui = _populate_gui_for_serve_test(tmp_path)
    from bsky_saves import _gui_serve
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui)

    with serve_in_background(gui=True) as (port, _):
        status, _, _ = _request(
            port, "/",
            method="POST",
            headers=_auth_headers(paired_helper, {
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            }),
            body=b"{}",
        )

    assert status == 404


def test_serve_without_gui_root_is_404(paired_helper):
    """Without --gui, GET / returns the existing 404 (no static branch)."""
    with serve_in_background() as (port, _):
        status, _, _ = _request(port, "/", headers=_auth_headers(paired_helper))
    assert status == 404


def test_serve_with_gui_unknown_api_path_404(tmp_path, monkeypatch):
    """An undocumented API-looking path returns the JSON 404, not SPA index."""
    gui = _populate_gui_for_serve_test(tmp_path)
    from bsky_saves import _gui_serve
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui)

    with serve_in_background(gui=True) as (port, _):
        status, headers, body = _request(port, "/fetch-image")

    # /fetch-image is a documented POST route. GET to it falls through to
    # _gui_serve, which defers (api prefix), and lands at the JSON 404 path.
    assert status == 404
    assert headers["Content-Type"] == "application/json"


# --- v0.6.2: session-token auth tests ---


def test_credentialed_endpoint_401_on_missing_authorization(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/fetch-image",
            method="POST",
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
    assert status == 401
    assert json.loads(body) == {"error": "authentication required"}


def test_credentialed_endpoint_401_on_wrong_token(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/fetch-image",
            method="POST",
            headers=_auth_headers("not-the-real-token"),
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
    assert status == 401
    assert json.loads(body) == {"error": "authentication required"}


def test_credentialed_endpoint_401_on_non_bearer_scheme(paired_helper):
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/fetch-image",
            method="POST",
            headers={"Authorization": f"Basic {paired_helper}"},
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
    assert status == 401


def test_pairing_401_missing_header_includes_www_authenticate_bearer(paired_helper):
    """Pairing-401 (no Bearer prefix) carries WWW-Authenticate: Bearer so
    cross-origin GUIs can distinguish it from an upstream-cause 401."""
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port, "/fetch-image",
            method="POST",
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
    assert status == 401
    assert headers.get("WWW-Authenticate") == 'Bearer realm="bsky-saves"'


def test_pairing_401_wrong_token_includes_www_authenticate_invalid_token(paired_helper):
    """Pairing-401 (Bearer present but token mismatches) carries
    WWW-Authenticate with error=\"invalid_token\" per RFC 6750 §3.1."""
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port, "/fetch-image",
            method="POST",
            headers=_auth_headers("not-the-real-token"),
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
    assert status == 401
    assert headers.get("WWW-Authenticate") == 'Bearer realm="bsky-saves", error="invalid_token"'


def test_auth_check_returns_200_empty_with_valid_token(paired_helper):
    """GET /auth/check with a valid Bearer token returns 200 with empty
    body — the helper-side primitive for the GUI's pairing-modal
    'verify before stashing in localStorage' step."""
    with serve_in_background() as (port, _):
        status, headers, body = _request(
            port, "/auth/check",
            headers=_auth_headers(paired_helper),
        )
    assert status == 200
    assert body == b""
    assert headers.get("Content-Length") == "0"


def test_auth_check_returns_401_without_authorization(paired_helper):
    """GET /auth/check with no Authorization header returns 401 and
    carries WWW-Authenticate: Bearer (same semantics as every other
    credentialed endpoint)."""
    with serve_in_background() as (port, _):
        status, headers, _ = _request(port, "/auth/check")
    assert status == 401
    assert headers.get("WWW-Authenticate") == 'Bearer realm="bsky-saves"'


def test_auth_check_returns_401_with_wrong_token(paired_helper):
    """GET /auth/check with a wrong token returns 401 and carries
    WWW-Authenticate: Bearer ..., error=\"invalid_token\"."""
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port, "/auth/check",
            headers=_auth_headers("not-the-real-token"),
        )
    assert status == 401
    assert headers.get("WWW-Authenticate") == 'Bearer realm="bsky-saves", error="invalid_token"'


def test_response_exposes_www_authenticate_via_cors(paired_helper):
    """Access-Control-Expose-Headers includes WWW-Authenticate so the
    cross-origin GUI's fetch() JS can actually read the header to make
    the pairing-vs-upstream-401 distinction."""
    with serve_in_background() as (port, _):
        status, headers, _ = _request(port, "/ping")
    assert status == 200
    expose = headers.get("Access-Control-Expose-Headers", "")
    # Tolerate either bare value or comma-separated list with extras (future-proof).
    tokens = [t.strip() for t in expose.split(",")]
    assert "WWW-Authenticate" in tokens, f"expected WWW-Authenticate in {expose!r}"


def test_ping_does_not_require_token(paired_helper):
    with serve_in_background() as (port, _):
        status, _, _ = _request(port, "/ping")
    assert status == 200


def test_options_preflight_does_not_require_token(paired_helper):
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/fetch-image",
            method="OPTIONS",
            headers={"Origin": DEFAULT_ORIGIN, "Access-Control-Request-Method": "POST"},
        )
    assert status == 204


def test_rotate_invalidates_running_daemon(paired_helper, tmp_path):
    """If --rotate is called from a separate process while serve is running,
    the next request from the now-stale-token client gets 401. Implementation
    detail this verifies: _check_token reads the token on every request, not
    once at startup."""
    with serve_in_background() as (port, _):
        # Sanity: original token works.
        status, _, _ = _request(
            port, "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
        # 200/400/502 all acceptable here — point is "not 401."
        assert status != 401

        # Simulate --rotate by overwriting the token file.
        cdir = tmp_path / "bsky-saves"
        (cdir / "token").write_text("a-new-rotated-token\n", encoding="utf-8")

        # Old token now invalid.
        status, _, _ = _request(
            port, "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
        assert status == 401

        # New token works.
        status, _, _ = _request(
            port, "/fetch-image",
            method="POST",
            headers=_auth_headers("a-new-rotated-token"),
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
        assert status != 401


# --- v0.6.2: Task 4 – token injection into served index.html ---


def test_index_html_substitutes_token_placeholder(paired_helper, tmp_path, monkeypatch):
    """When --gui is on, GET / serves index.html with the sentinel
    __BSKY_SAVES_TOKEN__ replaced by the current session token."""
    from bsky_saves import _gui_serve
    gui_root = tmp_path / "_gui"
    gui_root.mkdir()
    (gui_root / "index.html").write_text(
        '<html><head><meta name="bsky-saves-token" content="__BSKY_SAVES_TOKEN__"></head></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui_root)

    with serve_in_background(gui=True) as (port, _):
        status, _, body = _request(port, "/")
    assert status == 200
    text = body.decode("utf-8")
    assert "__BSKY_SAVES_TOKEN__" not in text
    assert paired_helper in text


def test_index_html_substitutes_in_spa_fallback(paired_helper, tmp_path, monkeypatch):
    """When --gui is on, a GET to a non-existent SPA route falls back to
    index.html with the same placeholder substitution as the root path."""
    from bsky_saves import _gui_serve
    gui_root = tmp_path / "_gui"
    gui_root.mkdir()
    (gui_root / "index.html").write_text(
        '<html><head><meta name="bsky-saves-token" content="__BSKY_SAVES_TOKEN__"></head></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui_root)

    with serve_in_background(gui=True) as (port, _):
        status, _, body = _request(port, "/some/spa/route")
    assert status == 200
    assert "__BSKY_SAVES_TOKEN__" not in body.decode("utf-8")
    assert paired_helper in body.decode("utf-8")


def test_non_index_static_files_are_not_substituted(paired_helper, tmp_path, monkeypatch):
    """A CSS file containing the literal sentinel must NOT be substituted —
    only index.html and the SPA fallback go through the substitution path."""
    from bsky_saves import _gui_serve
    gui_root = tmp_path / "_gui"
    gui_root.mkdir()
    (gui_root / "index.html").write_text("<html></html>", encoding="utf-8")
    assets = gui_root / "assets"
    assets.mkdir()
    css_body = "/* token sentinel: __BSKY_SAVES_TOKEN__ */"
    (assets / "style.css").write_text(css_body, encoding="utf-8")
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui_root)

    with serve_in_background(gui=True) as (port, _):
        status, _, body = _request(port, "/assets/style.css")
    assert status == 200
    assert body.decode("utf-8") == css_body


def test_static_assets_do_not_require_token(tmp_path, monkeypatch):
    """Per spec §8: static assets in --gui mode are exempt from token auth.
    Counterpart to the credentialed-endpoint 401 tests in Task 3."""
    from bsky_saves import _gui_serve
    gui_root = tmp_path / "_gui"
    gui_root.mkdir()
    (gui_root / "index.html").write_text("<html></html>", encoding="utf-8")
    assets = gui_root / "assets"
    assets.mkdir()
    (assets / "style.css").write_text("body{}", encoding="utf-8")
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui_root)

    # No paired_helper fixture — config_dir is the real one. The static-file
    # branch in _check_token must bypass auth entirely (per spec §5), so this
    # request should succeed without us setting up any token state.
    with serve_in_background(gui=True) as (port, _):
        status, _, _ = _request(port, "/assets/style.css")
    assert status == 200


# --- v0.6.4: first-time pairing-token print ---


def test_maybe_print_first_time_pairing_prints_when_token_absent_and_not_gui(
    monkeypatch, tmp_path, capsys
):
    """First serve on a fresh machine (no token file, --gui not set):
    print the token with a clear pairing-context message and lazy-create
    the token file."""
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    serve._maybe_print_first_time_pairing(gui=False)
    captured = capsys.readouterr()
    assert "first-time setup" in captured.err
    assert (tmp_path / "token").exists()
    written = (tmp_path / "token").read_text(encoding="utf-8").strip()
    assert written in captured.err


def test_maybe_print_first_time_pairing_silent_when_token_exists(
    monkeypatch, tmp_path, capsys
):
    """Subsequent runs (token already exists from prior serve or token
    --rotate): no output, file unchanged."""
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    (tmp_path / "token").write_text("preexisting-token-value\n", encoding="utf-8")
    serve._maybe_print_first_time_pairing(gui=False)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert (tmp_path / "token").read_text(encoding="utf-8").strip() == "preexisting-token-value"


def test_maybe_print_first_time_pairing_skipped_under_gui(
    monkeypatch, tmp_path, capsys
):
    """--gui flag is on: skip the print AND don't lazy-create. The bundled
    GUI's first request triggers token creation via _gui_serve substitution
    path; we don't want a duplicate creation path here that would print
    before the GUI's flow runs."""
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    serve._maybe_print_first_time_pairing(gui=True)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert not (tmp_path / "token").exists()
