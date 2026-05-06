# bsky-saves v0.4 — `serve` adds `/fetch`, `/enrich`, `/hydrate-threads`

> **Status:** approved 2026-05-06. Implementation pending.
> **Branch:** `v0.4` in `tenorune/bsky-saves` (to be created).
> **External contract:** `https://github.com/tenorune/bsky-saves-gui/blob/main/docs/bsky-saves-serve-fetch-enrich-threads-requirements.md`. That document is canonical for the HTTP API (endpoints, request/response shapes, cursor encoding, auth posture, batching guidance, acceptance criteria). This document is canonical for the bsky-saves-side implementation.
> **Builds on:** `docs/superpowers/specs/2026-05-04-bsky-saves-v0.3-serve-subcommand.md` (the v0.3 daemon).

---

## 1. Context

`bsky-saves serve` shipped in v0.3 with three endpoints — `/ping`, `/fetch-image`, `/extract-article` — to bridge the two CORS-blocked operations `bsky-saves-gui` couldn't perform from the browser. The remaining three CLI operations (`fetch`, `enrich`, `hydrate threads`) ran in the GUI's Pyodide worker.

Pyodide carries real cost for users who already have the helper installed: ~10 MB WASM cold start per session, brittle pyodide-http shim, long-running thread walks blocking a Web Worker for minutes. Adding `/fetch`, `/enrich`, and `/hydrate-threads` to the daemon lets a helper-equipped user opt out of Pyodide entirely. Users without the helper continue to fall back to Pyodide; capability detection remains via `/ping`'s `features` array.

This is the partial step toward the v1-spec-mentioned `POST /run` (a one-shot fetch + enrich + threads + image hydration + article extraction round trip). The three granular endpoints in v0.4 prove the credential-handling, pagination-cursor, and inventory-shape patterns that `/run` will build on.

## 2. Scope

bsky-saves remains an *ingestion package*. v0.4.0 is **purely additive**:

- Adds three new HTTP endpoints (`/fetch`, `/enrich`, `/hydrate-threads`) inside the existing `serve` subcommand.
- Updates `/ping`'s `features` array to advertise them.
- Adds one new helper in `fetch.py` (`fetch_one_page`) — the existing `probe_bookmark_endpoints` and `fetch_to_inventory` keep their behavior.
- Does not change: any other subcommand, any inventory-schema field, the v0.3 endpoints, the CLI flag set.

### What's new in `serve`

A `127.0.0.1`-bound daemon that, in addition to the v0.3 endpoints, supports:

- `POST /fetch` — single-page bookmark enumeration with opaque pagination cursor. Auth required.
- `POST /enrich` — pure-offline TID-decoded `post_created_at` per URI. No auth.
- `POST /hydrate-threads` — same-author thread-reply hydration via concurrent fan-out. Auth required.

See the consumer-side requirements doc for the full HTTP API contract.

### What's not changing

- The v0.3 endpoints (`/ping`, `/fetch-image`, `/extract-article`).
- The CLI subparser (no new flags).
- The inventory schema (the GUI-facing JSON shapes mirror what `bsky-saves` writes to inventory; no fields added).
- The package's dependency set.

## 3. Architecture and module layout

### Files modified

| File | Change |
|---|---|
| `src/bsky_saves/serve.py` | Add three new endpoint handlers (`_handle_fetch`, `_handle_enrich`, `_handle_hydrate_threads`) plus small helpers (`_validate_creds`, `_encode_cursor`, `_decode_cursor`). Update `_handle_ping`'s `features` list. Update `ROUTES` table. Target: keep under ~600 lines; split lazily in a follow-up if needed. |
| `src/bsky_saves/fetch.py` | Add a public-ish `fetch_one_page(session, *, pds_base, appview_base, endpoint_id, cursor, limit) -> (chosen_id, raw, next_upstream)` helper. Single-page granularity that the daemon needs. Existing `probe_bookmark_endpoints` and `fetch_to_inventory` are unchanged. Add an `ENDPOINT_IDS` mapping for cursor encoding. |
| `pyproject.toml` | Bump `version = "0.4.0"`. |

### Files created

| File | Responsibility |
|---|---|
| (none in `src/`) | All daemon code lives in the existing `serve.py`. |
| `tests/test_serve.py` | (Existing.) Append per-endpoint tests for the three new endpoints; update one `/ping` test. |

No new dependencies. Existing stack: `httpx` (sync), stdlib `http.server`, stdlib `concurrent.futures.ThreadPoolExecutor`, stdlib `base64`, stdlib `json`. The doc's prior reference to *"async-httpx code"* was a misstatement — `bsky-saves` is sync httpx everywhere; we keep that.

### Concurrency model

- **Across requests**: `ThreadingHTTPServer` (already the v0.3 baseline). One thread per request. No shared lock.
- **Within `/hydrate-threads`**: `concurrent.futures.ThreadPoolExecutor(max_workers=5)`. The 5 workers issue concurrent `getPostThread` calls against the public AppView. Each worker uses sync `httpx` with the existing `threads.fetch_thread` helper. No async/await refactor required.
- **Within `/fetch`** and **`/enrich`**: serial (one upstream call max for `/fetch`; zero upstream calls for `/enrich`). Concurrency would be premature.

### Module organization

Single `serve.py` file, growing from ~265 lines (v0.3) to ~500-600 lines. No submodule split for v0.4. If `serve.py` crosses ~800 lines in a future release, split lazily.

## 4. CLI surface

**Unchanged.** `bsky-saves serve [--port PORT] [--allow-origin ORIGIN]... [--verbose]` ships exactly as v0.3. The startup line stays generic per spec §10 (forward-compat) of the v0.3 spec — describes the daemon, not its endpoint set.

## 5. `POST /fetch` implementation

### Contract recap (per requirements doc §1)

- Request: `{credentials: {handle, app_password, pds?}, cursor: null|opaque-string, limit: 100}`.
- Response: `{saves: [<inventory-shaped entry>...], cursor: opaque-string|null}`.
- Errors: `400 missing credentials`, `400 invalid cursor`, `401 createSession failed: <msg>`, `5xx`.
- Timeout: 30s per page.

### Cursor encoding

The wrapped cursor format (daemon-internal, opaque to the GUI):

```python
cursor_obj = {"v": 1, "endpoint": "<endpoint-id>", "upstream": "<upstream-cursor-or-None>"}
wrapped = base64.urlsafe_b64encode(
    json.dumps(cursor_obj, separators=(",", ":")).encode("utf-8")
).decode("ascii")
```

`<endpoint-id>` is one of:
- `"pds:bookmark.getBookmarks"`
- `"appview:bookmark.getBookmarks"`
- `"appview:getActorBookmarks"`
- `"pds:listRecords"`

These are stable string aliases for the entries in the existing `BOOKMARK_ENDPOINTS` list. Decoding catches all parse failures (base64 malformed, JSON malformed, unknown `v`, unknown `endpoint`, missing fields) and returns `None`; the handler then emits `400 invalid cursor`.

### Refactor in `fetch.py`

A new public-ish helper. Keeps `probe_bookmark_endpoints` and `fetch_to_inventory` untouched.

```python
ENDPOINT_IDS: dict[tuple[str, str], str] = {
    ("pds", "app.bsky.bookmark.getBookmarks"): "pds:bookmark.getBookmarks",
    ("appview", "app.bsky.bookmark.getBookmarks"): "appview:bookmark.getBookmarks",
    ("appview", "app.bsky.feed.getActorBookmarks"): "appview:getActorBookmarks",
    ("pds", "com.atproto.repo.listRecords"): "pds:listRecords",
}

def fetch_one_page(
    session: dict,
    *,
    pds_base: str,
    appview_base: str,
    endpoint_id: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> tuple[str, list[dict], str | None]:
    """Fetch ONE page of bookmarks. Returns (chosen_endpoint_id, raw_records, next_upstream_cursor).

    If endpoint_id is None: probes BOOKMARK_ENDPOINTS in fallback order until one
    succeeds. If endpoint_id is given: calls that endpoint directly, no probing.

    Raises NoBookmarkEndpointError if no endpoint succeeds (probe path).
    Raises _DirectEndpointFailedError if the named endpoint hard-fails (direct path) —
    serve.py catches this and falls back to a fresh probe.
    """
```

`_DirectEndpointFailedError` is a sentinel exception class defined in `fetch.py`.

### Handler outline

```python
def _handle_fetch(handler) -> None:
    body = handler._read_json_body()
    creds = _validate_creds((body or {}).get("credentials"))
    if creds is None:
        handler._send_json_error(400, "missing credentials")
        return

    raw_cursor = (body or {}).get("cursor")
    limit = int((body or {}).get("limit", 100))
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
        session = create_session(creds["pds"], creds["handle"], creds["app_password"])
    except httpx.HTTPStatusError as e:
        handler._send_json_error(401, f"createSession failed: {e}")
        return

    try:
        chosen_id, raw, next_upstream = fetch_one_page(
            session,
            pds_base=creds["pds"], appview_base=APPVIEW_BASE,
            endpoint_id=endpoint_id, cursor=upstream_cursor, limit=limit,
        )
    except _DirectEndpointFailedError:
        # Silent fallback: re-probe from a fresh state (per requirements doc §1).
        # The fresh probe drops the upstream cursor — the four bookmark
        # endpoints have incompatible cursor formats (e.g., pds:listRecords
        # uses an rkey-TID; bookmark.getBookmarks uses an opaque lexicon
        # cursor; getActorBookmarks uses a different opaque AppView cursor),
        # so carrying a cursor across endpoint boundaries risks either an
        # immediate error or — worse — a silently wrong page. Restarting
        # from page 1 is correct-by-construction; the cost is one round of
        # re-pagination, which the GUI absorbs as "the helper hiccupped."
        chosen_id, raw, next_upstream = fetch_one_page(
            session,
            pds_base=creds["pds"], appview_base=APPVIEW_BASE,
            endpoint_id=None, cursor=None, limit=limit,
        )
    except NoBookmarkEndpointError as e:
        handler._send_json_error(502, f"no working bookmark endpoint: {e}")
        return

    saves = [normalise_record(r) for r in raw]
    out_cursor = _encode_cursor(chosen_id, next_upstream) if next_upstream else None
    handler._send_json(200, {"saves": saves, "cursor": out_cursor})
```

### Credential validation rule

`_validate_creds(obj)` returns the credentials dict (with `pds` filled in if missing) on success, or `None` on failure. Required: `handle`, `app_password`. Optional: `pds`, defaulting to `"https://bsky.social"` when absent or empty (matches the CLI's `BSKY_PDS` default behavior). Same helper is reused by `/hydrate-threads`.

### Constants

- `APPVIEW_BASE = "https://bsky.social"` — the AppView host used by the bookmark-endpoint probe in `/fetch`. Same default the v0.2 CLI fetch uses (`fetch_to_inventory`'s `appview_base` default). For most probe configurations this host is never actually called — the PDS-direct path wins first — but it's the fallback target when probing reaches the AppView entries in `BOOKMARK_ENDPOINTS`.

  **Note**: this is a different AppView host from `PUBLIC_APPVIEW` used in `/hydrate-threads` (see §7). The two endpoints address two AppView-shaped surfaces: `bsky.social` is the auth-ed AppView reachable from a logged-in session; `public.api.bsky.app` is the unauthenticated public AppView used for thread reads. We keep them as separate constants because they're addressing different lexicon surfaces with different auth assumptions.

## 6. `POST /enrich` implementation

### Contract recap (per requirements doc §2)

- Request: `{uris: [<at-uri>...]}` — no credentials.
- Response: `{enriched: [{uri, post_created_at}...], errors: [{uri, reason}...]}`.
- Errors: `400 missing uris` (top-level field absent), `5xx daemon failure`.
- Timeout: sub-second.

### Logic

Pure offline. Reuses `bsky_saves.tid.rkey_of` and `bsky_saves.tid.decode_tid_to_iso` (the same functions `enrich.enrich_inventory` already calls).

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
            errors.append({"uri": uri if isinstance(uri, str) else "", "reason": "invalid at-uri"})
            continue
        try:
            post_created_at = decode_tid_to_iso(rkey_of(uri))
        except Exception:
            errors.append({"uri": uri, "reason": "invalid at-uri"})
            continue
        enriched.append({"uri": uri, "post_created_at": post_created_at})

    handler._send_json(200, {"enriched": enriched, "errors": errors})
```

### Notes

- Per-URI failures use the static reason string `"invalid at-uri"`, matching the requirements doc's example. We don't include exception class names or messages — the requirements doc collapsed this to a single value.
- A request body with `credentials` set is accepted (no 400); the field is just unused.
- `{"uris": []}` returns `200 {"enriched": [], "errors": []}` — not an error, follows the "do what's asked" posture.
- No batching, no concurrency, no rate limit — pure-function string parsing on the input list.

## 7. `POST /hydrate-threads` implementation

### Contract recap (per requirements doc §3)

- Request: `{uris: [<at-uri>...], credentials: {handle, app_password, pds?}}`.
- Response: `{threaded: [{uri, thread_replies, thread_schema_version, thread_fetched_at}...], errors: [{uri, reason}...]}`.
- Errors: `400 missing credentials`, `400 missing uris`, `401 createSession failed`, `5xx`.
- Timeout: 300s.

### Auth path

Validate-only. The daemon calls `create_session(creds.pds, creds.handle, creds.app_password)` once at request entry to fail-fast on a bad app password. The resulting JWT is **discarded immediately** — never used for any upstream call. The actual thread fetches go to `https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread` with no `Authorization` header, matching the working pattern of `bsky-saves`'s CLI thread hydration.

### Concurrency

`concurrent.futures.ThreadPoolExecutor(max_workers=5)` per request. Five workers comfortably under public AppView's published rate budget (3000 req / 5min) with headroom for retries. No rate-limit sleep between calls (the CLI's 0.5s `RATE_LIMIT_SEC` is a per-process batch behavior; not appropriate for an interactive helper).

### Order preservation

The `threaded` array MUST preserve input-URI order. (The requirements doc is silent on order, but the GUI's `parseInventory` may rely on positional matching with input.) Implementation: build a list of `(uri, future_or_invalid_marker)` tuples in input order, then walk that list sequentially calling `.result()` — concurrency is preserved (all 5 workers run simultaneously; we just consume their results in input order). Both `threaded` and `errors` end up in input-URI order.

### Per-URI error reasons

Diagnostic when available, falling back to a generic message:

- `"invalid at-uri"` — input was not a non-empty string.
- The string returned by `threads.fetch_thread` (e.g., `"http_404"`, `"fetch_error:ConnectError:..."`) when the thread fetch failed.
- `"thread fetch failed"` — generic catch when `fetch_thread` returns `(None, None)` (shouldn't happen, but guarded).

### Handler outline

```python
PUBLIC_APPVIEW = "https://public.api.bsky.app"

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

    # Validate creds; we don't keep the JWT.
    try:
        create_session(creds["pds"], creds["handle"], creds["app_password"])
    except httpx.HTTPStatusError as e:
        handler._send_json_error(401, f"createSession failed: {e}")
        return

    def fetch_one(uri: str) -> tuple[str, dict | None, str | None]:
        thread, error = fetch_thread(uri, appview=PUBLIC_APPVIEW)
        if thread is None:
            return uri, None, error or "thread fetch failed"
        post_author_did = (
            thread.get("post", {}).get("author", {}).get("did", "")
            if isinstance(thread, dict) else ""
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

### Reuses

- `bsky_saves.threads.fetch_thread(uri, *, appview, user_agent)` — already returns `(thread, error)`. Existing function, no refactor.
- `bsky_saves.threads.collect_same_author_replies(thread, author_did)` — already correct after v0.3.1's chain-broken fix.
- `bsky_saves.threads.THREAD_SCHEMA_VERSION` — currently `4`.
- `bsky_saves.auth.create_session` — for credential validation only.

## 8. `/ping` features array update

One-line change in `_handle_ping`:

```python
"features": ["fetch-image", "extract-article", "fetch", "enrich", "hydrate-threads"]
```

Order matches the requirements doc (the doc says order doesn't matter, but lining up makes diffs readable). Hardcoded constant. The existing `test_ping_returns_name_version_features` test updates its expected `features` to match.

## 9. Test strategy

All test additions in `tests/test_serve.py` (existing file). Reuse the `serve_in_background` context manager and `_request` helper. Mock upstream HTTP via `respx`.

### `/fetch` tests

| Test | Verifies |
|---|---|
| `test_fetch_first_page_probes_and_returns_cursor` | `cursor: null` → probe runs, first page returned with wrapped cursor. |
| `test_fetch_continuation_skips_probe_via_cursor` | Wrapped cursor → daemon decodes, calls only the named endpoint, no re-probe. |
| `test_fetch_response_shape_matches_normalise_record` | Each `saves[]` entry has the exact field set produced by `normalise_record`. |
| `test_fetch_invalid_cursor_returns_400` | Garbage cursor → 400 `{"error": "invalid cursor"}`. |
| `test_fetch_missing_credentials_returns_400` | No credentials → 400. |
| `test_fetch_pds_defaults_to_bsky_social_when_omitted` | Credentials without `pds` → daemon uses `https://bsky.social` for createSession. |
| `test_fetch_createsession_failure_returns_401` | Mocked 401 from createSession → 401 with diagnostic message. |
| `test_fetch_silent_fallback_on_endpoint_failure` | Continuation cursor whose endpoint returns 5xx → daemon re-probes, returns next page from new winner. The new cursor encodes the new winner. |
| `test_fetch_no_more_pages_returns_null_cursor` | Upstream returns no `cursor` → response `cursor: null`. |
| `test_fetch_limit_clamping` | `limit: 999` clamped to 100; `limit: 0` clamped to 1. |
| `test_fetch_cursor_round_trips_byte_for_byte` | Wrapped cursor a daemon emits, when sent back, decodes to the same `{v, endpoint, upstream}` triple. |

### `/enrich` tests

| Test | Verifies |
|---|---|
| `test_enrich_decodes_post_created_at_for_each_uri` | Valid at-URIs → `enriched` populated in input order. |
| `test_enrich_invalid_uri_lands_in_errors` | Empty / non-string / malformed at-URI → `errors[]` with `reason: "invalid at-uri"`. |
| `test_enrich_missing_uris_field_returns_400` | Body without `uris` → 400 `{"error": "missing uris"}`. |
| `test_enrich_empty_uris_list_returns_200_with_empty_arrays` | `{"uris": []}` → 200 with `{enriched: [], errors: []}`. |
| `test_enrich_credentials_field_is_ignored` | Body with `credentials` is accepted (no 400); credentials are unused. |
| `test_enrich_mixed_valid_and_invalid` | Both arrays populated, input order preserved within each. |

### `/hydrate-threads` tests

| Test | Verifies |
|---|---|
| `test_hydrate_threads_returns_threaded_in_input_order` | Concurrent fan-out doesn't shuffle the response order. |
| `test_hydrate_threads_thread_replies_uses_v4_chain_logic` | A reply tree with OP-to-other-commenter responses yields no thread_replies; a true self-thread chain yields the chain. |
| `test_hydrate_threads_per_uri_failure_lands_in_errors_with_diagnostic` | Mocked 404 upstream → that URI in `errors` with `reason: "http_404"`; other URIs in `threaded`. |
| `test_hydrate_threads_credentials_validated_via_create_session` | Mock observes daemon called createSession once with the request's `handle/app_password/pds`. |
| `test_hydrate_threads_invalid_credentials_returns_401` | Mocked 401 from createSession → 401, no upstream `getPostThread` calls made. |
| `test_hydrate_threads_uses_public_appview_unauthenticated` | Mock asserts the request to `getPostThread` had no `Authorization` header. |
| `test_hydrate_threads_concurrency_caps_at_5` | 20 URIs in input → mock observes at most 5 concurrent `getPostThread` calls. Uses a `threading.Lock`-guarded counter inside the mock. |
| `test_hydrate_threads_invalid_uri_in_input` | Non-string / empty-string entry → `errors[]` with `reason: "invalid at-uri"`; other URIs unaffected. |
| `test_hydrate_threads_missing_credentials_returns_400` | No credentials → 400. |
| `test_hydrate_threads_missing_uris_returns_400` | No uris → 400. |

### `/ping` test update

`test_ping_returns_name_version_features` updates its expected `features` to `["fetch-image", "extract-article", "fetch", "enrich", "hydrate-threads"]`.

### `fetch.fetch_one_page` unit tests

Not added separately. Covered indirectly by the `/fetch` tests above. If the integration tests get hard to write because of probe-internals coupling, revisit.

### Test count delta

- Existing (after v0.3.1): 101 tests.
- New: ~26 (`11 /fetch + 6 /enrich + 9 /hydrate-threads`), plus 1 update to existing `/ping` test.
- Target after v0.4.0: ~127 tests passing.

## 10. Forward compatibility (Phase 2 awareness)

The eventual `POST /run` endpoint (mentioned in the v1 requirements doc's Phase 2 section) is **not part of v0.4.0**. v0.4.0's three new endpoints are the partial step toward it: they prove the credential-handling, pagination-cursor, and inventory-shape patterns that `/run` will compose.

Design choices to keep `/run` cheap to add later (already incidentally true after v0.4.0):

- **ROUTES table** extends mechanically — adding `/run` is one table entry.
- **`_validate_creds` helper** is reusable for `/run`'s credential parsing.
- **`fetch_one_page` helper** is reusable for `/run`'s bookmark enumeration.
- **`fetch_thread` + `collect_same_author_replies`** are reusable for `/run`'s thread step.
- **TID-decode for `post_created_at`** is reusable for `/run`'s enrich step.
- **`features` array advertisement** is the GUI's discovery mechanism — appending `"run"` is a one-line change when the time comes.

`/run`-specific design questions (binary blob serialization, partial-failure semantics, response size limits, auth caching across the multi-step pipeline) are deferred to a future spec when the GUI team is ready to design it.

## 11. Out of scope (deferred)

| Deferred | Trigger to revisit |
|---|---|
| `POST /run` endpoint | When the consumer team is ready to design v2 — separate spec. |
| Streaming responses (SSE / chunked JSON for slow `/hydrate-threads`) | If the GUI needs progress reporting during multi-minute thread walks. |
| Configurable batch size / concurrency knobs | If a consumer needs different defaults. Hardcoded to 5 workers, `limit ≤ 100`. No CLI flags. |
| Disk caching of `getPostThread` responses | If the daemon ever gains a "warm cache" mode. v0.4 stays stateless. |
| OAuth flow (replacing app passwords) | Tracked separately in the broader BlueSky ecosystem. |
| Authenticated AppView calls (vs current public AppView no-auth) | If the public AppView ever requires auth for thread reads. The "OAuth tokens are meant for PDS access only" wall is the reason this isn't done now. |
| `cid` capture in `/fetch` saves entries | If/when `bsky-saves`'s `normalise_record` starts capturing `cid`. The endpoint will inherit it automatically. |
| `display_name` refresh / profile data in `/enrich` | Not in v0.4 (CLI's enrich is offline-only). When the CLI's enrich step grows to populate networked fields, `/enrich`'s contract is re-opened — appropriately a major-version concern, not v0.4 scope. |
| Async/await refactor of `httpx` calls | Not needed. Sync httpx + `ThreadPoolExecutor` satisfies the concurrency requirement without an async port. |

## 12. Decisions log

| Date | Decision |
|---|---|
| 2026-05-06 | Version target: v0.4.0 (minor bump; new endpoints). |
| 2026-05-06 | All new endpoints in existing `serve.py`; no submodule split. |
| 2026-05-06 | Refactor: new `fetch.fetch_one_page` helper for single-page granularity; existing `probe_bookmark_endpoints` and `fetch_to_inventory` unchanged. |
| 2026-05-06 | Cursor encoding: `urlsafe-base64(JSON({v: 1, endpoint, upstream}))`, opaque to GUI. Daemon's private contract. |
| 2026-05-06 | Silent endpoint fallback: when a wrapped cursor's named endpoint hard-fails, daemon re-probes and emits a fresh cursor pointing at the new winner. The fallback **drops the upstream cursor** and restarts from page 1 — the four bookmark endpoints have incompatible cursor formats, so cross-endpoint cursor reuse risks silently wrong pages. The GUI absorbs one round of re-pagination on fallback as "the helper hiccupped." Invisible to GUI other than the latency bump. |
| 2026-05-06 | Credentials shape: `{handle, app_password, pds?}`. `pds` defaults to `"https://bsky.social"` when absent (matches CLI behavior). `handle` and `app_password` are required; absent → 400 missing credentials. |
| 2026-05-06 | `/enrich`: pure offline TID decode, no credentials required, sub-second latency, per-URI failure reason is the static string `"invalid at-uri"`. |
| 2026-05-06 | `/hydrate-threads` concurrency: `ThreadPoolExecutor(max_workers=5)` per request. No rate-limit sleep between upstream calls. |
| 2026-05-06 | `/hydrate-threads` auth path: validate via `create_session`, discard JWT, call `https://public.api.bsky.app` unauthenticated. |
| 2026-05-06 | `/hydrate-threads` order: `threaded` and `errors` arrays preserve input-URI order. |
| 2026-05-06 | Per-URI error reasons in `/hydrate-threads`: diagnostic when available (e.g., `"http_404"`), `"thread fetch failed"` as fallback, `"invalid at-uri"` for non-string input. |
| 2026-05-06 | Sync httpx everywhere (correcting the requirements doc's "async-httpx" misstatement). No async port required. |
