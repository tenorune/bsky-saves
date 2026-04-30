"""Tests for fetch.probe_bookmark_endpoints / auth.create_session, mocked via respx."""
from __future__ import annotations

import httpx
import pytest
import respx

from bsky_saves import auth, fetch


PDS_BASE = "https://bsky.social"
APPVIEW_BASE = "https://bsky.social"


def _mock_session(handle="user.bsky.social", did="did:plc:abc"):
    return {
        "accessJwt": "fake-access-token",
        "refreshJwt": "fake-refresh-token",
        "did": did,
        "handle": handle,
    }


def _mock_service_auth_ok(token="fake-service-token"):
    respx.get(f"{PDS_BASE}/xrpc/com.atproto.server.getServiceAuth").mock(
        return_value=httpx.Response(200, json={"token": token})
    )


@respx.mock
def test_create_session_returns_access_jwt():
    respx.post(f"{PDS_BASE}/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(
            200,
            json={
                "accessJwt": "abc",
                "refreshJwt": "def",
                "did": "did:plc:xyz",
                "handle": "user.bsky.social",
            },
        )
    )
    session = auth.create_session(PDS_BASE, "user.bsky.social", "app-password")
    assert session["accessJwt"] == "abc"
    assert session["did"] == "did:plc:xyz"


@respx.mock
def test_create_session_raises_on_401():
    respx.post(f"{PDS_BASE}/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(401, json={"error": "AuthenticationRequired"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        auth.create_session(PDS_BASE, "user.bsky.social", "wrong")


@respx.mock
def test_probe_bookmark_endpoints_succeeds_on_first():
    session = _mock_session()
    _mock_service_auth_ok()
    respx.get(f"{APPVIEW_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200,
            json={"bookmarks": [{"uri": "at://x/1", "indexedAt": "2026-04-12T00:00:00Z"}]},
        )
    )
    endpoint, records = fetch.probe_bookmark_endpoints(
        session, pds_base=PDS_BASE, appview_base=APPVIEW_BASE
    )
    assert endpoint == "app.bsky.bookmark.getBookmarks"
    assert len(records) == 1


@respx.mock
def test_probe_bookmark_endpoints_falls_through_on_404():
    session = _mock_session()
    _mock_service_auth_ok()
    respx.get(f"{APPVIEW_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(404, json={"error": "MethodNotImplemented"})
    )
    respx.get(f"{APPVIEW_BASE}/xrpc/app.bsky.feed.getActorBookmarks").mock(
        return_value=httpx.Response(
            200, json={"bookmarks": [{"uri": "at://y/1", "indexedAt": "2026-04-12T00:00:00Z"}]}
        )
    )
    endpoint, records = fetch.probe_bookmark_endpoints(
        session, pds_base=PDS_BASE, appview_base=APPVIEW_BASE
    )
    assert endpoint == "app.bsky.feed.getActorBookmarks"


@respx.mock
def test_probe_bookmark_endpoints_raises_when_all_fail():
    session = _mock_session()
    _mock_service_auth_ok()
    respx.get(f"{APPVIEW_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(401, json={"error": "AuthenticationRequired"})
    )
    respx.get(f"{APPVIEW_BASE}/xrpc/app.bsky.feed.getActorBookmarks").mock(
        return_value=httpx.Response(404, json={"error": "MethodNotImplemented"})
    )
    respx.get(f"{PDS_BASE}/xrpc/com.atproto.repo.listRecords").mock(
        return_value=httpx.Response(403, json={"error": "Forbidden"})
    )
    with pytest.raises(fetch.NoBookmarkEndpointError) as exc_info:
        fetch.probe_bookmark_endpoints(
            session, pds_base=PDS_BASE, appview_base=APPVIEW_BASE
        )
    msg = str(exc_info.value)
    assert "401" in msg
    assert "404" in msg
    assert "403" in msg


@respx.mock
def test_pagination_collects_all_pages():
    session = _mock_session()
    _mock_service_auth_ok()
    respx.get(f"{APPVIEW_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "bookmarks": [{"uri": "at://x/1", "indexedAt": "2026-04-12T00:00:00Z"}],
                    "cursor": "page2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "bookmarks": [{"uri": "at://x/2", "indexedAt": "2026-04-11T00:00:00Z"}],
                },
            ),
        ]
    )
    _, records = fetch.probe_bookmark_endpoints(
        session, pds_base=PDS_BASE, appview_base=APPVIEW_BASE
    )
    uris = [r["uri"] for r in records]
    assert uris == ["at://x/1", "at://x/2"]
