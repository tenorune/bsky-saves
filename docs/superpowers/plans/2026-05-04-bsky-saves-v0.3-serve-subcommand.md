# bsky-saves v0.3 — `serve` Subcommand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `bsky-saves serve` — a `127.0.0.1`-bound HTTP daemon with three endpoints (`GET /ping`, `POST /fetch-image`, `POST /extract-article`) that lets `bsky-saves-gui` work around CORS for image and article fetches. Purely additive on top of v0.2; no existing behavior changes.

**Architecture:** All work on a `v0.3` branch. New `src/bsky_saves/serve.py` houses the daemon (stdlib `http.server.ThreadingHTTPServer`, no new dependencies). `articles.py` refactored so a private `_extract_article` helper is shared between the v0.2-shape `fetch_article()` adapter and the new `serve` endpoint. CLI gains a `serve` subparser. Tests boot the server on an ephemeral port and exercise endpoints with stdlib `urllib.request`; upstream `httpx` calls mocked via `respx`.

**Tech Stack:** Python 3.11+, stdlib `http.server`, existing `httpx` and `trafilatura`, `pytest`, `respx` (existing dev dep).

**Spec:** `docs/superpowers/specs/2026-05-04-bsky-saves-v0.3-serve-subcommand.md`.
**External contract (HTTP API):** `https://github.com/tenorune/bsky-saves-gui/blob/main/docs/bsky-saves-serve-requirements.md`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/bsky_saves/serve.py` | Create | Daemon implementation. `make_handler(allow_origins, verbose) -> Handler class` factory; module-level `ROUTES` dispatch table; per-endpoint handlers (`_handle_ping`, `_handle_fetch_image`, `_handle_extract_article`); `run_serve(port, allow_origins, verbose) -> int` entry point. |
| `src/bsky_saves/articles.py` | Modify | Add `ArticleExtraction` dataclass and private `_extract_article(url, ...)` helper. Rewrite `fetch_article(url, ...)` as a thin adapter preserving its v0.2 public tuple shape. No external API change. |
| `src/bsky_saves/cli.py` | Modify | Add `serve` subparser. Dispatch to `serve.run_serve(...)`. |
| `tests/test_articles.py` | Create | Characterization / regression tests pinning `fetch_article()`'s public contract. Run before and after the refactor; must pass at all times. |
| `tests/test_serve.py` | Create | Integration tests. `serve_in_background(allow_origins=...)` context manager boots the server in a daemon thread on an ephemeral port; tests hit the server with stdlib `urllib.request`; upstream `httpx` calls mocked via `respx`. |
| `pyproject.toml` | Modify | Bump `version = "0.3.0"`. |
| `README.md` | Modify | Add a `serve` section pointing at the consumer-side requirements doc. Generic copy ("local HTTP helper for bsky-saves-gui") so it stays valid when `/run` is added in a future release per spec §10. |

The existing `tests/test_fetch.py`, `tests/test_normalize.py`, `tests/test_tid.py`, `tests/test_images.py`, `tests/test_enrich.py`, `tests/test_version.py`, `tests/conftest.py` are unchanged.

---

## Task 1: Create the v0.3 branch

**Files:** none (git operations only).

- [ ] **Step 1: Switch to main and pull latest.**

```bash
cd /home/user/bsky-saves
git checkout main
git pull origin main
```

Expected: `Already up to date.` (or pulls any pending updates).

- [ ] **Step 2: Create the v0.3 branch.**

```bash
git checkout -b v0.3
```

Expected: `Switched to a new branch 'v0.3'`.

- [ ] **Step 3: Push the empty branch to origin to establish tracking.**

```bash
git push -u origin v0.3
```

Expected: `branch 'v0.3' set up to track 'origin/v0.3'`.

---

## Task 2: Articles refactor — characterization tests, then extract `_extract_article` helper

**Files:**
- Create: `tests/test_articles.py`
- Modify: `src/bsky_saves/articles.py`

The existing `articles.py::fetch_article(url)` returns:
- `({"text": str, "date": str|None}, None)` on success.
- `(None, "fetch_error:<ExceptionType>:<message>")` on `httpx` exception.
- `(None, "http_<status>")` on upstream 4xx/5xx.
- `(None, "extraction_failed")` if trafilatura returns `None`.
- `(None, "extraction_too_short_or_empty")` if extracted text is missing or shorter than `MIN_EXTRACT_CHARS` (currently 100).

We add a richer `_extract_article` helper that returns title and a `short` flag (instead of treating short text as an error). `fetch_article` becomes a thin adapter preserving its v0.2 tuple shape exactly.

- [ ] **Step 1: Create the characterization test file.**

Write `tests/test_articles.py` with this content:

```python
"""Characterization / regression tests for bsky_saves.articles.fetch_article.

Pins the v0.2 public contract before the v0.3 refactor introduces
_extract_article. These tests must pass before AND after the refactor.
"""
from __future__ import annotations

import httpx
import respx

from bsky_saves.articles import fetch_article


HAPPY_HTML = (
    "<html><head><title>Hello</title></head><body><article>"
    + ("This is the article body. " * 30)
    + "</article></body></html>"
)
SHORT_HTML = "<html><body><article>too short</article></body></html>"


@respx.mock
def test_fetch_article_returns_text_and_date_on_success():
    respx.get("https://example.com/a").respond(200, html=HAPPY_HTML)
    result, error = fetch_article("https://example.com/a")
    assert error is None
    assert isinstance(result, dict)
    assert isinstance(result["text"], str) and len(result["text"]) >= 100
    assert "date" in result  # may be None when not extractable


@respx.mock
def test_fetch_article_http_error_returns_http_code_string():
    respx.get("https://example.com/a").respond(404)
    result, error = fetch_article("https://example.com/a")
    assert result is None
    assert error == "http_404"


@respx.mock
def test_fetch_article_network_error_returns_fetch_error_string():
    respx.get("https://example.com/a").mock(side_effect=httpx.ConnectError("nope"))
    result, error = fetch_article("https://example.com/a")
    assert result is None
    assert error is not None
    assert error.startswith("fetch_error:")
    assert "ConnectError" in error


@respx.mock
def test_fetch_article_short_extraction_returns_too_short_error():
    respx.get("https://example.com/a").respond(200, html=SHORT_HTML)
    result, error = fetch_article("https://example.com/a")
    assert result is None
    assert error == "extraction_too_short_or_empty"
```

- [ ] **Step 2: Run the tests against the unmodified `articles.py` and confirm they pass.**

```bash
cd /home/user/bsky-saves
python -m pytest tests/test_articles.py -v
```

Expected: all 4 tests pass. (They characterize current behavior; nothing has changed yet.)

- [ ] **Step 3: Refactor `src/bsky_saves/articles.py` — introduce `ArticleExtraction` and `_extract_article`, rewrite `fetch_article` as adapter.**

Replace the entire `fetch_article` function and add the new helper. The full `articles.py` after the edit should contain (showing the relevant top portion through the end of `fetch_article`):

```python
"""Hydrate inventory entries with article_text and article_published_at.

Iterates the inventory for entries whose embed.url has not yet been fetched,
downloads the article HTML, extracts the main text and the publication date
via trafilatura, and writes the result back into the entry's ``article_text``
and (if extractable) ``article_published_at`` fields.

Idempotent: entries with ``article_text`` already populated are skipped
unless they're missing ``article_published_at`` AND ``refresh_dates=True``.
Failed fetches are marked with ``article_fetch_error`` so subsequent runs
don't pointlessly re-hit them.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import trafilatura

DEFAULT_USER_AGENT = (
    "bsky-saves/0.1 (+https://github.com/tenorune/bsky-saves)"
)
RATE_LIMIT_SEC = 1.0
TIMEOUT = 30.0
MIN_EXTRACT_CHARS = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ArticleExtraction:
    """Result of a successful article HTTP fetch + trafilatura extraction.

    Returned by ``_extract_article``. Both ``serve``'s extract-article handler
    and the v0.2 ``fetch_article`` adapter consume this; each maps it to its
    own response shape.
    """
    url: str
    text: str            # may be "" when the page yielded no extractable body
    title: str | None
    date: str | None     # ISO date string if extractable
    fetched_at: str      # ISO timestamp; set whenever the HTTP fetch succeeded
    short: bool          # True if text is non-empty but below MIN_EXTRACT_CHARS,
                         # OR text is empty (paywall / login wall / JS-rendered)


def _extract_article(
    url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
) -> tuple[ArticleExtraction | None, str | None]:
    """Lower-level article fetch + extraction. Returns (extraction, error);
    exactly one is non-None.

    Errors:
      - "fetch_error:<ExceptionType>:<message-truncated>" — httpx raised.
      - "http_<status>" — upstream returned 4xx/5xx.
      - "extraction_failed" — trafilatura returned None.
    """
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.8"},
            follow_redirects=True,
            timeout=TIMEOUT,
        )
    except Exception as e:
        return None, f"fetch_error:{type(e).__name__}:{str(e)[:120]}"

    if r.status_code >= 400:
        return None, f"http_{r.status_code}"

    extracted = trafilatura.bare_extraction(
        r.text,
        include_comments=False,
        include_tables=False,
        favor_recall=True,
        with_metadata=True,
    )
    if extracted is None:
        return None, "extraction_failed"

    if isinstance(extracted, dict):
        text = extracted.get("text") or ""
        title = extracted.get("title")
        date = extracted.get("date")
    else:
        text = getattr(extracted, "text", "") or ""
        title = getattr(extracted, "title", None)
        date = getattr(extracted, "date", None)

    text = text.strip()
    short = (not text) or len(text) < MIN_EXTRACT_CHARS

    return (
        ArticleExtraction(
            url=url,
            text=text if not short else "",
            title=title or None,
            date=date or None,
            fetched_at=_now_iso(),
            short=short,
        ),
        None,
    )


def fetch_article(
    url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
) -> tuple[dict | None, str | None]:
    """v0.2 public adapter. Returns ({"text": str, "date": str|None}, None)
    on success, or (None, error_string) otherwise.

    Preserves the exact v0.2 contract used by ``hydrate_articles``."""
    extraction, error = _extract_article(url, user_agent=user_agent)
    if error is not None:
        return None, error
    assert extraction is not None  # for type checkers
    if extraction.short:
        return None, "extraction_too_short_or_empty"
    return {"text": extraction.text, "date": extraction.date}, None
```

The rest of `articles.py` (`hydrate_articles` and below) is unchanged.

- [ ] **Step 4: Run the characterization tests again — they must still pass.**

```bash
python -m pytest tests/test_articles.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 5: Run the full test suite to confirm nothing else regressed.**

```bash
python -m pytest tests/ -v
```

Expected: every existing test still passes (was 63 before; now 67 with the 4 new characterization tests).

- [ ] **Step 6: Commit.**

```bash
git add tests/test_articles.py src/bsky_saves/articles.py
git commit -m "refactor(articles): extract _extract_article helper

Adds ArticleExtraction dataclass and private _extract_article(url) that
returns title + text + date + fetched_at + short flag. fetch_article
becomes a thin adapter preserving its v0.2 tuple shape exactly.

Title and the short-as-success flag are unused by hydrate_articles
(the adapter discards title and converts short to extraction_too_short_or_empty
to preserve the v0.2 inventory contract); they exist so the upcoming
serve subcommand's /extract-article endpoint can reuse the same fetch/parse
path without duplication.

Adds tests/test_articles.py with characterization tests pinning
fetch_article's public contract before AND after this refactor.

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 3: Stub `serve.py` and tests boot/teardown infrastructure

**Files:**
- Create: `src/bsky_saves/serve.py`
- Create: `tests/test_serve.py`

Build the daemon scaffolding: a `make_handler` factory that closes over `allow_origins`/`verbose`, a module-level `ROUTES` dispatch table (initially empty), and a `serve_in_background` test context manager. Endpoints are added in subsequent tasks. The skeleton must answer 404 for everything (because `ROUTES` is empty) and handle `OPTIONS` preflights with CORS headers.

- [ ] **Step 1: Write the failing test — basic 404 + boot/teardown.**

Create `tests/test_serve.py` with:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail.**

```bash
python -m pytest tests/test_serve.py -v
```

Expected: ImportError or ModuleNotFoundError on `bsky_saves.serve` (module doesn't exist yet).

- [ ] **Step 3: Create the `serve.py` skeleton.**

Write `src/bsky_saves/serve.py`:

```python
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

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import __version__


# Populated by individual endpoint tasks; intentionally empty in the skeleton.
ROUTES: dict[tuple[str, str], Callable[["_HandlerLike"], None]] = {}


class _HandlerLike:
    """Type stub for request handlers. The real type is the class returned by
    make_handler; this exists only to give the ROUTES table a clean Callable
    annotation without triggering a forward-reference dance."""


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

        def _send_json_error(self, code: int, error: str) -> None:
            self._send_json(code, {"error": error})

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
```

- [ ] **Step 4: Run the tests to verify they pass.**

```bash
python -m pytest tests/test_serve.py -v
```

Expected: 2 tests pass (`test_unknown_path_returns_404`, `test_unknown_method_returns_404`).

- [ ] **Step 5: Run the full suite to confirm no regressions.**

```bash
python -m pytest tests/ -v
```

Expected: all tests pass (69 total: 67 from before + 2 new).

- [ ] **Step 6: Commit.**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): add daemon skeleton with CORS + 404 dispatch

New module bsky_saves.serve. make_handler(allow_origins, verbose) returns
a BaseHTTPRequestHandler subclass closed over the per-server config; a
module-level ROUTES dispatch table maps (method, path) to handler funcs;
run_serve() is the blocking entry point. Skeleton answers 404 to every
unknown path and handles OPTIONS preflights with CORS headers.

Endpoints (/ping, /fetch-image, /extract-article) are added in subsequent
tasks; the routing/CORS infrastructure is shared.

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 4: Implement `GET /ping`

**Files:**
- Modify: `src/bsky_saves/serve.py`
- Modify: `tests/test_serve.py`

- [ ] **Step 1: Append failing tests to `tests/test_serve.py`.**

Append:

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
        "features": ["fetch-image", "extract-article"],
    }
```

- [ ] **Step 2: Run the test — expect failure.**

```bash
python -m pytest tests/test_serve.py::test_ping_returns_name_version_features -v
```

Expected: 404 — `/ping` isn't routed yet.

- [ ] **Step 3: Add `_handle_ping` and register it in `ROUTES`.**

In `src/bsky_saves/serve.py`, find the `ROUTES: dict[...] = {}` line near the top and replace it (and add the helper just below) with:

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


ROUTES: dict[tuple[str, str], Callable[["_HandlerLike"], None]] = {
    ("GET", "/ping"): _handle_ping,
}
```

- [ ] **Step 4: Run the test — expect pass.**

```bash
python -m pytest tests/test_serve.py -v
```

Expected: 3 passing tests (the two 404 tests + the new /ping test).

- [ ] **Step 5: Commit.**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): implement GET /ping

Returns {name, version, features}. version reads from
bsky_saves.__version__ (which since v0.2.1 derives from
importlib.metadata, so it always matches the installed package).
features array is hardcoded; future endpoints append entries.

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 5: Implement CORS handling — preflight + per-response headers + allowlist enforcement

**Files:**
- Modify: `tests/test_serve.py`

The CORS *infrastructure* was added in Task 3 (`_cors_headers`, `do_OPTIONS`). This task verifies it via tests; no production code changes are required if Task 3 was done correctly.

- [ ] **Step 1: Append CORS tests to `tests/test_serve.py`.**

```python
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
```

- [ ] **Step 2: Run the tests — they should pass without code changes.**

```bash
python -m pytest tests/test_serve.py -v
```

Expected: all serve tests pass (the new CORS tests pass against the Task 3 infrastructure). If any fail, fix the CORS code in `serve.py` to match the assertions before continuing.

- [ ] **Step 3: Commit.**

```bash
git add tests/test_serve.py
git commit -m "test(serve): cover CORS preflight, allowlist echo, and 404+CORS

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 6: Implement `POST /fetch-image`

**Files:**
- Modify: `src/bsky_saves/serve.py`
- Modify: `tests/test_serve.py`

Endpoint behavior:
1. Parse JSON body; reject non-JSON / missing `url` → 400 `{"error": "missing url"}`.
2. URL allowlist: scheme `https`; hostname is `cdn.bsky.app` OR ends with `.bsky.app`. Otherwise → 400 `{"error": "url not allowed"}`.
3. Fetch via `httpx.get(url, follow_redirects=True, timeout=30, headers={"User-Agent": ..., "Accept": "image/*"})`.
4. On `httpx` exception → 502 `{"error": "<message>"}`.
5. On upstream 4xx/5xx → upstream status, body `{"error": "upstream <code>"}`.
6. On success → upstream status (200), `Content-Type` from upstream (default `application/octet-stream`), body = upstream bytes.

- [ ] **Step 1: Append failing tests to `tests/test_serve.py`.**

```python
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
```

- [ ] **Step 2: Run the tests — expect failures.**

```bash
python -m pytest tests/test_serve.py -v -k fetch_image
```

Expected: all fetch-image tests fail with 404 (route not registered yet).

- [ ] **Step 3: Implement `_handle_fetch_image` and register it in `ROUTES`.**

In `src/bsky_saves/serve.py`, add an `import httpx` and a `urllib.parse.urlparse` import at the top, then add this function below `_handle_ping`:

```python
import httpx
from urllib.parse import urlparse

from .images import DEFAULT_USER_AGENT as _IMAGE_USER_AGENT
from .images import TIMEOUT as _IMAGE_TIMEOUT


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
```

Then update the `ROUTES` table:

```python
ROUTES: dict[tuple[str, str], Callable[["_HandlerLike"], None]] = {
    ("GET", "/ping"): _handle_ping,
    ("POST", "/fetch-image"): _handle_fetch_image,
}
```

- [ ] **Step 4: Run the tests — expect pass.**

```bash
python -m pytest tests/test_serve.py -v
```

Expected: all serve tests pass (including the 8 new fetch-image tests).

- [ ] **Step 5: Run the full suite.**

```bash
python -m pytest tests/ -v
```

Expected: every test passes.

- [ ] **Step 6: Commit.**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): implement POST /fetch-image

Hardcoded URL allowlist: https + cdn.bsky.app or *.bsky.app. Bare
bsky.app and lookalike domains rejected. Reuses
images.DEFAULT_USER_AGENT and images.TIMEOUT (no duplication).
Upstream 4xx/5xx passed through as the upstream status with
{error: 'upstream <code>'}; httpx exceptions return 502.

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 7: Implement `POST /extract-article`

**Files:**
- Modify: `src/bsky_saves/serve.py`
- Modify: `tests/test_serve.py`

Endpoint behavior:
1. Parse JSON body; reject non-JSON / missing `url` → 400 `{"error": "missing url"}`.
2. URL scheme allowlist: `http://` or `https://` (anything else → 400 `{"error": "url scheme not allowed"}`).
3. Call `articles._extract_article(url)`.
4. Successful fetch + non-empty text → 200 with `{url, title, text, fetched_at}`.
5. Successful fetch + short/empty text → 200 with `{url, title, text: "", fetched_at, note: "no extractable body"}`.
6. `_extract_article` returned error `fetch_error:...` → 502 with `{"error": "<message>"}`.
7. `_extract_article` returned error `http_<code>` → that code with `{"error": "upstream <code>"}`.
8. `_extract_article` returned error `extraction_failed` → 502 with `{"error": "extraction_failed"}` (treat as a server-side problem; trafilatura couldn't parse anything).

- [ ] **Step 1: Append failing tests to `tests/test_serve.py`.**

```python
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
```

- [ ] **Step 2: Run the tests — expect failures.**

```bash
python -m pytest tests/test_serve.py -v -k extract_article
```

Expected: all 6 extract-article tests fail with 404 (route not registered yet).

- [ ] **Step 3: Implement `_handle_extract_article` and register it.**

In `src/bsky_saves/serve.py`, add `from .articles import _extract_article` to the imports near the top of the file (alongside the existing `from .images import ...` lines). Then add below `_handle_fetch_image`:

```python
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
```

Update `ROUTES`:

```python
ROUTES: dict[tuple[str, str], Callable[["_HandlerLike"], None]] = {
    ("GET", "/ping"): _handle_ping,
    ("POST", "/fetch-image"): _handle_fetch_image,
    ("POST", "/extract-article"): _handle_extract_article,
}
```

- [ ] **Step 4: Run the tests — expect pass.**

```bash
python -m pytest tests/test_serve.py -v
```

Expected: every serve test passes (the 6 new extract-article tests pass).

- [ ] **Step 5: Full suite.**

```bash
python -m pytest tests/ -v
```

Expected: every test passes.

- [ ] **Step 6: Commit.**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): implement POST /extract-article

Reuses articles._extract_article (the helper introduced in the v0.3
articles refactor) for the actual fetch + trafilatura call. Maps the
extraction result into the JSON response shape, including the optional
note: 'no extractable body' field when the page yielded short/empty
text. URL scheme allowlist: http or https. Upstream HTTP errors pass
through as their original status; network errors return 502;
extraction_failed returns 502.

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 8: `--allow-origin` override behavior + `--verbose` request logging

**Files:**
- Modify: `tests/test_serve.py`

Both behaviors are already implemented in `make_handler` and `run_serve` from Task 3. This task adds tests verifying them.

- [ ] **Step 1: Append tests to `tests/test_serve.py`.**

```python
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
```

- [ ] **Step 2: Run the tests — they should pass without code changes.**

```bash
python -m pytest tests/test_serve.py -v -k "allow_origin or verbose or silent"
```

Expected: all 4 new tests pass against the existing infrastructure. If any fail, fix `make_handler` in `serve.py` to satisfy them before continuing.

- [ ] **Step 3: Run the full suite.**

```bash
python -m pytest tests/ -v
```

Expected: every test passes.

- [ ] **Step 4: Commit.**

```bash
git add tests/test_serve.py
git commit -m "test(serve): cover --allow-origin override and --verbose logging

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 9: Wire up the `serve` CLI subcommand

**Files:**
- Modify: `src/bsky_saves/cli.py`

- [ ] **Step 1: Read the existing `cli.py` to find the dispatch table.**

```bash
grep -n "def main\|args.cmd ==\|sub.add_parser\|return parser" src/bsky_saves/cli.py
```

This shows the existing subparsers (`fetch`, `hydrate`, `enrich`) and the dispatch in `main()`.

- [ ] **Step 2: Add the `serve` subparser.**

In `src/bsky_saves/cli.py`, find the `_build_parser()` function. After the `enrich` subparser block (the last `sub.add_parser(...)` call before `return parser`), add:

```python
    p_serve = sub.add_parser(
        "serve",
        help="Run a local HTTP helper daemon for bsky-saves-gui.",
    )
    p_serve.add_argument(
        "--port",
        type=int,
        default=47826,
        help="TCP port to bind on 127.0.0.1 (default: 47826).",
    )
    p_serve.add_argument(
        "--allow-origin",
        action="append",
        default=None,
        metavar="ORIGIN",
        help="Origin permitted to call this daemon. Repeatable. "
             "Explicit values fully replace the default of "
             "https://saves.lightseed.net.",
    )
    p_serve.add_argument(
        "--verbose",
        action="store_true",
        help="Log each request to stderr.",
    )
```

- [ ] **Step 3: Add the dispatch in `main()`.**

In the same file, find the `if args.cmd == "enrich":` block. Below it (still inside `main`), add:

```python
    if args.cmd == "serve":
        from .serve import run_serve

        return run_serve(
            port=args.port,
            allow_origins=args.allow_origin or ["https://saves.lightseed.net"],
            verbose=args.verbose,
        )
```

- [ ] **Step 4: Update the docstring at the top of `cli.py`.**

Find the docstring block at the top of the file (begins with `"""Command-line entry point for ``bsky-saves``.`). After the existing subcommand list (the line beginning with `bsky-saves enrich --inventory PATH ...`), add:

```
  bsky-saves serve [--port PORT] [--allow-origin ORIGIN]... [--verbose]
      Run a local HTTP helper daemon for bsky-saves-gui (CORS bridge).
```

- [ ] **Step 5: Verify the help output.**

```bash
python -m pip install -e ".[dev]"  # ensure module is importable
python -c "from bsky_saves.cli import _build_parser; _build_parser().parse_args(['serve', '--help'])"
```

Expected: argparse prints the `serve` subcommand help with `--port`, `--allow-origin`, `--verbose` flags. Exit 0 (argparse exits after `--help`).

- [ ] **Step 6: Run the full test suite.**

```bash
python -m pytest tests/ -v
```

Expected: every existing test still passes; no regressions from the CLI changes.

- [ ] **Step 7: Commit.**

```bash
git add src/bsky_saves/cli.py
git commit -m "feat(cli): wire up bsky-saves serve subcommand

New 'serve' subparser with --port, --allow-origin (repeatable),
--verbose flags. Dispatch calls serve.run_serve(). --allow-origin
defaults to ['https://saves.lightseed.net'] when not provided;
explicit values fully replace the default.

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 10: Bump version and update README

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`

- [ ] **Step 1: Bump `pyproject.toml` to v0.3.0.**

Change the line `version = "0.2.3"` to `version = "0.3.0"`.

```bash
grep -n '^version' pyproject.toml
```

Expected before: `version = "0.2.3"`. After: `version = "0.3.0"`.

- [ ] **Step 2: Read the existing README to find the right insertion point.**

```bash
grep -n "^##\|^###\|hydrate images\|enrich" README.md | head -30
```

Locate the "Use" section of the README that lists CLI examples (added in earlier tasks).

- [ ] **Step 3: Add a `serve` example to the existing CLI usage section.**

Find the code-block showing CLI invocations in the README (the one that ends with `bsky-saves hydrate images --inventory ./saves_inventory.json --out ./images`). Append a new comment + command at the end of that block:

```
# Run a local HTTP helper daemon for bsky-saves-gui (CORS bridge).
# Binds 127.0.0.1:47826; pass --allow-origin for self-hosted GUI deployments.
bsky-saves serve
```

- [ ] **Step 4: Add a dedicated `serve` section after the existing CLI section.**

Add a new top-level section to the README, immediately before the "Inventory schema" section. The full section text:

```markdown
## `bsky-saves serve`

`bsky-saves serve` runs a small HTTP helper daemon on `127.0.0.1` that
[bsky-saves-gui](https://github.com/tenorune/bsky-saves-gui) — a static web
app running `bsky-saves` in Pyodide — calls to fetch image bytes and extract
article text. Both operations are blocked by CORS in the browser; the helper
runs on the user's own machine so the actual outbound HTTP happens locally.

```
bsky-saves serve [--port 47826] [--allow-origin https://saves.lightseed.net]... [--verbose]
```

The daemon exposes three endpoints (`GET /ping`, `POST /fetch-image`,
`POST /extract-article`), binds only to `127.0.0.1`, requires no
authentication or credentials, writes nothing to disk, and reads no
config files. It's a stateless passthrough that exists for the duration
of the `serve` invocation.

The full HTTP API contract lives in the consumer-side requirements doc:
[`bsky-saves-gui/docs/bsky-saves-serve-requirements.md`](https://github.com/tenorune/bsky-saves-gui/blob/main/docs/bsky-saves-serve-requirements.md).

```

(Note the closing triple-backtick on its own line at the end; markdown nests fine.)

- [ ] **Step 5: Verify nothing weird in the rest of the README.**

```bash
grep -in "0.1.x\|0.2.x" README.md || echo "(no version-pin references)"
```

Expected: `(no version-pin references)`. (The v0.2.1 README cleanup already removed those.)

- [ ] **Step 6: Run the full test suite.**

```bash
python -m pytest tests/ -v
```

Expected: every test passes.

- [ ] **Step 7: Commit.**

```bash
git add pyproject.toml README.md
git commit -m "chore: bump version to 0.3.0; document serve in README

https://claude.ai/code/session_01TMfymjB13QmHZczFsaS3Jf"
```

---

## Task 11: Final verification gate

**Files:** none (verification only).

This is the gate before pushing the v0.3 branch and creating the v0.3.0 release.

- [ ] **Step 1: Run the full test suite from a clean state.**

```bash
cd /home/user/bsky-saves
python -m pytest tests/ -v
```

Expected: all tests pass. Counting from prior plans: 63 (after v0.2.3) + 4 articles regression + 25 serve tests ≈ 92 total. Confirm the actual count, but the key requirement is **zero failures**.

- [ ] **Step 2: Build sdist and wheel.**

```bash
rm -rf dist/ build/ src/bsky_saves.egg-info/
python -m build
ls -la dist/
```

Expected: `dist/bsky_saves-0.3.0-py3-none-any.whl` and `dist/bsky_saves-0.3.0.tar.gz` both present.

- [ ] **Step 3: Smoke-test the wheel in a clean venv.**

```bash
rm -rf /tmp/v030-smoke
python -m venv /tmp/v030-smoke
/tmp/v030-smoke/bin/pip install dist/bsky_saves-0.3.0-py3-none-any.whl
/tmp/v030-smoke/bin/python -c "import bsky_saves; print('__version__:', bsky_saves.__version__)"
/tmp/v030-smoke/bin/bsky-saves --help
/tmp/v030-smoke/bin/bsky-saves serve --help
```

Expected:
- `__version__: 0.3.0`.
- `bsky-saves --help` lists `serve` among the subcommands.
- `bsky-saves serve --help` shows `--port`, `--allow-origin`, `--verbose`.

- [ ] **Step 4: Live boot smoke test.**

```bash
/tmp/v030-smoke/bin/bsky-saves serve --port 47827 &
SERVE_PID=$!
sleep 1
curl -sS http://127.0.0.1:47827/ping
echo
kill $SERVE_PID
wait $SERVE_PID 2>/dev/null
```

Expected: `curl` prints a JSON body matching `{"name": "bsky-saves", "version": "0.3.0", "features": ["fetch-image", "extract-article"]}` (whitespace will vary; substance must match). The daemon exits cleanly on `kill` (which sends SIGTERM, treated as a clean shutdown by `serve_forever`).

- [ ] **Step 5: Confirm 127.0.0.1-only binding.**

```bash
/tmp/v030-smoke/bin/bsky-saves serve --port 47828 &
SERVE_PID=$!
sleep 1
# Should succeed (loopback):
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:47828/ping
# Should fail (non-loopback IP on the same machine; daemon is bound to 127.0.0.1 only).
# Use the host's primary non-loopback IP if available; otherwise skip this assertion.
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -n "$HOST_IP" ] && [ "$HOST_IP" != "127.0.0.1" ]; then
    curl -sS -m 2 -o /dev/null -w "%{http_code}\n" "http://${HOST_IP}:47828/ping" || echo "connection refused (expected)"
fi
kill $SERVE_PID
wait $SERVE_PID 2>/dev/null
```

Expected: loopback request returns `200`. Non-loopback request fails with connection refused or timeout (depending on platform), confirming the bind is loopback-only.

- [ ] **Step 6: Push the v0.3 branch.**

```bash
git push -u origin v0.3
```

Expected: branch pushed; tracking established.

- [ ] **Step 7: Merge to main and push.**

```bash
git checkout main
git pull origin main
git merge --ff-only v0.3
git push origin main
```

Expected: fast-forward merge succeeds; main now contains all v0.3.0 commits. Confirms a clean linear history.

- [ ] **Step 8: Stop and hand off.**

After main is updated, this session's work is done. Per spec §8 release strategy:

1. The user deletes the `v0.3` branch on GitHub (proxy 403 prevents remote branch deletes from this sandbox).
2. The user creates the `v0.3.0` release / tag via the GitHub UI on the new HEAD of `main`. Suggested release title: `v0.3.0 — bsky-saves serve (local HTTP helper daemon)`.
3. The `release.yml` workflow publishes `bsky-saves==0.3.0` to PyPI.
4. The `bsky-saves-gui` team can then update the example `version` string in their requirements doc from `"0.2.4"` to `"0.3.0"` and adopt the `serve` daemon in their installer instructions.

Report back to the user:
- All tests green (count).
- Wheel + sdist built.
- v0.3 branch pushed; main fast-forwarded; main pushed.
- Local smoke test confirmed: `/ping` returns the correct payload, loopback-only bind verified.
- Awaiting branch deletion + GitHub release creation on the user's side.

---

## Self-review notes

After writing this plan I checked it against the spec:

- **Spec §1 (context):** Plan task 1 (branch creation) and the overall architecture description in the header reflect the same context. ✓
- **Spec §2 (scope):** Tasks are purely additive (new module, new tests, new CLI subparser, version bump, README addition). One refactor in `articles.py`, but it preserves `fetch_article`'s public shape — characterization tests in Task 2 enforce this. ✓
- **Spec §3 (architecture):** All listed new and modified files appear in the File Structure table and in individual tasks. Single `serve.py` module; `make_handler` factory; `ROUTES` dispatch table. ✓
- **Spec §4 (CLI surface):** Task 9 implements the `serve` subparser with `--port`, `--allow-origin`, `--verbose`. Default-replacement semantics for `--allow-origin` are encoded in Task 9 (`default=None` then `args.allow_origin or [...]`) and verified by Task 8 tests. ✓
- **Spec §5 (article extraction reuse):** Task 2 introduces `ArticleExtraction` and `_extract_article` exactly as the spec sketches. `fetch_article` becomes the v0.2-shape adapter. Characterization tests pin the existing public contract. ✓
- **Spec §6 (HTTP behavior):** Tasks 4–7 implement each endpoint per the spec's per-endpoint subsections. CORS handling per spec §6 is implemented in Task 3 and verified in Task 5. URL allowlist for `/fetch-image` matches the spec's `cdn.bsky.app + *.bsky.app` rule. Schema allowlist for `/extract-article` is `http`/`https`. ✓
- **Spec §7 (test strategy):** `serve_in_background` context manager (Task 3); `make_handler` factory (Task 3); per-test ephemeral port (each test calls `serve_in_background()` fresh); stdlib `urllib.request` for client; `respx` for upstream mocks. Test matrix matches: ping, 404 path, 404 method, OPTIONS preflight, CORS allowed/disallowed/no-origin, fetch-image happy/wildcard/bare-rejected/lookalike-rejected/http-rejected/missing-url/4xx/network, extract-article happy/empty/missing-url/scheme-rejected/5xx/network, allow-origin override, verbose. The `test_fetch_article_v02_contract_preserved` requirement is satisfied by `tests/test_articles.py` (Task 2) which has 4 characterization tests. ✓
- **Spec §8 (release strategy):** Task 1 creates the v0.3 branch; Task 11 step 6 pushes it; step 7 merges to main and pushes; step 8 documents the user's manual steps (branch delete, GitHub release creation). ✓
- **Spec §9 (security posture):** Hardcoded 127.0.0.1 bind is in `run_serve` (Task 3); URL allowlist is in `_is_allowed_image_url` (Task 6); origin allowlist is in `_cors_headers` (Task 3); no persistence anywhere; verbose-only stderr logging (Task 8 verifies). ✓
- **Spec §10 (forward-compat / phase-2 awareness):** Generic naming preserved throughout (`serve.py`, `serve` CLI subcommand, `run_serve`, `_handle_<endpoint>`); ROUTES table extension is a one-entry append; `features` array is hardcoded but documented as the consumer's discovery mechanism. README copy in Task 10 frames the daemon generically (not "the image and article helper"). ✓
- **Spec §11 (out of scope):** No tasks address phase-2 `/run`, auth, streaming, rate limits, etc. ✓
- **Spec §12 (decisions log):** All decisions reflected in implementation choices. ✓

**Placeholder scan:** None of the no-placeholder rules from the writing-plans skill are violated. Every step has the actual code, command, or expected output it needs. Test code blocks contain complete tests. Implementation blocks contain complete functions. No "TBD," no "similar to Task N," no "add appropriate error handling."

**Type / name consistency:**
- `make_handler(allow_origins, verbose)` — consistent across Task 3 (definition), Task 5 (test usage), Task 8 (test usage). ✓
- `run_serve(port, allow_origins, verbose) -> int` — Task 3 (definition), Task 9 (CLI dispatch). ✓
- `_extract_article(url, *, user_agent)` — Task 2 (definition), Task 7 (consumed by `_handle_extract_article`). ✓
- `ArticleExtraction` field names: `url`, `text`, `title`, `date`, `fetched_at`, `short` — consistent in Task 2 (dataclass), Task 7 (handler reads them). ✓
- `_handle_ping`, `_handle_fetch_image`, `_handle_extract_article`, `_is_allowed_image_url` — consistent across tasks. ✓
- `ROUTES` table extended with single-entry appends across Tasks 4, 6, 7. ✓

No fixes needed.
