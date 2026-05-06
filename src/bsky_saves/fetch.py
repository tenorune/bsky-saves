"""Fetch BlueSky bookmarks via app-password authentication.

Probes several bookmark-related XRPC endpoints in fallback order:

1. PDS-direct ``app.bsky.bookmark.getBookmarks`` — the active path for
   third-party PDS accounts (e.g. eurosky.social). Calls the user's PDS
   directly, authenticated with the same session JWT.
2. AppView ``app.bsky.bookmark.getBookmarks`` — used by bsky.social-hosted
   accounts. Requires service-auth tokens for cross-server calls when
   PDS != AppView (which then often fails for third-party PDSes).
3. AppView ``app.bsky.feed.getActorBookmarks`` — older AppView endpoint.
4. PDS ``com.atproto.repo.listRecords`` for ``app.bsky.bookmark`` —
   raw-record fallback. Returns URI references only (no hydrated content).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

from .auth import ServiceAuthError, create_session, get_service_auth
from .normalize import merge_into_inventory, normalise_record


EndpointParams = Callable[[str | None, str], dict]


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

    serve.py's /fetch handler catches this to trigger a silent fallback re-probe
    or, in the JWT-pair credential path, a refreshSession + retry on 401.

    Distinct from NoBookmarkEndpointError (which is raised after exhausting all
    candidates during a probe).

    Attributes:
        status_code: the HTTP status code from the upstream response, or None
            if the failure was a transport-level error (httpx exception,
            service-auth error, etc.).
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


BOOKMARK_ENDPOINTS: list[tuple[str, str, EndpointParams]] = [
    (
        "pds",
        "app.bsky.bookmark.getBookmarks",
        lambda cursor, did: {"limit": 100, **({"cursor": cursor} if cursor else {})},
    ),
    (
        "appview",
        "app.bsky.bookmark.getBookmarks",
        lambda cursor, did: {"limit": 100, **({"cursor": cursor} if cursor else {})},
    ),
    (
        "appview",
        "app.bsky.feed.getActorBookmarks",
        lambda cursor, did: {"actor": did, "limit": 100, **({"cursor": cursor} if cursor else {})},
    ),
    (
        "pds",
        "com.atproto.repo.listRecords",
        lambda cursor, did: {
            "repo": did,
            "collection": "app.bsky.bookmark",
            "limit": 100,
            **({"cursor": cursor} if cursor else {}),
        },
    ),
]

ENDPOINT_FAILURE_CODES = {400, 401, 403, 404, 500, 501, 502, 503, 504}

DEFAULT_APPVIEW_DID_CANDIDATES = [
    "did:web:api.bsky.app",
    "did:web:bsky.app",
    "did:web:bsky.social",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stderr_is_tty() -> bool:
    return sys.stderr.isatty()


class NoBookmarkEndpointError(Exception):
    """All probed bookmark endpoints failed.

    Attributes:
        status_codes: list of HTTP status codes observed across the probe
            attempts. May be empty if all failures were transport-level.
    """

    def __init__(self, message: str, status_codes: list[int] | None = None):
        super().__init__(message)
        self.status_codes = list(status_codes or [])


def _records_from_response(data: dict) -> list[dict]:
    for key in ("bookmarks", "records", "feed"):
        if key in data and isinstance(data[key], list):
            return data[key]
    return []


def probe_bookmark_endpoints(
    session: dict,
    *,
    pds_base: str,
    appview_base: str,
    appview_did_candidates: list[str] | None = None,
) -> tuple[str, list[dict]]:
    """Try each endpoint in BOOKMARK_ENDPOINTS until one returns 200.

    Returns (endpoint_name, list_of_raw_records).
    Raises NoBookmarkEndpointError listing each (endpoint, aud, status_code)
    that was tried and failed.
    """
    pds_base = pds_base.rstrip("/")
    appview_base = appview_base.rstrip("/")
    candidates = appview_did_candidates or DEFAULT_APPVIEW_DID_CANDIDATES

    pds_headers = {"Authorization": f"Bearer {session['accessJwt']}"}
    did = session["did"]
    tried: list[str] = []
    status_codes: list[int] = []

    same_server = pds_base == appview_base

    for host, method, params_factory in BOOKMARK_ENDPOINTS:
        base = pds_base if host == "pds" else appview_base
        if host == "pds" or same_server:
            candidate_auds: list[str | None] = [None]
        else:
            candidate_auds = list(candidates)

        give_up_on_endpoint = False

        for candidate_aud in candidate_auds:
            if give_up_on_endpoint:
                break

            if host == "pds" or candidate_aud is None:
                headers = pds_headers
            else:
                try:
                    svc_token = get_service_auth(pds_base, session, candidate_aud, method)
                    headers = {"Authorization": f"Bearer {svc_token}"}
                except ServiceAuthError as e:
                    print(
                        f"bsky-saves:   {host}:{method} aud={candidate_aud} -> "
                        f"service-auth failed: {e}",
                        file=sys.stderr,
                    )
                    tried.append(f"{host}:{method} aud={candidate_aud} -> svc-auth-fail")
                    continue

            records: list[dict] = []
            cursor: str | None = None
            invalid_token = False
            request_failed = False
            endpoint_announced = False
            progress_totals: list[int] = []
            progress_in_place_active = False

            aud_tag = "" if host == "pds" else f" aud={candidate_aud}"
            endpoint_label = f"bsky-saves:   {host}:{method}{aud_tag}"

            while True:
                params = params_factory(cursor, did)
                r = httpx.get(
                    f"{base}/xrpc/{method}",
                    params=params,
                    headers=headers,
                    timeout=30.0,
                )
                body: object = None
                is_error = r.status_code >= 400
                if is_error:
                    try:
                        body = r.json()
                    except ValueError:
                        body = {"raw": r.text[:500]}

                    if progress_in_place_active:
                        sys.stderr.write("\n")
                        sys.stderr.flush()
                        progress_in_place_active = False
                    print(
                        f"{endpoint_label} -> {r.status_code}  body={body}",
                        file=sys.stderr,
                    )

                    if (
                        host != "pds"
                        and r.status_code == 400
                        and isinstance(body, dict)
                        and body.get("error") == "InvalidToken"
                    ):
                        invalid_token = True
                        break

                    if r.status_code in ENDPOINT_FAILURE_CODES:
                        tried.append(f"{host}:{method}{aud_tag} -> {r.status_code}")
                        status_codes.append(r.status_code)
                        request_failed = True
                        break

                r.raise_for_status()
                data = r.json()
                page = _records_from_response(data)
                records.extend(page)

                if not endpoint_announced:
                    print(
                        f"{endpoint_label} -> {r.status_code}",
                        file=sys.stderr,
                    )
                    endpoint_announced = True

                progress_totals.append(len(records))
                if _stderr_is_tty():
                    line = "bsky-saves: progress: " + ", ".join(
                        str(n) for n in progress_totals
                    )
                    sys.stderr.write("\r" + line)
                    sys.stderr.flush()
                    progress_in_place_active = True
                else:
                    print(
                        f"bsky-saves: progress: {len(records)}",
                        file=sys.stderr,
                    )

                cursor = data.get("cursor")
                if not cursor or not page:
                    break

            if progress_in_place_active:
                sys.stderr.write("\n")
                sys.stderr.flush()
                progress_in_place_active = False

            if invalid_token:
                continue
            if request_failed:
                give_up_on_endpoint = True
                break
            return method, records

    raise NoBookmarkEndpointError(
        "All bookmark endpoints failed: " + "; ".join(tried),
        status_codes=status_codes,
    )


def list_repo_collections(session: dict, *, pds_base: str) -> list[str]:
    """Diagnostic helper: list collection names in the user's PDS repo."""
    headers = {"Authorization": f"Bearer {session['accessJwt']}"}
    r = httpx.get(
        f"{pds_base.rstrip('/')}/xrpc/com.atproto.repo.describeRepo",
        params={"repo": session["did"]},
        headers=headers,
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()
    return list(data.get("collections", []))


def fetch_to_inventory(
    inventory_path: Path,
    *,
    handle: str,
    app_password: str,
    pds_base: str = "https://bsky.social",
    appview_base: str = "https://bsky.social",
    appview_did_candidates: list[str] | None = None,
) -> int:
    """High-level: authenticate, probe, normalise, merge into inventory file.
    Returns the number of saves in the resulting inventory.
    """
    print(f"bsky-saves: authenticating as {handle}", file=sys.stderr)
    session = create_session(pds_base, handle, app_password)

    print("bsky-saves: probing bookmark endpoints", file=sys.stderr)
    endpoint, raw = probe_bookmark_endpoints(
        session,
        pds_base=pds_base,
        appview_base=appview_base,
        appview_did_candidates=appview_did_candidates,
    )
    print(
        f"bsky-saves: used {endpoint} ({len(raw)} raw records)",
        file=sys.stderr,
    )

    if not raw:
        try:
            collections = list_repo_collections(session, pds_base=pds_base)
            print(
                f"bsky-saves: 0 records — collections in your PDS repo: {collections}",
                file=sys.stderr,
            )
        except Exception as e:
            print(
                f"bsky-saves: 0 records, and describeRepo also failed: {e}",
                file=sys.stderr,
            )

    new_entries = [normalise_record(r) for r in raw]

    first_run = not inventory_path.exists()
    if first_run:
        existing = {"fetched_at": None, "saves": []}
    else:
        existing = json.loads(inventory_path.read_text(encoding="utf-8"))
    merged = merge_into_inventory(existing, new_entries)

    if first_run or merged["saves"] != existing["saves"]:
        merged["fetched_at"] = _now_iso()
        inventory_path.parent.mkdir(parents=True, exist_ok=True)
        inventory_path.write_text(
            json.dumps(merged, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    print(
        f"bsky-saves: inventory now has {len(merged['saves'])} total entries",
        file=sys.stderr,
    )
    return len(merged["saves"])


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
    status_codes: list[int] = []
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
                raise _DirectEndpointFailedError(
                    "; ".join(tried), status_code=r.status_code
                )
            status_codes.append(r.status_code)
            continue

        # Success path.
        r.raise_for_status()
        data = r.json()
        page = _records_from_response(data)
        next_cursor = data.get("cursor")
        return eid, page, (next_cursor or None)

    raise NoBookmarkEndpointError(
        "All bookmark endpoints failed: " + "; ".join(tried),
        status_codes=status_codes,
    )
