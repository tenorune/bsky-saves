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


# --- fetch_to_inventory write-on-change tests ---

import json
from pathlib import Path

from bsky_saves import fetch as _fetch_mod


def _mock_create_session(handle="user.bsky.social", did="did:plc:abc"):
    respx.post(f"{PDS_BASE}/xrpc/com.atproto.server.createSession").mock(
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


def _bookmark_record(uri: str, saved_at: str = "2026-04-12T18:31:00Z") -> dict:
    """Match the hydrated app.bsky.bookmark.getBookmarks shape that
    normalise_record consumes."""
    return {
        "subject": {"uri": uri},
        "createdAt": saved_at,
        "item": {
            "uri": uri,
            "indexedAt": saved_at,
            "record": {"text": "post body"},
            "author": {
                "handle": "x.bsky.social",
                "displayName": "X",
                "did": "did:plc:x",
            },
        },
    }


@respx.mock
def test_fetch_to_inventory_no_write_when_no_new_saves(tmp_path, monkeypatch):
    """Second fetch with the same bookmarks must leave the inventory file
    untouched (no fetched_at bump, no rewrite). Two distinct timestamps
    from monkeypatched _now_iso make a coincidental same-second pass impossible."""
    _mock_create_session()
    respx.get(f"{PDS_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200, json={"bookmarks": [_bookmark_record("at://x/p/1")]}
        )
    )

    inv_path = tmp_path / "inv.json"

    timestamps = iter(["2026-04-12T00:00:00Z", "2026-04-12T01:00:00Z"])
    monkeypatch.setattr(_fetch_mod, "_now_iso", lambda: next(timestamps))

    _fetch_mod.fetch_to_inventory(
        inv_path,
        handle="user.bsky.social",
        app_password="app-password",
        pds_base=PDS_BASE,
        appview_base=APPVIEW_BASE,
    )
    first = json.loads(inv_path.read_text(encoding="utf-8"))
    assert first["fetched_at"] == "2026-04-12T00:00:00Z"

    _fetch_mod.fetch_to_inventory(
        inv_path,
        handle="user.bsky.social",
        app_password="app-password",
        pds_base=PDS_BASE,
        appview_base=APPVIEW_BASE,
    )
    second = json.loads(inv_path.read_text(encoding="utf-8"))
    assert second["fetched_at"] == "2026-04-12T00:00:00Z", (
        "second fetch with no new bookmarks must not bump fetched_at"
    )


@respx.mock
def test_fetch_to_inventory_writes_when_new_saves(tmp_path):
    """A fetch that returns new saves must rewrite the inventory."""
    _mock_create_session()

    inv_path = tmp_path / "inv.json"

    # First run: one bookmark.
    respx.get(f"{PDS_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200, json={"bookmarks": [_bookmark_record("at://x/p/1")]}
        )
    )
    _fetch_mod.fetch_to_inventory(
        inv_path,
        handle="user.bsky.social",
        app_password="app-password",
        pds_base=PDS_BASE,
        appview_base=APPVIEW_BASE,
    )
    first_content = inv_path.read_text(encoding="utf-8")
    first = json.loads(first_content)
    assert len(first["saves"]) == 1

    # Second run: a new bookmark.
    respx.reset()
    _mock_create_session()
    respx.get(f"{PDS_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200,
            json={
                "bookmarks": [
                    _bookmark_record("at://x/p/1"),
                    _bookmark_record("at://x/p/2", saved_at="2026-04-13T00:00:00Z"),
                ]
            },
        )
    )
    _fetch_mod.fetch_to_inventory(
        inv_path,
        handle="user.bsky.social",
        app_password="app-password",
        pds_base=PDS_BASE,
        appview_base=APPVIEW_BASE,
    )
    second_content = inv_path.read_text(encoding="utf-8")
    second = json.loads(second_content)
    assert len(second["saves"]) == 2
    assert second_content != first_content


@respx.mock
def test_fetch_to_inventory_creates_file_on_first_run_with_zero_records(tmp_path):
    """First run with zero bookmarks still creates the inventory file."""
    _mock_create_session()
    respx.get(f"{PDS_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(200, json={"bookmarks": []})
    )
    respx.get(f"{PDS_BASE}/xrpc/com.atproto.repo.describeRepo").mock(
        return_value=httpx.Response(200, json={"collections": []})
    )

    inv_path = tmp_path / "inv.json"
    _fetch_mod.fetch_to_inventory(
        inv_path,
        handle="user.bsky.social",
        app_password="app-password",
        pds_base=PDS_BASE,
        appview_base=APPVIEW_BASE,
    )

    assert inv_path.exists()
    data = json.loads(inv_path.read_text(encoding="utf-8"))
    assert data["saves"] == []
    assert data["fetched_at"] is not None


# --- Progress output format tests ---

import sys


@respx.mock
def test_progress_non_tty_emits_one_line_per_page(capsys, monkeypatch):
    """Non-TTY (CI/pipe) mode emits one `progress: N` line per page,
    plus a single endpoint announcement line."""
    monkeypatch.setattr("bsky_saves.fetch._stderr_is_tty", lambda: False)

    session = _mock_session()
    _mock_service_auth_ok()
    respx.get(f"{APPVIEW_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "bookmarks": [{"uri": f"at://x/p/{i}", "indexedAt": "2026-04-12T00:00:00Z"} for i in range(100)],
                    "cursor": "p2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "bookmarks": [{"uri": f"at://x/p/{i}", "indexedAt": "2026-04-12T00:00:00Z"} for i in range(100, 178)],
                },
            ),
        ]
    )

    fetch.probe_bookmark_endpoints(
        session, pds_base=PDS_BASE, appview_base=APPVIEW_BASE
    )

    err = capsys.readouterr().err
    # Endpoint line announced exactly once
    assert err.count("pds:app.bsky.bookmark.getBookmarks -> 200") == 1
    # Per-page progress lines
    assert "bsky-saves: progress: 100\n" in err
    assert "bsky-saves: progress: 178\n" in err


@respx.mock
def test_progress_tty_uses_in_place_carriage_return(capsys, monkeypatch):
    """TTY mode rewrites a single line with growing comma-separated totals."""
    monkeypatch.setattr("bsky_saves.fetch._stderr_is_tty", lambda: True)

    session = _mock_session()
    _mock_service_auth_ok()
    respx.get(f"{APPVIEW_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "bookmarks": [{"uri": f"at://x/p/{i}", "indexedAt": "2026-04-12T00:00:00Z"} for i in range(100)],
                    "cursor": "p2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "bookmarks": [{"uri": f"at://x/p/{i}", "indexedAt": "2026-04-12T00:00:00Z"} for i in range(100, 178)],
                },
            ),
        ]
    )

    fetch.probe_bookmark_endpoints(
        session, pds_base=PDS_BASE, appview_base=APPVIEW_BASE
    )

    err = capsys.readouterr().err
    # In-place CR-prefixed updates
    assert "\rbsky-saves: progress: 100" in err
    assert "\rbsky-saves: progress: 100, 178" in err
    # Should NOT see the per-page-line non-TTY format
    assert "bsky-saves: progress: 100\n" not in err
    assert "bsky-saves: progress: 178\n" not in err


@respx.mock
def test_progress_single_page_no_pagination(capsys, monkeypatch):
    """A single-page response still emits one progress line."""
    monkeypatch.setattr("bsky_saves.fetch._stderr_is_tty", lambda: False)

    session = _mock_session()
    _mock_service_auth_ok()
    respx.get(f"{APPVIEW_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        return_value=httpx.Response(
            200,
            json={"bookmarks": [{"uri": "at://x/1", "indexedAt": "2026-04-12T00:00:00Z"}]},
        )
    )

    fetch.probe_bookmark_endpoints(
        session, pds_base=PDS_BASE, appview_base=APPVIEW_BASE
    )

    err = capsys.readouterr().err
    assert "pds:app.bsky.bookmark.getBookmarks -> 200" in err
    assert "bsky-saves: progress: 1\n" in err


@respx.mock
def test_progress_tty_terminates_with_newline_before_error(capsys, monkeypatch):
    """If pagination fails mid-walk in TTY mode, the in-place line is
    terminated with a newline before the error line is printed."""
    monkeypatch.setattr("bsky_saves.fetch._stderr_is_tty", lambda: True)

    session = _mock_session()
    _mock_service_auth_ok()
    # PDS:bookmark.getBookmarks: page 1 succeeds, page 2 fails with 500.
    # Then code falls through to the next endpoints, which all 404.
    respx.get(f"{APPVIEW_BASE}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "bookmarks": [{"uri": "at://x/1", "indexedAt": "2026-04-12T00:00:00Z"}],
                    "cursor": "p2",
                },
            ),
            httpx.Response(500, json={"error": "InternalServerError"}),
            # appview retry of same URL — 404 to fall through.
            httpx.Response(404, json={"error": "MethodNotImplemented"}),
        ]
    )
    respx.get(f"{APPVIEW_BASE}/xrpc/app.bsky.feed.getActorBookmarks").mock(
        return_value=httpx.Response(404, json={"error": "MethodNotImplemented"})
    )
    respx.get(f"{PDS_BASE}/xrpc/com.atproto.repo.listRecords").mock(
        return_value=httpx.Response(404, json={"error": "MethodNotImplemented"})
    )

    with pytest.raises(fetch.NoBookmarkEndpointError):
        fetch.probe_bookmark_endpoints(
            session, pds_base=PDS_BASE, appview_base=APPVIEW_BASE
        )

    err = capsys.readouterr().err
    # In-place progress line was terminated before the error printed.
    idx_progress = err.find("\rbsky-saves: progress: 1")
    idx_error = err.find("-> 500")
    assert idx_progress != -1
    assert idx_error != -1
    assert idx_progress < idx_error
    # There must be a newline between them.
    assert "\n" in err[idx_progress:idx_error]
