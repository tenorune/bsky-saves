"""App-password authentication against an AT Protocol PDS."""
from __future__ import annotations

import sys

import httpx


class ServiceAuthError(Exception):
    """The PDS refused to issue a service-auth token (likely scope-restricted
    app password)."""


def create_session(pds_base: str, handle: str, app_password: str) -> dict:
    """Authenticate via com.atproto.server.createSession.

    Returns the session dict (accessJwt, refreshJwt, did, handle).
    Raises httpx.HTTPStatusError on non-2xx, with the response body included
    in stderr for diagnosis.
    """
    handle = (handle or "").strip()
    app_password = (app_password or "").strip()

    r = httpx.post(
        f"{pds_base.rstrip('/')}/xrpc/com.atproto.server.createSession",
        json={"identifier": handle, "password": app_password},
        timeout=30.0,
    )
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:500]}
        print(
            f"bsky-saves: createSession returned {r.status_code}: {body}",
            file=sys.stderr,
        )
    r.raise_for_status()
    return r.json()


def get_service_auth(pds_base: str, session: dict, aud: str, lxm: str) -> str:
    """Request a service-auth token from the user's PDS, scoped to a specific
    AppView audience and lexicon method.

    Used for bsky.social-hosted accounts that need to call an AppView
    endpoint with a token signed by the user's PDS. Not used by the
    PDS-direct bookmark path (which is the active path for third-party PDSes).
    """
    headers = {"Authorization": f"Bearer {session['accessJwt']}"}
    r = httpx.get(
        f"{pds_base.rstrip('/')}/xrpc/com.atproto.server.getServiceAuth",
        params={"aud": aud, "lxm": lxm},
        headers=headers,
        timeout=30.0,
    )
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:500]}
        raise ServiceAuthError(
            f"getServiceAuth({aud}, lxm={lxm}) returned {r.status_code}: {body}"
        )
    payload = r.json()
    return payload.get("token", "")
