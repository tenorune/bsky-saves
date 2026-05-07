"""bsky-saves serve — local HTTP helper daemon for bsky-saves-gui.

A 127.0.0.1-bound HTTP server that exposes a small set of endpoints the
browser-side bsky-saves-gui app can't reach directly because of CORS:
fetching image bytes from cdn.bsky.app and extracting article text from
arbitrary URLs.

Spec: docs/superpowers/specs/2026-05-04-bsky-saves-v0.3-serve-subcommand.md
External HTTP API contract:
https://github.com/tenorune/bsky-saves-gui/blob/main/docs/bsky-saves-serve-requirements.md
"""
from __future__ import annotations

import base64
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlparse

import httpx

from . import __version__
from .articles import _extract_article
from .auth import create_session, refresh_session
from .fetch import (
    ENDPOINT_IDS,
    fetch_one_page,
    NoBookmarkEndpointError,
    _DirectEndpointFailedError,
)
from .images import DEFAULT_USER_AGENT as _IMAGE_USER_AGENT
from .images import TIMEOUT as _IMAGE_TIMEOUT
from .normalize import normalise_record
from .threads import (
    fetch_thread,
    collect_same_author_replies,
    THREAD_SCHEMA_VERSION,
)
from .tid import rkey_of, decode_tid_to_iso


def _handle_ping(handler) -> None:
    handler._send_json(
        200,
        {
            "name": "bsky-saves",
            "version": __version__,
            "features": ["fetch-image", "extract-article", "fetch", "enrich", "hydrate-threads", "jwt-credentials"],
        },
    )


def _is_allowed_image_url(url: str) -> bool:
    """Hardcoded allowlist for /fetch-image: https + cdn.bsky.app or *.bsky.app."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = parsed.hostname or ""
    return host == "cdn.bsky.app" or host.endswith(".bsky.app")


def _handle_fetch_image(handler) -> None:
    body = handler._read_json_body()
    url = (body or {}).get("url")
    if not isinstance(url, str) or not url:
        handler._send_json_error(400, "missing url")
        return
    if not _is_allowed_image_url(url):
        handler._send_json_error(400, "url not allowed")
        return
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": _IMAGE_USER_AGENT, "Accept": "image/*"},
            follow_redirects=True,
            timeout=_IMAGE_TIMEOUT,
        )
    except Exception as e:
        handler._send_json_error(502, f"{type(e).__name__}: {str(e)[:200]}")
        return
    if r.status_code >= 400:
        handler._send_json_error(r.status_code, f"upstream {r.status_code}")
        return
    content_type = r.headers.get("Content-Type", "application/octet-stream")
    handler._send_bytes(r.status_code, content_type, r.content)


def _handle_extract_article(handler) -> None:
    body = handler._read_json_body()
    url = (body or {}).get("url")
    if not isinstance(url, str) or not url:
        handler._send_json_error(400, "missing url")
        return
    scheme = (urlparse(url).scheme or "").lower()
    if scheme not in ("http", "https"):
        handler._send_json_error(400, "url scheme not allowed")
        return

    extraction, error = _extract_article(url)
    if error is not None:
        if error.startswith("http_"):
            try:
                code = int(error.split("_", 1)[1])
            except ValueError:
                code = 502
            handler._send_json_error(code, f"upstream {code}")
            return
        if error.startswith("fetch_error:"):
            handler._send_json_error(502, error)
            return
        # extraction_failed and any other error → 502 server-side problem.
        handler._send_json_error(502, error)
        return

    assert extraction is not None
    payload = {
        "url": extraction.url,
        "title": extraction.title,
        "text": extraction.text,
        "fetched_at": extraction.fetched_at,
    }
    if extraction.short:
        payload["note"] = "no extractable body"
    handler._send_json(200, payload)


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

    # Build the session dict per credential variant.
    if creds["variant"] == "app_password":
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
    else:  # jwt variant
        session = {
            "accessJwt": creds["access_jwt"],
            "refreshJwt": creds["refresh_jwt"],
            "did": creds["did"],
            "handle": "",
        }

    rotated_credentials: dict | None = None

    def call_fetch_one_page(use_session: dict, eid: str | None, cur: str | None):
        return fetch_one_page(
            use_session,
            pds_base=creds["pds"],
            appview_base=APPVIEW_BASE,
            endpoint_id=eid,
            cursor=cur,
            limit=limit,
        )

    try:
        chosen_id, raw, next_upstream = call_fetch_one_page(
            session, endpoint_id, upstream_cursor
        )
    except _DirectEndpointFailedError as e:
        # Direct named-endpoint failure.
        if creds["variant"] == "jwt" and e.status_code == 401:
            # Refresh + retry on the same endpoint with the same cursor.
            try:
                new_session = refresh_session(creds["pds"], session["refreshJwt"])
            except Exception:
                handler._send_json_error(
                    401,
                    "auth refresh failed",
                    extra={"code": "refresh_failed"},
                )
                return
            rotated_credentials = {
                "access_jwt": new_session["accessJwt"],
                "refresh_jwt": new_session["refreshJwt"],
                "did": new_session["did"],
            }
            try:
                chosen_id, raw, next_upstream = call_fetch_one_page(
                    new_session, endpoint_id, upstream_cursor
                )
            except (_DirectEndpointFailedError, NoBookmarkEndpointError):
                handler._send_json_error(
                    401,
                    "auth refresh failed",
                    extra={"code": "upstream_rejected_after_refresh"},
                )
                return
        else:
            # Non-401 direct failure (or app-password path): silent fallback
            # re-probe with the cursor dropped (per spec — endpoint cursor
            # formats are incompatible). No refresh attempted.
            try:
                chosen_id, raw, next_upstream = call_fetch_one_page(
                    session, None, None
                )
            except NoBookmarkEndpointError as ee:
                handler._send_json_error(
                    502, f"no working bookmark endpoint: {ee}"
                )
                return
    except NoBookmarkEndpointError as e:
        # Probe failure (first call with cursor=None had no working endpoint).
        if creds["variant"] == "jwt" and 401 in e.status_codes:
            # At least one endpoint returned 401 — likely access_jwt expired.
            try:
                new_session = refresh_session(creds["pds"], session["refreshJwt"])
            except Exception:
                handler._send_json_error(
                    401,
                    "auth refresh failed",
                    extra={"code": "refresh_failed"},
                )
                return
            rotated_credentials = {
                "access_jwt": new_session["accessJwt"],
                "refresh_jwt": new_session["refreshJwt"],
                "did": new_session["did"],
            }
            try:
                chosen_id, raw, next_upstream = call_fetch_one_page(
                    new_session, None, None
                )
            except (NoBookmarkEndpointError, _DirectEndpointFailedError):
                handler._send_json_error(
                    401,
                    "auth refresh failed",
                    extra={"code": "upstream_rejected_after_refresh"},
                )
                return
        else:
            handler._send_json_error(
                502, f"no working bookmark endpoint: {e}"
            )
            return

    saves = [normalise_record(r) for r in raw]
    out_cursor = _encode_cursor(chosen_id, next_upstream) if next_upstream else None
    response: dict = {"saves": saves, "cursor": out_cursor}
    if rotated_credentials is not None:
        response["rotated_credentials"] = rotated_credentials
    handler._send_json(200, response)


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


PUBLIC_APPVIEW = "https://public.api.bsky.app"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    # App-password variant: validate via createSession (fail-fast on bad password).
    # JWT variant: skip validation entirely. The endpoint's upstream call is to
    # the public AppView unauthenticated; the JWT is unused. Spec § 6 (JWT-pair
    # path) explicitly relaxes validation here — fail-fast under the JWT path
    # would require a getSession round-trip we don't want to spend.
    if creds["variant"] == "app_password":
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


ROUTES: dict[tuple[str, str], Callable[["_HandlerLike"], None]] = {
    ("GET", "/ping"): _handle_ping,
    ("POST", "/fetch-image"): _handle_fetch_image,
    ("POST", "/extract-article"): _handle_extract_article,
    ("POST", "/fetch"): _handle_fetch,
    ("POST", "/enrich"): _handle_enrich,
    ("POST", "/hydrate-threads"): _handle_hydrate_threads,
}


class _HandlerLike:
    """Type stub for request handlers. The real type is the class returned by
    make_handler; this exists only to give the ROUTES table a clean Callable
    annotation without triggering a forward-reference dance."""


DEFAULT_PDS = "https://bsky.social"
APPVIEW_BASE = "https://bsky.social"


def _validate_creds(creds: object) -> dict | None:
    """Validate a credentials dict from a request body.

    Two accepted shapes (detected by which fields are present):

    - **App-password** (v0.4.0+): requires `handle` and `app_password` (both
      non-empty strings). Optional `pds` defaults to `https://bsky.social`
      when absent or empty. Daemon will call `createSession` per request.

    - **JWT-pair** (v0.4.1+): requires `access_jwt`, `refresh_jwt`, and `did`
      (all non-empty strings). Optional `pds` defaults to `https://bsky.social`.
      Daemon skips `createSession` and uses the tokens directly. The `did`
      field is treated as opaque — no JWT decoding, no `sub`-claim verification.

    Detection priority: `app_password` present → app-password path. Else
    `access_jwt` present → JWT path. Else returns None.

    Returns a normalized dict with a `variant` field set to either
    "app_password" or "jwt", plus the credential fields and pds. Returns
    None when required fields are missing or wrong type.
    """
    if not isinstance(creds, dict):
        return None

    pds = creds.get("pds")
    if not isinstance(pds, str) or not pds:
        pds = DEFAULT_PDS

    # App-password path takes priority when app_password is present.
    if creds.get("app_password") is not None:
        handle = creds.get("handle")
        app_password = creds.get("app_password")
        if not isinstance(handle, str) or not handle:
            return None
        if not isinstance(app_password, str) or not app_password:
            return None
        return {
            "variant": "app_password",
            "handle": handle,
            "app_password": app_password,
            "pds": pds,
        }

    # JWT-pair path.
    if creds.get("access_jwt") is not None:
        access_jwt = creds.get("access_jwt")
        refresh_jwt = creds.get("refresh_jwt")
        did = creds.get("did")
        if not isinstance(access_jwt, str) or not access_jwt:
            return None
        if not isinstance(refresh_jwt, str) or not refresh_jwt:
            return None
        if not isinstance(did, str) or not did:
            return None
        return {
            "variant": "jwt",
            "access_jwt": access_jwt,
            "refresh_jwt": refresh_jwt,
            "did": did,
            "pds": pds,
        }

    return None


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


def make_handler(
    *,
    allow_origins: list[str],
    verbose: bool = False,
) -> type[BaseHTTPRequestHandler]:
    """Build a BaseHTTPRequestHandler subclass that closes over allow_origins
    and verbose. Returning a class (not an instance) is what BaseHTTPRequestHandler
    expects from ThreadingHTTPServer."""

    origins = list(allow_origins)

    class Handler(BaseHTTPRequestHandler):
        # Suppress the default "127.0.0.1 - - [...] GET /ping" log line; we
        # emit our own (or none) via _log_request based on the verbose flag.
        def log_message(self, format, *args):
            return

        def _log_request(self) -> None:
            if verbose:
                print(
                    f"bsky-saves: {self.command} {self.path}",
                    file=sys.stderr,
                )

        def _cors_headers(self) -> None:
            origin = self.headers.get("Origin", "")
            if origin and origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_json_error(self, code: int, error: str, *, extra: dict | None = None) -> None:
            payload: dict = {"error": error}
            if extra:
                payload.update(extra)
            self._send_json(code, payload)

        def _send_bytes(
            self,
            code: int,
            content_type: str,
            body: bytes,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                return None
            if length <= 0:
                return None
            try:
                raw = self.rfile.read(length)
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None
            return parsed if isinstance(parsed, dict) else None

        def do_OPTIONS(self) -> None:
            self._log_request()
            self.send_response(204)
            self._cors_headers()
            self.end_headers()

        def do_GET(self) -> None:
            self._log_request()
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._log_request()
            self._dispatch("POST")

        def __getattr__(self, name: str):
            # BaseHTTPRequestHandler calls self.do_<METHOD>() and raises 501
            # if the method is not found. Override __getattr__ so that any
            # unrecognised do_* verb falls through to _dispatch (which returns
            # 404 for every path not in ROUTES).
            if name.startswith("do_"):
                method = name[3:]

                def _unknown_verb():
                    self._log_request()
                    self._dispatch(method)

                return _unknown_verb
            raise AttributeError(name)

        def _dispatch(self, method: str) -> None:
            handler = ROUTES.get((method, self.path))
            if handler is None:
                self._send_json_error(404, "not found")
                return
            handler(self)

    return Handler


def run_serve(
    *,
    port: int = 47826,
    allow_origins: list[str] | None = None,
    verbose: bool = False,
) -> int:
    """Start the daemon. Blocks until Ctrl-C. Returns an exit code."""
    origins = list(allow_origins or ["https://saves.lightseed.net"])
    handler_cls = make_handler(allow_origins=origins, verbose=verbose)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    except OSError as e:
        print(f"bsky-saves: failed to bind 127.0.0.1:{port}: {e}", file=sys.stderr)
        return 2
    print(
        f"bsky-saves serve listening on http://127.0.0.1:{port} "
        f"(origins: {', '.join(origins)})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0
