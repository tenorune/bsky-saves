# bsky-saves v0.3 — `serve` subcommand (local helper daemon)

> **Status:** approved 2026-05-04. Implementation pending.
> **Branch:** `v0.3` in `tenorune/bsky-saves` (to be created).
> **External contract:** `https://github.com/tenorune/bsky-saves-gui/blob/main/docs/bsky-saves-serve-requirements.md`. That document is canonical for the HTTP API (endpoints, request/response shapes, CORS rules, security constraints). This document is canonical for the bsky-saves-side implementation (module layout, code reuse, test strategy, release).

---

## 1. Context

`bsky-saves-gui` is a static web app that runs `bsky-saves` in Pyodide so non-technical users can ingest their Bluesky bookmarks without installing anything. The browser environment can do most of the work, but two operations are blocked by CORS: fetching image bytes from `cdn.bsky.app` and fetching arbitrary article URLs for text extraction. Neither host sends CORS headers, so any in-browser fetch is blocked from reading the response body.

The consumer-side fix is a small localhost HTTP daemon — `bsky-saves serve` — that runs on the user's machine and proxies these two operations. The web app probes for it on a known port and degrades gracefully if it's not running.

bsky-saves is the natural home for the daemon: the operations it bridges (fetch image bytes, extract article text) are exactly what `hydrate images` and `hydrate articles` already do. `serve` is a *second transport* for those operations, not a new product capability.

## 2. Scope

bsky-saves remains an *ingestion package*. v0.3.0 is **purely additive**:

- Adds: `bsky-saves serve` subcommand and supporting module.
- Adds: title extraction in the article-extraction pipeline (used by `serve`; discarded by the v0.2-shape `fetch_article()` adapter to preserve the existing inventory contract).
- Does not change: behavior of `fetch`, `enrich`, `hydrate articles`, `hydrate threads`, `hydrate images`. No flag changes, no inventory-schema changes.

### What `serve` is

A `127.0.0.1`-bound HTTP daemon with three endpoints (`GET /ping`, `POST /fetch-image`, `POST /extract-article`), an origin allowlist, a hardcoded URL allowlist for image fetches (`*.bsky.app`), no authentication, no persistence, no config files. See the consumer-side requirements doc for the full HTTP API contract.

### What `serve` is not

A general-purpose proxy. A long-running supervised service. An auto-launched daemon. A multi-tenant server. Anything that touches disk beyond the ingestion paths it already shares with `hydrate articles`/`hydrate images` (and `serve` itself touches no disk at all).

## 3. Architecture and module layout

### New files

| File | Responsibility |
|---|---|
| `src/bsky_saves/serve.py` | Daemon implementation. Request handler factory, HTTP server entry point, JSON helpers, URL allowlist for `/fetch-image`, CORS handling. Target: <300 lines. |
| `tests/test_serve.py` | Integration tests that boot the server on an ephemeral port and exercise each endpoint. |
| `tests/test_articles.py` | Regression test for `articles.fetch_article()`'s public contract (added because the Section 5 refactor restructures `articles.py` internals). |

### Modified files

| File | Change |
|---|---|
| `src/bsky_saves/cli.py` | Adds `serve` subparser. Dispatches to `serve.run_serve(...)`. |
| `src/bsky_saves/articles.py` | Refactor: extract a private `_extract_article(url, ...)` helper that returns a richer dataclass (title + text + date + fetched_at + short flag). `fetch_article()` becomes a thin adapter preserving its v0.2 public contract; `serve.py` calls `_extract_article` directly. |
| `pyproject.toml` | Bump `version = "0.3.0"`. |
| `README.md` | Add a `serve` section pointing at the consumer-side requirements doc for the HTTP API contract; note bind address, default port, default origin allowlist, security posture (127.0.0.1, no auth, no persistence). |

### Concurrency

`http.server.ThreadingHTTPServer` (stdlib). One thread per request. Both endpoint operations are I/O-bound (HTTP fetch + trafilatura parse); the GIL is not a bottleneck. No async layer.

### Dependencies

Zero new packages. Uses stdlib (`http.server`, `socketserver`, `urllib.parse`, `json`, `threading`, `socket`, `dataclasses`) plus existing `httpx` and `trafilatura`.

### Out of scope as entry points

No `python -m bsky_saves.serve` shortcut. No `__main__.py`. The single entry point is `bsky-saves serve` via the existing CLI.

## 4. CLI surface

```
bsky-saves serve [--port PORT] [--allow-origin ORIGIN]... [--verbose]
```

| Flag | argparse | Default | Notes |
|---|---|---|---|
| `--port` | `type=int` | `47826` | TCP bind port. Default is the consumer-side discovery port. |
| `--allow-origin` | `action="append"`, `default=None` | `["https://saves.lightseed.net"]` (applied if user passes none) | Repeatable. **Explicit values fully replace the default** — passing `--allow-origin https://other.example` once results in only that origin being allowed. To keep the default *and* add another, the user passes both: `--allow-origin https://saves.lightseed.net --allow-origin https://other.example`. |
| `--verbose` | `action="store_true"` | False | Logs each request to stderr. Off by default. Never logs to disk. |

**Bind address:** hardcoded `127.0.0.1`. Not exposed as a flag (consumer doc's hard requirement).

**Startup line** to stderr:
```
bsky-saves serve listening on http://127.0.0.1:47826 (origins: https://saves.lightseed.net)
```
Multi-origin form: `(origins: https://saves.lightseed.net, https://other.example)`.

**Shutdown:** `KeyboardInterrupt` (Ctrl-C) → `server.shutdown()` + `server.server_close()` → exit 0.

**Dispatch in `cli.py`:**
```python
if args.cmd == "serve":
    from .serve import run_serve
    return run_serve(
        port=args.port,
        allow_origins=args.allow_origin or ["https://saves.lightseed.net"],
        verbose=args.verbose,
    )
```

`run_serve(port, allow_origins, verbose) -> int` returns an exit code (0 on clean shutdown; nonzero on bind failure or other startup error).

## 5. Code reuse — shared article-extraction helper

### Problem

The current `articles.py::fetch_article(url)` returns `({"text": str, "date": str|None}, None)` on success or `(None, "<error>")` on failure. It treats short-text extraction as an error (`"extraction_too_short_or_empty"`) and does not extract title.

The new `/extract-article` endpoint needs **title** and treats **short/empty text as success** (200 with `text: ""` and `note: "no extractable body"`).

### Decision: shared private helper, both consumers adapt

Extract a private `_extract_article(url, *, user_agent=...)` in `articles.py` that returns a richer dataclass:

```python
@dataclass
class ArticleExtraction:
    url: str
    text: str            # may be ""
    title: str | None
    date: str | None     # ISO date string if extractable
    fetched_at: str      # ISO timestamp; set on any successful HTTP fetch
    short: bool          # True if text was below MIN_EXTRACT_CHARS

def _extract_article(url: str, *, user_agent=DEFAULT_USER_AGENT) -> tuple[ArticleExtraction | None, str | None]:
    """Lower-level extraction. Returns (extraction, error). Exactly one is non-None."""
    ...
```

`fetch_article()` becomes a thin adapter preserving the v0.2 tuple shape:

```python
def fetch_article(url: str, *, user_agent=DEFAULT_USER_AGENT) -> tuple[dict | None, str | None]:
    extraction, error = _extract_article(url, user_agent=user_agent)
    if error is not None:
        return None, error
    if extraction.short:
        return None, "extraction_too_short_or_empty"
    return {"text": extraction.text, "date": extraction.date}, None
```

The serve handler calls `_extract_article` directly and maps `short`/`title` into the JSON response, including the optional `note: "no extractable body"` field when `extraction.short` is True.

### Why this over duplication or inline-in-serve

- **Duplication (parallel function in `articles.py`)** would force trafilatura-handling logic to be maintained in two places; future improvements (timeout tuning, parser flags, error-classification edge cases) would have to be made twice and could drift.
- **Inline in serve.py** would actively create drift risk between the daemon's article extraction and `hydrate articles`'.
- **Shared helper** is DRY, isolates each adapter to its own response-shape concerns, and makes title extraction "free" for both paths (currently discarded by the `fetch_article` adapter; could be opted into by `hydrate articles` later if the inventory schema gains an article-title field — explicitly out of scope for v0.3.0).

### Regression net

`tests/test_articles.py` (new) gets one focused test asserting `fetch_article()`'s public contract: tuple shape, the four success/failure return modes, and the exact error strings (`fetch_error:...`, `http_<code>`, `extraction_failed`, `extraction_too_short_or_empty`). The existing higher-level `hydrate articles` tests provide additional indirect coverage.

## 6. HTTP behavior — implementation notes

The consumer-side requirements doc is canonical for the HTTP API. The notes here cover bsky-saves-side implementation choices not pinned by that doc.

### Routing

Single `BaseHTTPRequestHandler` subclass with a small dispatch table:

```python
ROUTES = {
    ("GET", "/ping"): _handle_ping,
    ("POST", "/fetch-image"): _handle_fetch_image,
    ("POST", "/extract-article"): _handle_extract_article,
}
```

- `OPTIONS` to any path with a CORS preflight → 204 + CORS headers, no body.
- Any unknown `(method, path)` → 404 with body `{"error": "not found"}`.
- The handler never reads the filesystem and never exposes any debug surface.

### `GET /ping`

Returns:
```python
{"name": "bsky-saves", "version": __version__, "features": ["fetch-image", "extract-article"]}
```

`__version__` is sourced from `bsky_saves.__version__` (which itself comes from `importlib.metadata.version("bsky-saves")` since v0.2.1, so it always matches the installed package). The `features` list is a hardcoded constant in `serve.py`; adding a new endpoint in a future release means appending here.

### `POST /fetch-image`

1. Parse JSON body. Reject non-JSON or missing `url` → 400 `{"error": "missing url"}`.
2. URL allowlist (hardcoded): `urlparse(url).scheme == "https"` AND (`hostname == "cdn.bsky.app"` OR `hostname.endswith(".bsky.app")`). Rejects `bsky.app` exactly (no leading dot in subdomain match), `bskyapp.com`, `bsky.app.evil.example`, etc. Rejects → 400 `{"error": "url not allowed"}`.
3. `httpx.get(url, follow_redirects=True, timeout=30, headers={"User-Agent": images.DEFAULT_USER_AGENT, "Accept": "image/*"})`. Reuses the existing User-Agent constant from `images.py`; does NOT call `download_to(url, dest)` because that writes to disk.
4. On 4xx/5xx upstream → status from upstream, body `{"error": "upstream <code>"}`.
5. On `httpx` exception → 502, body `{"error": "<exception message, truncated>"}`.
6. On success → upstream status (200), `Content-Type` from upstream (default `application/octet-stream` if absent), `Content-Length` set to `len(bytes)`, body = upstream bytes. Buffered fully in memory before responding (images are small; streaming through `BaseHTTPRequestHandler` adds complexity for marginal benefit).

### `POST /extract-article`

1. Parse JSON body. Reject non-JSON or missing `url` → 400.
2. URL scheme allowlist: `http://` or `https://`. Reject `file://`, `ftp://`, etc. → 400 `{"error": "url scheme not allowed"}`.
3. Call `articles._extract_article(url, user_agent=...)`. Timeout: 60s.
4. Successful fetch + non-empty text → 200 with `{"url", "title", "text", "fetched_at"}`.
5. Successful fetch + short/empty text → 200 with `{"url", "title", "text": "", "fetched_at", "note": "no extractable body"}`.
6. `_extract_article` returned with error string `fetch_error:...` → 502 with `{"error": "<message>"}`.
7. `_extract_article` returned with error string `http_<code>` → that code, body `{"error": "upstream <code>"}`.

### CORS

- Per-request: read `Origin`. If in the allowlist, set `Access-Control-Allow-Origin: <origin>`. If not in list, omit the header (browser fail-closed).
- Always set on every response (including 4xx/5xx and OPTIONS preflight): `Access-Control-Allow-Methods: GET, POST, OPTIONS`, `Access-Control-Allow-Headers: Content-Type`, `Access-Control-Max-Age: 600`.
- No `Origin` header (curl/scripts) → process the request normally; CORS headers absent (irrelevant to non-browser clients).

## 7. Test strategy

`tests/test_serve.py` boots the server in a daemon thread on an ephemeral port (`port=0`), exercises endpoints with stdlib `urllib.request`, then shuts the server down via context manager teardown.

```python
@contextlib.contextmanager
def serve_in_background(allow_origins=("https://saves.lightseed.net",)):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(allow_origins=allow_origins))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
```

`make_handler(allow_origins=...)` is a small factory in `serve.py` that closes over the origin allowlist and returns a `BaseHTTPRequestHandler` subclass — avoids global state on the handler class and makes per-test customization clean.

**Client side: stdlib `urllib.request`.** Keeps `respx` free to mock the *upstream* `httpx` calls without test-tool collision (using `httpx` for both client and upstream sides would complicate respx scope).

**Test matrix (target ≥15 tests):**

| Test | Verifies |
|---|---|
| `test_ping_returns_name_version_features` | `/ping` → 200 with `{name, version, features}`; `version == bsky_saves.__version__`. |
| `test_unknown_path_returns_404` | `GET /admin` → 404 `{"error": "not found"}`. |
| `test_unknown_method_returns_404` | `DELETE /ping` → 404. |
| `test_options_preflight_returns_204_with_cors` | `OPTIONS /fetch-image` with allowed `Origin` → 204 + CORS headers. |
| `test_cors_allowed_origin_echoed` | Allowed `Origin` echoed in `Access-Control-Allow-Origin`. |
| `test_cors_disallowed_origin_omits_header` | Disallowed `Origin` → header absent in response. |
| `test_cors_no_origin_header_request_succeeds` | curl-style request (no `Origin`) succeeds. |
| `test_fetch_image_happy_path` | Mock `cdn.bsky.app/img/...` upstream → 200; assert response bytes + Content-Type passthrough. |
| `test_fetch_image_subdomain_wildcard` | `https://video.bsky.app/...` accepted; `https://bskyapp.com/...` rejected; bare `https://bsky.app/...` rejected. |
| `test_fetch_image_disallowed_url` | Off-allowlist URL → 400 `{"error": "url not allowed"}`. |
| `test_fetch_image_upstream_4xx` | Mock 404 upstream → response status 404 + `{"error": "upstream 404"}`. |
| `test_fetch_image_network_error` | Mock httpx exception → 502 + `{"error": ...}`. |
| `test_extract_article_happy_path` | Mock article HTML upstream → 200 with title, text, fetched_at. |
| `test_extract_article_empty_body` | Mock HTML that yields short text → 200 with `text: ""`, `note: "no extractable body"`. |
| `test_extract_article_disallowed_scheme` | `file:///etc/passwd` → 400. |
| `test_extract_article_upstream_error` | Mock 500 upstream → 500 + `{"error": "upstream 500"}`. |
| `test_allow_origin_override_replaces_default` | Custom `allow_origins=("https://other.example",)` rejects the default origin and accepts the custom one. |
| `test_fetch_article_v02_contract_preserved` (in `tests/test_articles.py`) | `fetch_article()` still returns the v0.2 tuple shape with the same error strings. |

`respx` mocks `httpx` calls in the server's request thread — respx patches at the transport layer process-wide, so cross-thread mocking just works without ceremony.

## 8. Release strategy

**Branch:** `v0.3` in `tenorune/bsky-saves`. All v0.3.0 work commits there; `main` stays at `v0.2.x` until merge.

**Cutover:**

1. Implement on the `v0.3` branch.
2. Merge `v0.3` → `main` (fast-forward).
3. Push `main`.
4. Create the `v0.3.0` release / tag via the GitHub UI (sandbox-side tag pushes hit the proxy 403 we've documented; release workflow fires on tag creation).
5. PyPI publishes `bsky-saves==0.3.0`.

**Consumer cutover (in `bsky-saves-gui`):**

1. Update the example `version` string in `bsky-saves-gui/docs/bsky-saves-serve-requirements.md` from `"0.2.4"` to `"0.3.0"` (or note that v0.3.0 is the first release containing `serve`).
2. Optionally add a `bsky-saves>=0.3.0` floor in any relevant install instructions or feature-detection logic. (The doc's `features` array remains the GUI's primary feature-detection mechanism; the version is informational.)

**Rollback:** if v0.3.0 has a critical bug, the `bsky-saves-gui` install instructions can pin `bsky-saves==0.2.3` until a fix lands. The v0.2.x wheels remain on PyPI indefinitely.

## 9. Security posture (recap)

These are reflected in the implementation as hard constraints, not configuration:

- Bind to `127.0.0.1` only. Not `0.0.0.0`. Not configurable.
- `/fetch-image` URL allowlist hardcoded to `cdn.bsky.app` + `*.bsky.app`. Not configurable.
- No authentication. The combination of `127.0.0.1` binding and origin allowlist is the entire auth layer.
- No persistence. No config files read. No state files written. No logs to disk.
- `--verbose` logs URLs to stderr only.
- No credentials accepted or stored.

## 10. Out of scope (deferred)

| Deferred | Trigger to revisit |
|---|---|
| Authenticated endpoints (sessions, JWT, shared secrets) | If `serve` ever needs to expose anything sensitive. |
| Streaming responses | If `/extract-article` routinely takes >15s and silence becomes a UX problem. |
| Rate limits / concurrency caps | If anyone reports daemon abuse. |
| Configurable `/fetch-image` URL allowlist | If a future consumer needs non-`bsky.app` image sources. |
| Auto-launch on boot, system-tray UI, daemon supervision | Not a daemon supervisor. Users run `bsky-saves serve` when they want it. |
| Windows service / macOS LaunchAgent integration | CLI command only. |
| `python -m bsky_saves.serve` shortcut | Single CLI entry: `bsky-saves serve`. |
| Logging to disk or external services | Hard no. |
| Persisting any config or state | Hard no. |
| Binding to anything other than `127.0.0.1` | Hard no. |
| Image format conversion / re-encoding | Bytes pass through. |
| `hydrate articles` persisting the new title field | Possibly future-useful (the shared helper now extracts it), but not in v0.3.0. The `fetch_article()` adapter discards it to preserve the v0.2 inventory contract. |
| Any change to existing `fetch` / `enrich` / `hydrate articles|threads|images` behavior | Out of scope. v0.3.0 is purely additive. |

## 11. Decisions log

| Date | Decision |
|---|---|
| 2026-05-04 | Version target: v0.3.0 (minor bump; new public surface). |
| 2026-05-04 | `serve` lives in `bsky-saves`; not a separate package. |
| 2026-05-04 | Single `serve.py` module; no submodule split. |
| 2026-05-04 | `http.server.ThreadingHTTPServer` (stdlib); no new dependency. |
| 2026-05-04 | Article extraction: shared private `_extract_article` helper, both `fetch_article` and `serve` adapt to it. Shape: `ArticleExtraction` dataclass returning text + title + date + fetched_at + short flag. |
| 2026-05-04 | `--allow-origin` semantics: explicit values fully replace the default; default applies only when no `--allow-origin` is passed. |
| 2026-05-04 | Image responses buffered in memory, not streamed. |
| 2026-05-04 | Test client side uses stdlib `urllib.request`; upstream mocks via `respx` (cross-thread). |
| 2026-05-04 | No `python -m bsky_saves.serve` shortcut. |
| 2026-05-04 | No `__main__.py`. |
