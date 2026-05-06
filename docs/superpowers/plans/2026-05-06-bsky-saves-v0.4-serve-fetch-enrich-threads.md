# bsky-saves v0.4 — `serve` adds `/fetch`, `/enrich`, `/hydrate-threads` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing `bsky-saves serve` daemon with three new HTTP endpoints — `POST /fetch` (paginated bookmark enumeration), `POST /enrich` (offline TID-decoded `post_created_at`), `POST /hydrate-threads` (concurrent same-author thread fetcher) — letting `bsky-saves-gui` route those operations through the helper instead of running them in Pyodide.

**Architecture:** All new endpoint code in the existing `src/bsky_saves/serve.py`. One new public-ish helper `fetch_one_page` added to `src/bsky_saves/fetch.py` for single-page bookmark granularity (existing `probe_bookmark_endpoints` and `fetch_to_inventory` unchanged). Pagination cursor format is `urlsafe-base64(JSON)` — opaque to the GUI, decoded by the daemon. `/hydrate-threads` fan-out uses `concurrent.futures.ThreadPoolExecutor(max_workers=5)` over sync `httpx`. No new dependencies.

**Tech Stack:** Python 3.11+, stdlib `http.server` + `concurrent.futures` + `base64`, existing `httpx` (sync), `pytest`, `respx` (existing dev dep).

**Spec:** `docs/superpowers/specs/2026-05-06-bsky-saves-v0.4-serve-fetch-enrich-threads.md`.
**External contract (HTTP API):** `https://github.com/tenorune/bsky-saves-gui/blob/main/docs/bsky-saves-serve-fetch-enrich-threads-requirements.md`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/bsky_saves/serve.py` | Modify | Add three endpoint handlers (`_handle_fetch`, `_handle_enrich`, `_handle_hydrate_threads`), three helpers (`_validate_creds`, `_encode_cursor`, `_decode_cursor`), local `_now_iso`. Update `_handle_ping`'s features list. Update `ROUTES` dispatch table. New imports for `auth`, `fetch`, `normalize`, `threads`, `tid` symbols. Add `APPVIEW_BASE` and `PUBLIC_APPVIEW` constants. |
| `src/bsky_saves/fetch.py` | Modify | Add `ENDPOINT_IDS` mapping, `_DirectEndpointFailedError` sentinel exception class, and `fetch_one_page(session, *, pds_base, appview_base, endpoint_id, cursor, limit) -> tuple[str, list[dict], str | None]` helper. Existing `probe_bookmark_endpoints` and `fetch_to_inventory` unchanged. |
| `tests/test_serve.py` | Modify | Append per-endpoint tests: helpers (10 tests), `/fetch` (10 tests), `/enrich` (6 tests), `/hydrate-threads` (10 tests); update one existing `/ping` test. |
| `pyproject.toml` | Modify | Bump `version = "0.4.0"`. |

No new files in `src/`. No new test files. No new dependencies. The existing `tests/conftest.py`, `test_articles.py`, `test_enrich.py`, `test_fetch.py`, `test_images.py`, `test_normalize.py`, `test_threads.py`, `test_tid.py`, `test_version.py` are unchanged.

---

## Task 1: Create the v0.4 branch

**Files:** none (git operations only).

- [ ] **Step 1: Switch to main and pull latest.**

```bash
cd /home/user/bsky-saves
git checkout main
git pull origin main
```

Expected: branch is `main`, up to date.

- [ ] **Step 2: Create the v0.4 branch.**

```bash
git checkout -b v0.4
```

Expected: `Switched to a new branch 'v0.4'`.

- [ ] **Step 3: Push the empty branch to origin to establish tracking.**

```bash
git push -u origin v0.4
```

Expected: `branch 'v0.4' set up to track 'origin/v0.4'`.

---

## Task 2: Refactor `fetch.py` — add `ENDPOINT_IDS`, `_DirectEndpointFailedError`, `fetch_one_page`

**Files:**
- Modify: `src/bsky_saves/fetch.py`

This task adds three new symbols to `fetch.py`. It does NOT modify the existing `probe_bookmark_endpoints` or `fetch_to_inventory`. The new helpers are exercised by the `/fetch` integration tests in Task 4 (per the spec, no separate unit-test file is added for `fetch_one_page`).

### Step 1: Add the new imports and `ENDPOINT_IDS` mapping

In `src/bsky_saves/fetch.py`, find the line `BOOKMARK_ENDPOINTS: list[tuple[str, str, EndpointParams]] = [` (near the top of the file, after the imports). Just **above** the `BOOKMARK_ENDPOINTS` definition, add:

```python
# Stable string aliases for BOOKMARK_ENDPOINTS entries — used by serve.py's
# /fetch cursor encoding to remember which endpoint succeeded across paginated
# calls without re-probing each page.
ENDPOINT_IDS: dict[tuple[str, str], str] = {
    ("pds", "app.bsky.bookmark.getBookmarks"): "pds:bookmark.getBookmarks",
    ("appview", "app.bsky.bookmark.getBookmarks"): "appview:bookmark.getBookmarks",
    ("appview", "app.bsky.feed.getActorBookmarks"): "appview:getActorBookmarks",
    ("pds", "com.atproto.repo.listRecords"): "pds:listRecords",
}


class _DirectEndpointFailedError(Exception):
    """Raised by fetch_one_page when the explicitly-named endpoint hard-fails.

    serve.py's /fetch handler catches this to trigger a silent fallback re-probe.
    Distinct from NoBookmarkEndpointError (which is raised after exhausting all
    candidates during a probe).
    """
```

- [ ] **Step 2: Add `fetch_one_page` at the bottom of `fetch.py`**

Append to `src/bsky_saves/fetch.py` (after `fetch_to_inventory`):

```python
def fetch_one_page(
    session: dict,
    *,
    pds_base: str,
    appview_base: str,
    endpoint_id: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
    user_agent: str | None = None,
) -> tuple[str, list[dict], str | None]:
    """Fetch ONE page of bookmarks. Returns (chosen_endpoint_id, raw_records, next_upstream_cursor).

    If ``endpoint_id`` is None, probes BOOKMARK_ENDPOINTS in fallback order until
    one succeeds for a single page; raises NoBookmarkEndpointError if all fail.

    If ``endpoint_id`` is given, looks it up in ENDPOINT_IDS and calls that
    endpoint directly with the given upstream cursor; raises
    _DirectEndpointFailedError if the call hard-fails.

    Used by serve.py's /fetch handler to expose pagination one page at a time
    while remembering which endpoint succeeded inside the cursor.
    """
    pds_base = pds_base.rstrip("/")
    appview_base = appview_base.rstrip("/")
    pds_headers = {"Authorization": f"Bearer {session['accessJwt']}"}
    if user_agent:
        pds_headers["User-Agent"] = user_agent
    did = session["did"]

    # Build a list of candidate (host, method, params_factory, id) to try.
    candidates: list[tuple[str, str, EndpointParams, str]] = []
    if endpoint_id is None:
        # Probe path: try each in fallback order.
        for host, method, params_factory in BOOKMARK_ENDPOINTS:
            eid = ENDPOINT_IDS[(host, method)]
            candidates.append((host, method, params_factory, eid))
    else:
        # Direct path: look up the single endpoint by id.
        for (host, method), eid in ENDPOINT_IDS.items():
            if eid == endpoint_id:
                # Find the matching factory in BOOKMARK_ENDPOINTS.
                factory = next(
                    f for h, m, f in BOOKMARK_ENDPOINTS if h == host and m == method
                )
                candidates = [(host, method, factory, eid)]
                break
        if not candidates:
            raise _DirectEndpointFailedError(f"unknown endpoint_id: {endpoint_id}")

    tried: list[str] = []
    for host, method, params_factory, eid in candidates:
        base = pds_base if host == "pds" else appview_base
        # Service-auth handling — same logic as probe_bookmark_endpoints.
        same_server = pds_base == appview_base
        if host == "pds" or same_server:
            headers = pds_headers
        else:
            try:
                svc_token = get_service_auth(
                    pds_base, session, "did:web:api.bsky.app", method
                )
                headers = {"Authorization": f"Bearer {svc_token}"}
            except ServiceAuthError:
                tried.append(f"{eid}:svc-auth-fail")
                if endpoint_id is not None:
                    raise _DirectEndpointFailedError("; ".join(tried))
                continue

        params = params_factory(cursor, did)
        params["limit"] = limit
        try:
            r = httpx.get(
                f"{base}/xrpc/{method}",
                params=params,
                headers=headers,
                timeout=30.0,
            )
        except Exception as e:
            tried.append(f"{eid}:{type(e).__name__}")
            if endpoint_id is not None:
                raise _DirectEndpointFailedError("; ".join(tried))
            continue

        if r.status_code in ENDPOINT_FAILURE_CODES:
            tried.append(f"{eid}:{r.status_code}")
            if endpoint_id is not None:
                raise _DirectEndpointFailedError("; ".join(tried))
            continue

        # Success path.
        r.raise_for_status()
        data = r.json()
        page = _records_from_response(data)
        next_cursor = data.get("cursor")
        return eid, page, (next_cursor or None)

    raise NoBookmarkEndpointError(
        "All bookmark endpoints failed: " + "; ".join(tried)
    )
```

- [ ] **Step 3: Quick smoke check — import the new symbols from a Python REPL**

```bash
cd /home/user/bsky-saves
python -c "
from bsky_saves.fetch import (
    fetch_one_page,
    ENDPOINT_IDS,
    _DirectEndpointFailedError,
    NoBookmarkEndpointError,
)
print('OK')
print(sorted(ENDPOINT_IDS.values()))
"
```

Expected:
```
OK
['appview:bookmark.getBookmarks', 'appview:getActorBookmarks', 'pds:bookmark.getBookmarks', 'pds:listRecords']
```

- [ ] **Step 4: Run the full existing test suite to confirm no regression**

```bash
python -m pytest tests/ -v
```

Expected: 101 tests pass (same as v0.3.1 baseline; we haven't added tests yet).

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/fetch.py
git commit -m "refactor(fetch): add fetch_one_page helper for single-page granularity

Adds ENDPOINT_IDS mapping (stable string aliases for BOOKMARK_ENDPOINTS
entries), _DirectEndpointFailedError sentinel, and fetch_one_page()
which fetches ONE page either by probing BOOKMARK_ENDPOINTS in fallback
order (endpoint_id=None) or by calling a specifically-named endpoint
(endpoint_id given). Existing probe_bookmark_endpoints and
fetch_to_inventory are unchanged.

Used by the upcoming v0.4 serve /fetch endpoint to expose pagination
one page at a time while remembering which endpoint succeeded inside
the opaque cursor it returns to the GUI.

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 3: Add helpers to `serve.py` — `_validate_creds`, `_encode_cursor`, `_decode_cursor`

**Files:**
- Modify: `src/bsky_saves/serve.py`
- Test: `tests/test_serve.py`

These three helpers are reusable across `/fetch` and `/hydrate-threads`. They're testable in isolation, so we TDD them first before any endpoint wiring.

### Step 1: Append failing tests to `tests/test_serve.py`

Append (do not overwrite existing tests):

```python
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
```

### Step 2: Run the tests to verify they fail

```bash
python -m pytest tests/test_serve.py -v -k "validate_creds or encode_cursor or decode_cursor"
```

Expected: ImportError on `_validate_creds`, `_encode_cursor`, or `_decode_cursor` (helpers don't exist yet).

### Step 3: Add the helpers to `src/bsky_saves/serve.py`

In `src/bsky_saves/serve.py`, near the top of the file (after the existing imports but before `ROUTES`), add the new imports and helpers:

```python
import base64

from .fetch import ENDPOINT_IDS
```

Then, **after the `_HandlerLike` class** (which is currently around line ~30) and **before** the `_handle_ping` function, add these three helper functions:

```python
DEFAULT_PDS = "https://bsky.social"


def _validate_creds(creds: object) -> dict | None:
    """Validate a credentials dict from a request body.

    Required fields: handle, app_password (both must be non-empty strings).
    Optional field: pds (defaults to "https://bsky.social" when absent or empty).

    Returns a normalized dict with all three fields populated, or None if
    required fields are missing / wrong type.
    """
    if not isinstance(creds, dict):
        return None
    handle = creds.get("handle")
    app_password = creds.get("app_password")
    if not isinstance(handle, str) or not handle:
        return None
    if not isinstance(app_password, str) or not app_password:
        return None
    pds = creds.get("pds")
    if not isinstance(pds, str) or not pds:
        pds = DEFAULT_PDS
    return {"handle": handle, "app_password": app_password, "pds": pds}


def _encode_cursor(endpoint_id: str, upstream_cursor: str | None) -> str:
    """Encode an opaque pagination cursor for /fetch.

    Format: urlsafe-base64(JSON({v: 1, endpoint, upstream})).
    The GUI MUST treat this as fully opaque and round-trip it byte-for-byte.
    """
    payload = {"v": 1, "endpoint": endpoint_id, "upstream": upstream_cursor}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(wrapped: str) -> dict | None:
    """Decode an opaque pagination cursor produced by _encode_cursor.

    Returns the {v, endpoint, upstream} dict on success; None if the cursor
    is corrupted, malformed, has an unknown schema version, or names an
    unknown endpoint id.
    """
    if not isinstance(wrapped, str) or not wrapped:
        return None
    try:
        raw = base64.urlsafe_b64decode(wrapped.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("v") != 1:
        return None
    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, str):
        return None
    if endpoint not in ENDPOINT_IDS.values():
        return None
    upstream = payload.get("upstream")
    if upstream is not None and not isinstance(upstream, str):
        return None
    return {"v": 1, "endpoint": endpoint, "upstream": upstream}
```

### Step 4: Run the tests to verify they pass

```bash
python -m pytest tests/test_serve.py -v -k "validate_creds or encode_cursor or decode_cursor"
```

Expected: 12 tests pass (the 12 helper tests added in Step 1).

### Step 5: Run the full test suite

```bash
python -m pytest tests/ -v
```

Expected: 113 tests pass (101 baseline + 12 new helper tests).

### Step 6: Commit

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): add _validate_creds, _encode_cursor, _decode_cursor helpers

Three new module-level helpers used by the upcoming v0.4 endpoints:

- _validate_creds: parses a credentials dict, defaults pds to bsky.social
  when omitted or empty, returns None on malformed input.
- _encode_cursor: produces an opaque urlsafe-base64(JSON({v,endpoint,upstream}))
  pagination token for /fetch.
- _decode_cursor: round-trips an encoded cursor; returns None for corrupted,
  unknown-version, or unknown-endpoint-id payloads.

Tests cover the round-trip, default-pds behavior, and rejection of malformed
or wrong-schema-version cursors.

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 4: Implement `POST /fetch` endpoint

**Files:**
- Modify: `src/bsky_saves/serve.py`
- Test: `tests/test_serve.py`

This is the largest task. We add 10 integration tests, then the `_handle_fetch` handler and route registration.

### Step 1: Append failing tests to `tests/test_serve.py`

Append:

```python
# --- /fetch endpoint ---

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
    # Continuation cursor names pds:bookmark.getBookmarks.
    cursor = _encode_cursor("pds:bookmark.getBookmarks", "upstream-x")
    # Mock pds:bookmark.getBookmarks to fail (any 4xx in failure codes works).
    # We need it to fail on the direct call but succeed on the re-probe.
    # respx supports side_effect for sequential responses.
    respx.get(f"{PDS_BASE_TEST}/xrpc/app.bsky.bookmark.getBookmarks").mock(
        side_effect=[
            httpx.Response(500, json={"error": "ServerError"}),  # direct call fails
            httpx.Response(  # re-probe call succeeds
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
            json={"bookmarks": [_bookmark_record_for_fetch("at://x/p/1")]},  # no cursor
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
```

### Step 2: Run the tests to verify they fail

```bash
python -m pytest tests/test_serve.py -v -k "test_fetch_"
```

Expected: 10 tests fail with 404 or import errors (route not registered, helpers possibly missing imports).

### Step 3: Implement `_handle_fetch` and register it in `ROUTES`

In `src/bsky_saves/serve.py`, **at the top of the file with the other imports**, add (some may already be present from earlier tasks; add only the missing ones):

```python
from .auth import create_session
from .fetch import (
    fetch_one_page,
    NoBookmarkEndpointError,
    _DirectEndpointFailedError,
)
from .normalize import normalise_record
```

Add the constant near the top of the file (with other constants like `DEFAULT_PDS`):

```python
APPVIEW_BASE = "https://bsky.social"
```

Then, after `_handle_extract_article` (or `_handle_ping` if `_handle_extract_article` is below it — find a sensible location among the other endpoint handlers), add `_handle_fetch`:

```python
def _handle_fetch(handler) -> None:
    body = handler._read_json_body()
    creds = _validate_creds((body or {}).get("credentials"))
    if creds is None:
        handler._send_json_error(400, "missing credentials")
        return

    raw_cursor = (body or {}).get("cursor")
    raw_limit = (body or {}).get("limit", 100)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 100))

    if raw_cursor is None:
        endpoint_id, upstream_cursor = None, None
    else:
        decoded = _decode_cursor(raw_cursor)
        if decoded is None:
            handler._send_json_error(400, "invalid cursor")
            return
        endpoint_id, upstream_cursor = decoded["endpoint"], decoded["upstream"]

    try:
        session = create_session(
            creds["pds"], creds["handle"], creds["app_password"]
        )
    except httpx.HTTPStatusError as e:
        handler._send_json_error(401, f"createSession failed: {e}")
        return
    except Exception as e:
        handler._send_json_error(502, f"{type(e).__name__}: {str(e)[:200]}")
        return

    try:
        chosen_id, raw, next_upstream = fetch_one_page(
            session,
            pds_base=creds["pds"],
            appview_base=APPVIEW_BASE,
            endpoint_id=endpoint_id,
            cursor=upstream_cursor,
            limit=limit,
        )
    except _DirectEndpointFailedError:
        # Silent fallback: re-probe from a fresh state. Drop the upstream cursor
        # because the four bookmark endpoints have incompatible cursor formats.
        try:
            chosen_id, raw, next_upstream = fetch_one_page(
                session,
                pds_base=creds["pds"],
                appview_base=APPVIEW_BASE,
                endpoint_id=None,
                cursor=None,
                limit=limit,
            )
        except NoBookmarkEndpointError as e:
            handler._send_json_error(502, f"no working bookmark endpoint: {e}")
            return
    except NoBookmarkEndpointError as e:
        handler._send_json_error(502, f"no working bookmark endpoint: {e}")
        return

    saves = [normalise_record(r) for r in raw]
    out_cursor = _encode_cursor(chosen_id, next_upstream) if next_upstream else None
    handler._send_json(200, {"saves": saves, "cursor": out_cursor})
```

Update the `ROUTES` table to register the new endpoint. Find:

```python
ROUTES: dict[tuple[str, str], Callable[["_HandlerLike"], None]] = {
    ("GET", "/ping"): _handle_ping,
    ("POST", "/fetch-image"): _handle_fetch_image,
    ("POST", "/extract-article"): _handle_extract_article,
}
```

And replace with:

```python
ROUTES: dict[tuple[str, str], Callable[["_HandlerLike"], None]] = {
    ("GET", "/ping"): _handle_ping,
    ("POST", "/fetch-image"): _handle_fetch_image,
    ("POST", "/extract-article"): _handle_extract_article,
    ("POST", "/fetch"): _handle_fetch,
}
```

### Step 4: Run the tests to verify they pass

```bash
python -m pytest tests/test_serve.py -v -k "test_fetch_"
```

Expected: 10 tests pass.

### Step 5: Run the full suite

```bash
python -m pytest tests/ -v
```

Expected: 123 tests pass (113 + 10 new `/fetch` tests).

### Step 6: Commit

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): implement POST /fetch with opaque pagination cursor

Adds the /fetch endpoint that paginates a user's bookmarks through any
of the four BOOKMARK_ENDPOINTS (PDS-direct, AppView-bookmark, AppView-
getActorBookmarks, PDS-listRecords) by probing on the first call and
remembering the chosen endpoint inside an opaque urlsafe-base64(JSON)
cursor for subsequent calls.

Per spec, the cursor is the daemon's private contract — the GUI
round-trips it byte-for-byte. Silent endpoint fallback: when a
continuation cursor's named endpoint hard-fails, the daemon re-probes
from a fresh state (cursor dropped, since the four endpoints have
incompatible cursor formats) and returns the next page from whichever
endpoint becomes the new winner. Invisible to the GUI other than a
slight latency bump.

createSession is called once per request; the JWT is used for the
upstream call (bookmark endpoints require auth). pds defaults to
bsky.social when absent. limit is clamped to [1, 100].

10 new tests cover: probe-then-cursor round trip, continuation skips
probe, response shape matches normalise_record, invalid cursor → 400,
missing credentials → 400, pds default, createSession 401 → 401,
silent fallback, null cursor on last page, limit clamping.

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 5: Implement `POST /enrich` endpoint

**Files:**
- Modify: `src/bsky_saves/serve.py`
- Test: `tests/test_serve.py`

### Step 1: Append failing tests to `tests/test_serve.py`

Append:

```python
# --- /enrich endpoint ---


def test_enrich_decodes_post_created_at_for_each_uri():
    """Valid at-URIs with TID rkeys → enriched populated in input order."""
    # Use real TIDs (the same one used in test_threads.py).
    uri1 = "at://did:plc:abc/app.bsky.feed.post/3jzfcijpj2z2a"
    uri2 = "at://did:plc:def/app.bsky.feed.post/3kxyzghi456ab"  # may decode or fail; we'll catch
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
    # post_created_at is a non-empty ISO string
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
    # Two valid → enriched in input order; two invalid → errors in input order.
    assert len(payload["enriched"]) == 2
    assert payload["enriched"][0]["uri"] == valid
    assert payload["enriched"][1]["uri"] == valid
    assert len(payload["errors"]) == 2
```

### Step 2: Run the tests to verify they fail

```bash
python -m pytest tests/test_serve.py -v -k "test_enrich_"
```

Expected: 6 tests fail with 404 (route not registered).

### Step 3: Implement `_handle_enrich` and register it

In `src/bsky_saves/serve.py`, add the import near the top (with other `from .` lines):

```python
from .tid import rkey_of, decode_tid_to_iso
```

Add the handler near `_handle_fetch`:

```python
def _handle_enrich(handler) -> None:
    body = handler._read_json_body()
    uris = (body or {}).get("uris")
    if not isinstance(uris, list):
        handler._send_json_error(400, "missing uris")
        return

    enriched: list[dict] = []
    errors: list[dict] = []
    for uri in uris:
        if not isinstance(uri, str) or not uri:
            errors.append({
                "uri": uri if isinstance(uri, str) else "",
                "reason": "invalid at-uri",
            })
            continue
        try:
            post_created_at = decode_tid_to_iso(rkey_of(uri))
        except Exception:
            errors.append({"uri": uri, "reason": "invalid at-uri"})
            continue
        enriched.append({"uri": uri, "post_created_at": post_created_at})

    handler._send_json(200, {"enriched": enriched, "errors": errors})
```

Update `ROUTES`:

```python
ROUTES: dict[tuple[str, str], Callable[["_HandlerLike"], None]] = {
    ("GET", "/ping"): _handle_ping,
    ("POST", "/fetch-image"): _handle_fetch_image,
    ("POST", "/extract-article"): _handle_extract_article,
    ("POST", "/fetch"): _handle_fetch,
    ("POST", "/enrich"): _handle_enrich,
}
```

### Step 4: Run the tests to verify they pass

```bash
python -m pytest tests/test_serve.py -v -k "test_enrich_"
```

Expected: 6 tests pass.

### Step 5: Run the full suite

```bash
python -m pytest tests/ -v
```

Expected: 129 tests pass (123 + 6 new).

### Step 6: Commit

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): implement POST /enrich (offline TID decode)

Per the spec, /enrich is a thin wrapper over bsky-saves's existing
offline enrich logic — for each at-URI in the request, decode the
post_created_at ISO timestamp from the URI's record-key TID. No
network calls, no credentials required, sub-second response time.

Per-URI failures (empty / non-string / malformed at-URI / TID decode
error) land in the response's errors[] array with the static reason
string 'invalid at-uri', matching the requirements doc's example.
Other URIs in the same request are unaffected.

A request body that includes 'credentials' is accepted (no 400); the
field is silently ignored. Empty 'uris' list returns 200 with empty
arrays. Missing 'uris' top-level field returns 400 missing uris.

6 new tests cover: happy path, invalid URIs, missing/empty uris,
credentials-ignored, mixed valid/invalid input-order preservation.

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 6: Implement `POST /hydrate-threads` endpoint

**Files:**
- Modify: `src/bsky_saves/serve.py`
- Test: `tests/test_serve.py`

### Step 1: Append failing tests to `tests/test_serve.py`

Append:

```python
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
    # Two URIs; mock each to a distinct getPostThread response.
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
    # OP's post with one self-continuation reply (collected) and one comment-by-other
    # whose own reply by OP should NOT be collected.
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
    # Only the unbroken-chain self-continuation is collected.
    reply_uris = [r["uri"] for r in entry["thread_replies"]]
    assert reply_uris == ["at://op/cont"]


@respx.mock
def test_hydrate_threads_per_uri_failure_lands_in_errors_with_diagnostic():
    _mock_fetch_create_session()
    # First call: 404. Second call: success.
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread").mock(
        side_effect=[
            httpx.Response(404, json={"error": "NotFound"}),
            httpx.Response(
                200,
                json=_thread_view_post("at://x/p/2", "did:plc:x", "ok"),
            ),
        ]
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
    # First URI failed (in errors); second succeeded (in threaded).
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
        # Sleep briefly to force overlap between threads.
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


def test_hydrate_threads_invalid_uri_in_input():
    """Non-string / empty-string entries → errors[] with reason 'invalid at-uri'."""
    with serve_in_background() as (port, _):
        # No mocks needed because all URIs are invalid; we'll get a missing-creds 400 first.
        # So we DO need to send valid creds; but no upstream getPostThread calls happen.
        # Wait — invalid URIs DON'T trigger getPostThread; only valid ones do. We need to
        # short-circuit createSession too.
        pass
    # Mock createSession; no upstream getPostThread calls expected.
    _setup_for_invalid_uri_test_in_hydrate_threads()  # placeholder; inline below


@respx.mock
def test_hydrate_threads_invalid_uri_in_input_actual():
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
```

**Note**: remove the placeholder `test_hydrate_threads_invalid_uri_in_input` and the `_setup_for_invalid_uri_test_in_hydrate_threads()` reference. Use only `test_hydrate_threads_invalid_uri_in_input_actual` (rename it to `test_hydrate_threads_invalid_uri_in_input` after deleting the placeholder).

### Step 2: Run the tests to verify they fail

```bash
python -m pytest tests/test_serve.py -v -k "test_hydrate_threads_"
```

Expected: 10 tests fail with 404 (route not registered).

### Step 3: Implement `_handle_hydrate_threads` and register it

In `src/bsky_saves/serve.py`, add imports near the top:

```python
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .threads import (
    fetch_thread,
    collect_same_author_replies,
    THREAD_SCHEMA_VERSION,
)
```

Add the constant near other constants:

```python
PUBLIC_APPVIEW = "https://public.api.bsky.app"
```

Add a local `_now_iso` helper near other helpers (top of file, after the existing helpers):

```python
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

Add the handler near `_handle_enrich`:

```python
def _handle_hydrate_threads(handler) -> None:
    body = handler._read_json_body()
    creds = _validate_creds((body or {}).get("credentials"))
    if creds is None:
        handler._send_json_error(400, "missing credentials")
        return
    uris = (body or {}).get("uris")
    if not isinstance(uris, list):
        handler._send_json_error(400, "missing uris")
        return

    # Validate credentials; we don't use the resulting JWT for upstream calls.
    try:
        create_session(creds["pds"], creds["handle"], creds["app_password"])
    except httpx.HTTPStatusError as e:
        handler._send_json_error(401, f"createSession failed: {e}")
        return
    except Exception as e:
        handler._send_json_error(502, f"{type(e).__name__}: {str(e)[:200]}")
        return

    def fetch_one(uri: str) -> tuple[str, dict | None, str | None]:
        thread, error = fetch_thread(uri, appview=PUBLIC_APPVIEW)
        if thread is None:
            return uri, None, error or "thread fetch failed"
        post_author_did = ""
        if isinstance(thread, dict):
            post_author_did = (
                thread.get("post", {}).get("author", {}).get("did", "")
            )
        replies = collect_same_author_replies(thread, post_author_did)
        return uri, {
            "uri": uri,
            "thread_replies": replies,
            "thread_schema_version": THREAD_SCHEMA_VERSION,
            "thread_fetched_at": _now_iso(),
        }, None

    threaded: list[dict] = []
    errors: list[dict] = []
    scheduled: list[tuple[str, object]] = []  # (uri, Future-or-"invalid")

    with ThreadPoolExecutor(max_workers=5) as pool:
        for u in uris:
            if isinstance(u, str) and u:
                scheduled.append((u, pool.submit(fetch_one, u)))
            else:
                scheduled.append((u if isinstance(u, str) else "", "invalid"))

        for u, fut_or_marker in scheduled:
            if fut_or_marker == "invalid":
                errors.append({"uri": u, "reason": "invalid at-uri"})
                continue
            _, entry, err = fut_or_marker.result()
            if entry is not None:
                threaded.append(entry)
            else:
                errors.append({"uri": u, "reason": err or "thread fetch failed"})

    handler._send_json(200, {"threaded": threaded, "errors": errors})
```

Update `ROUTES`:

```python
ROUTES: dict[tuple[str, str], Callable[["_HandlerLike"], None]] = {
    ("GET", "/ping"): _handle_ping,
    ("POST", "/fetch-image"): _handle_fetch_image,
    ("POST", "/extract-article"): _handle_extract_article,
    ("POST", "/fetch"): _handle_fetch,
    ("POST", "/enrich"): _handle_enrich,
    ("POST", "/hydrate-threads"): _handle_hydrate_threads,
}
```

### Step 4: Run the tests to verify they pass

```bash
python -m pytest tests/test_serve.py -v -k "test_hydrate_threads_"
```

Expected: 10 tests pass.

### Step 5: Run the full suite

```bash
python -m pytest tests/ -v
```

Expected: 139 tests pass (129 + 10 new).

### Step 6: Commit

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): implement POST /hydrate-threads with concurrent fan-out

Adds the /hydrate-threads endpoint. Per the spec: validates credentials
via createSession at request entry (fail-fast on bad app password) but
discards the resulting JWT, then fetches each URI's thread from the
public AppView (https://public.api.bsky.app/xrpc/...) unauthenticated —
matching the bsky-saves CLI's working pattern.

Concurrency: ThreadPoolExecutor(max_workers=5) per request. Results are
walked in input-URI order so both threaded[] and errors[] preserve the
caller's order. Per-URI failures use the underlying fetch_thread error
string (e.g., 'http_404', 'fetch_error:ConnectError:...') when
available, falling back to 'thread fetch failed' otherwise. Non-string
or empty-string inputs produce 'invalid at-uri' errors.

Each entry's thread_replies is the same shape bsky-saves writes to
inventory JSON, with thread_schema_version=4 (after v0.3.1's
chain-broken fix).

10 new tests cover: input-order preservation, v4 chain logic,
per-URI 404 → diagnostic error reason, createSession invocation,
401 on bad creds → no upstream calls, public AppView
unauthenticated, concurrency capped at 5, invalid URI handling,
missing credentials, missing uris.

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 7: Update `/ping` features array

**Files:**
- Modify: `src/bsky_saves/serve.py`
- Test: `tests/test_serve.py`

### Step 1: Update the `_handle_ping` function

In `src/bsky_saves/serve.py`, find `_handle_ping`:

```python
def _handle_ping(handler) -> None:
    handler._send_json(
        200,
        {
            "name": "bsky-saves",
            "version": __version__,
            "features": ["fetch-image", "extract-article"],
        },
    )
```

Replace the `features` list:

```python
def _handle_ping(handler) -> None:
    handler._send_json(
        200,
        {
            "name": "bsky-saves",
            "version": __version__,
            "features": ["fetch-image", "extract-article", "fetch", "enrich", "hydrate-threads"],
        },
    )
```

### Step 2: Update the existing `/ping` test

Find `test_ping_returns_name_version_features` in `tests/test_serve.py`. The current expected `features` list is `["fetch-image", "extract-article"]`. Change it to:

```python
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
```

### Step 3: Run the test to verify it passes

```bash
python -m pytest tests/test_serve.py::test_ping_returns_name_version_features -v
```

Expected: 1 test passes.

### Step 4: Run the full suite

```bash
python -m pytest tests/ -v
```

Expected: 139 tests still pass (no count change; only one test's expected value was updated).

### Step 5: Commit

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): /ping advertises fetch, enrich, hydrate-threads features

Updates the features array to include the three new v0.4 endpoints, so
bsky-saves-gui's feature-detection on /ping picks them up automatically.
The order matches the requirements doc's example for diff readability;
the GUI is documented as not relying on order.

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 8: Bump version to 0.4.0

**Files:**
- Modify: `pyproject.toml`

### Step 1: Bump the version

In `pyproject.toml`, change:

```toml
version = "0.3.1"
```

to:

```toml
version = "0.4.0"
```

### Step 2: Reinstall and verify the version

```bash
cd /home/user/bsky-saves
python -m pip install --quiet -e ".[dev]"
python -c "import bsky_saves; print(bsky_saves.__version__)"
```

Expected: `0.4.0`.

### Step 3: Run the full test suite

```bash
python -m pytest tests/ -v
```

Expected: 139 tests pass.

### Step 4: Commit

```bash
git add pyproject.toml
git commit -m "chore: bump version to 0.4.0

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 9: Final verification gate

**Files:** none (verification only).

This is the gate before pushing the v0.4 branch and creating the v0.4.0 release.

### Step 1: Run the full test suite from a clean state

```bash
cd /home/user/bsky-saves
python -m pytest tests/ -v
```

Expected: 139 tests pass. No failures.

### Step 2: Build sdist and wheel

```bash
rm -rf dist/ build/ src/bsky_saves.egg-info/
python -m build
ls -la dist/
```

Expected: `dist/bsky_saves-0.4.0-py3-none-any.whl` and `dist/bsky_saves-0.4.0.tar.gz` both present.

### Step 3: Smoke-test the wheel in a clean venv

```bash
rm -rf /tmp/v040-smoke
python -m venv /tmp/v040-smoke
/tmp/v040-smoke/bin/pip install dist/bsky_saves-0.4.0-py3-none-any.whl
/tmp/v040-smoke/bin/python -c "import bsky_saves; print('__version__:', bsky_saves.__version__)"
/tmp/v040-smoke/bin/bsky-saves --help
/tmp/v040-smoke/bin/bsky-saves serve --help
```

Expected:
- `__version__: 0.4.0`.
- `bsky-saves --help` lists `serve` among the subcommands.
- `bsky-saves serve --help` shows `--port`, `--allow-origin`, `--verbose` (no new flags — endpoints are HTTP-only).

### Step 4: Live `/ping` smoke test

```bash
/tmp/v040-smoke/bin/bsky-saves serve --port 47829 &
SERVE_PID=$!
sleep 1
echo "--- /ping ---"
curl -sS http://127.0.0.1:47829/ping
echo
echo "--- shutdown ---"
kill $SERVE_PID
wait $SERVE_PID 2>/dev/null
```

Expected: `curl` prints a JSON body matching `{"name": "bsky-saves", "version": "0.4.0", "features": ["fetch-image", "extract-article", "fetch", "enrich", "hydrate-threads"]}` (whitespace varies; substance must match).

### Step 5: Live `/enrich` smoke test (offline, no creds)

```bash
/tmp/v040-smoke/bin/bsky-saves serve --port 47830 &
SERVE_PID=$!
sleep 1
echo "--- /enrich ---"
curl -sS -X POST http://127.0.0.1:47830/enrich \
    -H "Content-Type: application/json" \
    -d '{"uris": ["at://did:plc:abc/app.bsky.feed.post/3jzfcijpj2z2a"]}'
echo
kill $SERVE_PID
wait $SERVE_PID 2>/dev/null
```

Expected: `curl` prints a JSON body with `"enriched"` containing one entry with the URI and a non-empty `post_created_at` ISO string. Errors array empty.

### Step 6: Push the v0.4 branch

```bash
git push -u origin v0.4
```

Expected: branch pushed; tracking established.

### Step 7: Merge to main and push

```bash
git checkout main
git pull origin main
git merge --ff-only v0.4
git push origin main
```

Expected: fast-forward merge succeeds; main contains all v0.4.0 commits.

### Step 8: Delete the local v0.4 branch

```bash
git branch -D v0.4
```

Expected: `Deleted branch v0.4`.

### Step 9: Stop and hand off

After main is updated and the v0.4 dev branch is deleted locally, the implementation is done. Per spec §release strategy:

1. **User action**: delete the `v0.4` branch on GitHub (proxy 403 prevents remote branch deletes from this sandbox).
2. **User action**: create the `v0.4.0` release / tag via the GitHub UI on the new HEAD of `main`. Suggested release title: `v0.4.0 — bsky-saves serve adds /fetch, /enrich, /hydrate-threads`.
3. The `release.yml` workflow publishes `bsky-saves==0.4.0` to PyPI.
4. The `bsky-saves-gui` team can then bump `MIN_HELPER_VERSION` to `0.4.0` and wire helper-routed `fetch` / `enrich` / `hydrate-threads` runners.

Report back to the user:
- All tests green (139 expected).
- Wheel + sdist built.
- v0.4 branch pushed; main fast-forwarded; main pushed; local v0.4 deleted.
- Live smoke confirmed: `/ping` returns the expected features array; `/enrich` decodes a sample TID.
- Awaiting branch deletion + GitHub release creation on the user's side.

---

## Self-review notes

After writing the plan I checked it against the spec:

- **Spec §1 (context):** Plan §header reflects the same context. ✓
- **Spec §2 (scope):** Plan covers all additive items: three new endpoints, fetch_one_page helper, /ping features bump, version bump. No existing-subcommand changes touched. ✓
- **Spec §3 (architecture and module layout):** Tasks 2, 3, 4, 5, 6 collectively touch `src/bsky_saves/fetch.py` and `src/bsky_saves/serve.py` exactly as the spec lists. No new files in `src/`. ✓
- **Spec §4 (CLI surface unchanged):** No task touches `cli.py` or argparse subparsers. ✓
- **Spec §5 (`/fetch`):** Task 4 implements credential validation, cursor encoding, silent fallback (drop cursor, re-probe), createSession-401 mapping, limit clamping, response shape via `normalise_record`. ✓
- **Spec §6 (`/enrich`):** Task 5 implements offline TID decode, no credentials required, `"invalid at-uri"` per-URI reason, sub-second response. ✓
- **Spec §7 (`/hydrate-threads`):** Task 6 implements validate-only credential pattern, `ThreadPoolExecutor(max_workers=5)`, public AppView unauthenticated, input-order preservation, diagnostic error reasons. ✓
- **Spec §8 (`/ping` features array):** Task 7. ✓
- **Spec §9 (test strategy):** Plan tests align with spec test matrix. Helper tests (12 in Task 3) cover `_validate_creds`/`_encode_cursor`/`_decode_cursor` shape; per-endpoint tests in Tasks 4/5/6 cover the spec's listed test cases. The spec's `test_fetch_cursor_round_trips_byte_for_byte` is folded into Task 3's helper tests (since it's testing the helper, not the endpoint integration); plan still has 10 distinct `/fetch` integration tests covering the rest. ✓
- **Spec §10 (forward-compat / phase-2 awareness):** No tasks needed — the design choices that keep `/run` cheap to add (mechanical ROUTES extension, reusable `_validate_creds` and `fetch_one_page` helpers, hardcoded features array, etc.) are baked into the implementation tasks. ✓
- **Spec §11 (out of scope):** No tasks address `/run`, streaming, configurable batch size, OAuth, async refactor. ✓
- **Spec §12 (decisions log):** All decisions reflected in implementation choices. ✓

**Placeholder scan:** None of the no-placeholder rules from the writing-plans skill are violated. Every step has the actual code, command, or expected output it needs. Test code blocks contain complete tests. Implementation blocks contain complete functions. No "TBD," no "similar to Task N," no "add appropriate error handling."

**Type / name consistency:**
- `_validate_creds(obj) -> dict | None` — Task 3 (definition), Task 4 (use), Task 6 (use). Same shape across all.
- `_encode_cursor(endpoint_id, upstream_cursor) -> str` and `_decode_cursor(wrapped) -> dict | None` — Task 3 (definition), Task 4 (use), Task 4 tests (use). Same shape.
- `fetch_one_page(session, *, pds_base, appview_base, endpoint_id, cursor, limit) -> tuple[str, list[dict], str | None]` — Task 2 (definition), Task 4 (use). Same signature.
- `_DirectEndpointFailedError`, `NoBookmarkEndpointError` — Task 2 (definitions), Task 4 (catches). Same names.
- `ENDPOINT_IDS` — Task 2 (definition), Task 3 (used in `_decode_cursor`). Same import path.
- Constants `APPVIEW_BASE`, `PUBLIC_APPVIEW`, `DEFAULT_PDS` — Task 4 (intro APPVIEW_BASE), Task 3 (intro DEFAULT_PDS), Task 6 (intro PUBLIC_APPVIEW). All module-level in `serve.py`.
- `_handle_fetch`, `_handle_enrich`, `_handle_hydrate_threads`, `_handle_ping`, `_handle_fetch_image`, `_handle_extract_article` — Task 4/5/6 add new ones; Task 7 modifies `_handle_ping`. All in `ROUTES` table.
- `_now_iso()` — Task 6 (definition in serve.py). Local; doesn't conflict with the same-named helpers in articles.py / threads.py / images.py.

No fixes needed.

**Test count cross-check:**
- Baseline: 101 tests (after v0.3.1).
- After Task 3: 101 + 12 helper tests = 113.
- After Task 4: 113 + 10 `/fetch` tests = 123.
- After Task 5: 123 + 6 `/enrich` tests = 129.
- After Task 6: 129 + 10 `/hydrate-threads` tests = 139.
- After Task 7: 139 (no count change).
- After Task 8: 139.

Spec §9 said "~127 tests passing" — plan ends at 139, which is within the spec's softness target.
