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
    """All probed bookmark endpoints failed."""


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
        "All bookmark endpoints failed: " + "; ".join(tried)
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
