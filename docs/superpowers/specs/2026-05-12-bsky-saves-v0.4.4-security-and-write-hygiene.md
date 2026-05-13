# bsky-saves v0.4.4 — security hardening and write hygiene

> **Status:** approved 2026-05-12. Implementation pending.
> **Branch:** `claude/bsky-saves-next-phase-o7mOi` in `tenorune/bsky-saves`.
> **Releases on:** PyPI as `bsky-saves==0.4.4`. The v0.5.0 GUI-vendoring work builds on this release.
> **External coordination:** the GUI team's MVP spec (`bsky-saves-gui/docs/bsky-saves-mvp-spec.md`) needs a one-paragraph edit to §5.3 to document the new SSRF guard. Diff in §11 below.
> **Builds on:** `docs/superpowers/specs/2026-05-06-bsky-saves-v0.4-serve-fetch-enrich-threads.md`.

---

## 1. Context

Two threads converge in this release:

1. **The GUI team's MVP spec** (`bsky-saves-gui/docs/bsky-saves-mvp-spec.md`) requires four daemon-side security tightenings — Origin enforcement returns explicit `403`, `--allow-origin` becomes additive, the default origin allowlist gains the two loopback origins, and `Host` header validation rejects DNS-rebinding attempts with `421`. These are the prerequisites for v0.5.0's `--gui` flag to safely serve the bundled PWA bundle on `127.0.0.1`.

2. **A defensive audit of the daemon and CLI surfaces** (driven by the VibeSec checklist) surfaced nine findings beyond the GUI team's spec — primarily SSRF vectors via user-controlled URLs reaching internal IPs, a missing request-body cap, and a handful of cheap defense-in-depth omissions.

Plus two latent code-hygiene items flagged in the v0.4.3 handoff that touch the same files and are cheap to fold in: inconsistent atomic-write coverage across the inventory-writing modules, and stale `User-Agent` strings frozen at `bsky-saves/0.1` and `bsky-saves/0.2`.

v0.4.4 ships all three threads as one release. The version bump is the patch slot because none of the changes alter the public HTTP API contract — they tighten existing behavior, not add new endpoints. v0.5.0 stays reserved for the GUI vendoring + `--gui` work.

## 2. Scope

bsky-saves remains an *ingestion package*. v0.4.4 is **non-additive**: no new endpoints, no new CLI subcommands, no new inventory fields.

### In scope

- **Origin enforcement** on every API route returns `403 {"error":"Origin not allowed"}` for non-allowlisted origins (currently silently drops the CORS header).
- **Additive `--allow-origin`** that augments rather than replaces the default allowlist.
- **Default allowlist** gains `http://127.0.0.1:<port>` and `http://localhost:<port>` alongside the existing `https://saves.lightseed.net`.
- **`Host` header validation** rejects any value other than `127.0.0.1:<port>` or `localhost:<port>` with `421 Misdirected Request`.
- **SSRF guard** on user-supplied URLs across `/extract-article`, `/fetch` (`pds` field), `/hydrate-threads` (`pds` field), CLI `hydrate articles`, and CLI `hydrate images`. Rejects private/loopback/link-local/CGNAT/multicast/reserved IP destinations, including DNS aliases that resolve to those ranges.
- **Request body size cap** of 10 MB applied to every POST endpoint; oversize bodies return `413`.
- **Response security headers** `X-Content-Type-Options: nosniff` and `Cache-Control: no-store` on every daemon response.
- **Redirect handling** on outbound HTTP fetches that accept user-influenced URLs: redirects walked manually with the SSRF guard re-applied to each hop.
- **Verbose-log control-char sanitization** in `serve.py`'s request log.
- **Trafilatura XXE posture** verified and pinned via a minimum-version constraint in `pyproject.toml`.
- **Shared atomic-write helper** in a new `_io.py` module; all five inventory-writing callsites (`fetch`, `enrich`, `articles`, `threads`, `images`) migrate to it.
- **`User-Agent` strings** in `images.py` and `articles.py` derive from `__version__` so they don't go stale again.

### Out of scope (explicitly deferred)

- **v0.5.0 work**: GUI vendoring build hook, `--gui` flag, static file serving with SPA fallback, CSP / X-Frame-Options / COOP / Referrer-Policy headers on static responses, pre-release CI smoke test. Tracked separately.
- **`limit` kwarg for `hydrate_articles` and HTTP `/hydrate-threads`**. The v0.4.3 `limit` only landed on the Python `hydrate_threads()` function. Extending it elsewhere is not requested.
- **DNS-rebinding mitigation by pinned-IP connect** for outbound fetches. v0.4.4's pre-resolve + reject is sufficient for the daemon's localhost-bound threat model; the residual window between pre-resolve and httpx connect is much harder to weaponise than the static-IP attacks v0.4.4 closes. Custom transport hooks are a v0.5.x stretch goal.
- **Per-route body-size differentiation**. A uniform 10 MB cap is simpler and easily covers worst-case `/fetch` URI batches.
- **HTTPS-only enforcement on `/extract-article`**. The spec §5.3 explicitly permits both `http` and `https` because article URLs are "the open web." We keep both.
- **Sigstore / cosign signing** of any artifacts. Stretch goal.

## 3. Architecture and module layout

### Files modified

| File | Change |
|---|---|
| `src/bsky_saves/serve.py` | Add `_security_gate()` method to `make_handler`'s `Handler` class. Gate called from `do_GET`, `do_POST`, `do_OPTIONS`, and the `__getattr__` unknown-verb fallback before `_dispatch`. Default allowlist computation moves from `run_serve` into a helper that takes `port` and `extra_origins`. `_read_json_body` gains a 10 MB size cap. `_send_json` and `_send_bytes` add `nosniff` + `no-store` headers. `_handle_extract_article` and `_validate_creds` add SSRF checks. `_handle_fetch_image` switches to manual redirect-walking. `_log_request` escapes control characters in `self.path`. |
| `src/bsky_saves/cli.py` | Help text update on `--allow-origin` noting additive behavior. No new flags. |
| `src/bsky_saves/_io.py` | **New module.** Single function `atomic_write_inventory(path: Path, inv: dict) -> None`. Adapted verbatim from the current `threads._atomic_write_inventory` (same JSON formatting: `indent=2, sort_keys=True, ensure_ascii=False`, trailing newline; same `os.replace` semantics). |
| `src/bsky_saves/_net.py` | **New module.** SSRF guard helpers: `assert_public_http_url(url, *, allow_http=False)` raises `UnsafeURLError` for unsafe URLs; `safe_http_get(url, *, allow_http=False, max_redirects=5, hop_check=None, **httpx_kwargs)` walks redirects manually with the guard re-applied per hop. |
| `src/bsky_saves/threads.py` | Delete the local `_atomic_write_inventory`; import from `_io`. |
| `src/bsky_saves/images.py` | Replace the inline temp+rename block (currently `os.rename`) with `atomic_write_inventory`. Switch the inline `httpx.get` in `download_to` to `safe_http_get`. Rewrite `DEFAULT_USER_AGENT` to derive from `__version__`. |
| `src/bsky_saves/fetch.py` | Replace direct `write_text` in `fetch_to_inventory` with `atomic_write_inventory`. |
| `src/bsky_saves/enrich.py` | Replace direct `write_text` with `atomic_write_inventory`. |
| `src/bsky_saves/articles.py` | Replace direct `write_text` with `atomic_write_inventory`. Switch the inline `httpx.get` in `_extract_article` to `safe_http_get`. Rewrite `DEFAULT_USER_AGENT` to derive from `__version__`. |
| `pyproject.toml` | Pin a minimum `trafilatura` version known to ship XXE-safe lxml defaults. Specific floor determined during implementation; documented in the PR. |
| `tests/test_serve.py` | Flip 3 existing assertions (disallowed-origin now returns `403`); add ~20 new tests covering Host validation, additive allowlist, body-size cap, response headers, SSRF rejections on each user-URL endpoint, manual redirect walking, and control-char log escaping. |
| `tests/test_net.py` | **New file.** Unit tests for the SSRF guard helper covering: public hostnames, IP literals across every notation (decimal/octal/hex/IPv6/IPv4-mapped), DNS aliases for loopback/private/metadata, scheme rejection, malformed URLs, redirect-walking happy path, and redirect-to-private-IP rejection. |
| `tests/test_io.py` | **New file.** Two tests on `atomic_write_inventory`: happy-path write produces correct content, and the `.tmp` sidecar is gone after success. |
| `tests/test_articles.py`, `tests/test_images.py` | Add SSRF-rejection tests for the CLI paths. |

### Module-boundary intent

`_io.py` and `_net.py` are private utility modules (leading underscore). They are not part of the public API and may be refactored freely between releases. They exist primarily to centralise behavior that was previously duplicated or absent, so a single fix covers all callsites.

## 4. Section A — security gate (Host + Origin enforcement)

### 4.1 Gate placement

A new method `_security_gate(method: str) -> bool` on the handler. Returns `True` to allow the request to proceed to `_dispatch`; returns `False` after having already sent an error response.

Called as the first action in `do_GET`, `do_POST`, `do_OPTIONS`, and the `__getattr__` unknown-verb fallback:

```python
def do_POST(self) -> None:
    self._log_request()
    if not self._security_gate("POST"):
        return
    self._dispatch("POST")
```

### 4.2 Check ordering

1. **`Host` header.** Accept exactly the literal strings `127.0.0.1:<port>` or `localhost:<port>`. Reject everything else with `421 Misdirected Request` + body `{"error":"misdirected request"}`. Rejected cases include: empty Host, missing Host, wrong port, IPv6 (`[::1]:<port>`), trailing-dot DNS (`localhost.:<port>`), any other hostname or IP that happens to resolve to loopback.
2. **`Origin` header.** If present and not in the allowlist, reject with `403 Forbidden` + body `{"error":"Origin not allowed"}`. If absent, allow (curl-style is permitted per MVP spec §4.4). Same gate applies to `OPTIONS` preflight — disallowed-origin preflight gets `403`, not the previously-unconditional `204`.

Host first is the cheaper check and the more fundamental defense (catches DNS rebinding regardless of method/origin).

### 4.3 Default allowlist

`run_serve(port, allow_origins=None)` computes its defaults from the bound port:

```python
def _default_origins(port: int) -> list[str]:
    return [
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        "https://saves.lightseed.net",
    ]
```

The effective origin list becomes `_default_origins(port) + list(allow_origins or [])`. Today's `allow_origins or defaults` (replacement) becomes additive. The MVP spec §4.4 calls this out as a footgun fix; passing `--allow-origin foo` no longer silently drops the hosted GUI's origin.

### 4.4 CORS interaction

`_cors_headers()` keeps its current shape: echo Origin only if allowlisted, set `Methods`, `Headers`, `Max-Age: 600`. The disallowed-origin path no longer reaches this method (the gate returns first).

`Access-Control-Allow-Origin` is never `*`.

### 4.5 Existing test churn

Three tests in `tests/test_serve.py` flip their assertions from "Allow-Origin header absent" to "status 403, body `{"error":"Origin not allowed"}`":

- `test_cors_disallowed_origin_omits_allow_origin_header` (renamed to `test_cors_disallowed_origin_returns_403`)
- `test_allow_origin_override_replaces_default` (renamed to `test_allow_origin_additive_keeps_defaults`)
- `test_multiple_allow_origins_all_allowed` (one assertion flips)

`test_cors_no_origin_header_request_succeeds` does not change — missing Origin stays permitted.

## 5. Section B — SSRF guard for user-supplied URLs

### 5.1 The helper

`src/bsky_saves/_net.py` exposes:

```python
class UnsafeURLError(ValueError):
    """Raised when a URL targets a non-public IP or is otherwise unsafe."""

def assert_public_http_url(url: str, *, allow_http: bool = False) -> None:
    """Raise UnsafeURLError if the URL is malformed, uses a disallowed scheme,
    or resolves to a private/loopback/link-local/CGNAT/multicast/reserved IP."""

def safe_http_get(
    url: str,
    *,
    allow_http: bool = False,
    max_redirects: int = 5,
    hop_check: Callable[[str], None] | None = None,
    **httpx_kwargs,
) -> httpx.Response:
    """Like httpx.get, but walks redirects manually with assert_public_http_url
    re-applied to each hop. Caller's optional hop_check runs before the SSRF
    check on each hop (used by /fetch-image to enforce the bsky.app allowlist
    on every redirect target). Disables httpx's own follow_redirects."""
```

### 5.2 The check algorithm

`assert_public_http_url` proceeds in this order:

1. Parse with `urllib.parse.urlparse`. Raise on parse failure or empty hostname.
2. Require scheme in `{"https"}` (or `{"http", "https"}` when `allow_http=True`). Raise otherwise.
3. Normalise the hostname: lowercase, strip trailing dot, reject if it equals `localhost` or ends with `.localhost`.
4. Try to parse the hostname as an `ipaddress.ip_address`. If it parses (IP literal), check directly. If not (DNS name), `socket.getaddrinfo` and check every returned address.
5. For each IP address in scope, reject if any of:
   - `is_private` (covers RFC 1918)
   - `is_loopback`
   - `is_link_local` (covers `169.254/16` and `fe80::/10` — AWS / GCP / Azure metadata)
   - `is_multicast`
   - `is_reserved`
   - `is_unspecified` (`0.0.0.0`, `::`)
   - In IPv4-mapped IPv6 (`::ffff:x.x.x.x`): unwrap and re-check
   - In CGNAT range `100.64.0.0/10` (Python's `is_private` does not include this in older versions — explicit check)

### 5.3 Callsites

| Callsite | Function | `allow_http` | Notes |
|---|---|---|---|
| `serve._handle_extract_article` | via `_extract_article` shared with CLI | `True` | Open-web allowlist per MVP spec §5.3, narrowed by SSRF guard |
| `serve._validate_creds` (`pds` field) | inline check on the parsed `pds` URL | `False` | Require HTTPS; MVP spec defaults `pds` to `https://bsky.social` |
| `serve._handle_fetch_image` | via `safe_http_get` with `hop_check=_is_allowed_image_url` | `False` | Existing bsky.app allowlist runs per-hop; SSRF guard is belt-and-suspenders |
| `articles._extract_article` | via `safe_http_get` | `True` | Same code path as the HTTP endpoint |
| `images.download_to` | via `safe_http_get` | `False` | Bsky CDN URLs are HTTPS; tighten now |

The `pds`-field guard runs at validation time inside `_validate_creds`, before any outbound HTTP. Bad `pds` returns the existing `400 {"error":"missing credentials"}` (the field is treated as invalid input rather than an upstream failure).

### 5.4 Behavior on rejection

- HTTP endpoints (`/extract-article`, `/fetch-image`): return `400 {"error":"url not allowed"}` — matches the existing rejection shape for the bsky.app allowlist on `/fetch-image`.
- `/fetch` and `/hydrate-threads` with bad `pds`: return `400 {"error":"missing credentials"}` (existing shape; the field is treated as part of the credentials object).
- CLI `hydrate articles` and `hydrate images`: log the rejection to stderr and skip the entry. Inventory hydration is best-effort — one bad URL doesn't fail the run. The skipped entry retains no `local_images` or `article_text`/`article_fetch_error` field.

### 5.5 DNS-rebinding posture

The pre-resolve + reject approach has a TOCTOU gap between our `getaddrinfo` and httpx's actual connect. Closing it requires a custom httpx transport that re-validates at TCP-connect time. Out of scope for v0.4.4; the localhost-bound daemon's threat model doesn't include a network attacker poisoning DNS within a millisecond-scale window. Deferred to v0.5.x as a stretch.

## 6. Section C — request body size cap

A new module constant `_MAX_BODY_BYTES = 10 * 1024 * 1024`. `_read_json_body` checks `Content-Length` before reading. Behavior:

- `Content-Length` missing or 0 → return `None` (no body); caller treats as malformed.
- `Content-Length` > `_MAX_BODY_BYTES` → method sends `413 {"error":"request too large"}` itself and returns a sentinel (`_BODY_REJECTED`) distinct from `None`.
- Otherwise → read, parse, return the parsed dict (or `None` if parse fails / not a dict).

Callers handle the three cases:

- `body is _BODY_REJECTED`: the 413 was already sent; caller returns silently.
- `body is None`: caller sends its own `400 {"error":"missing X"}` (existing behavior).
- `body is dict`: dispatch to per-handler logic.

The 10 MB cap is comfortably above any legitimate usage (a realistic `/fetch` body is single-digit KB; `/hydrate-threads` with a few hundred URIs is under 100 KB) while bounding the worst-case memory blow-up.

## 7. Section D — response security headers

`_send_json` and `_send_bytes` add two headers before `end_headers()`:

```python
self.send_header("X-Content-Type-Options", "nosniff")
self.send_header("Cache-Control", "no-store")
```

`do_OPTIONS` similarly adds them before `end_headers()`. Applies uniformly to success responses, error responses, and preflight 204s.

These are belt-and-suspenders:

- `nosniff` prevents browsers from MIME-sniffing a JSON or image response as HTML/script — irrelevant inside CORS, but a defense against misconfigurations downstream.
- `no-store` prevents browser/proxy caching of auth-bearing responses or image bytes. `/ping` doesn't need it strictly, but applying uniformly keeps the policy simple.

## 8. Section E — redirect handling on `/fetch-image`

`_handle_fetch_image` switches from `httpx.get(url, follow_redirects=True, ...)` to `safe_http_get` with a `hop_check` callback enforcing the existing bsky.app allowlist on every redirect target:

```python
def _enforce_bsky_cdn(u: str) -> None:
    if not _is_allowed_image_url(u):
        raise UnsafeURLError("not a bsky.app CDN URL")

r = safe_http_get(
    url,
    allow_http=False,
    max_redirects=3,
    hop_check=_enforce_bsky_cdn,
    headers={"User-Agent": _IMAGE_USER_AGENT, "Accept": "image/*"},
    timeout=_IMAGE_TIMEOUT,
)
```

Per-hop check order: `hop_check` runs first (cheap allowlist), then the SSRF resolve+check. A redirect to a non-bsky host is rejected as `400 {"error":"url not allowed"}`. A redirect to a bsky.app host that resolves to a private IP (defense against a hypothetical bsky.app DNS compromise) is also rejected.

`max_redirects=3` covers any plausible CDN failover; CDN responses for static assets typically don't redirect at all.

## 9. Section F — verbose log control-char sanitization

`_log_request` switches from:

```python
print(f"bsky-saves: {self.command} {self.path}", file=sys.stderr)
```

to:

```python
safe_path = self.path.encode("ascii", "backslashreplace").decode("ascii")
print(f"bsky-saves: {self.command} {safe_path}", file=sys.stderr)
```

Replaces any non-ASCII or control byte with a backslash-escape. Two lines of code; closes the very-low-severity terminal-escape-injection vector.

## 10. Section G — trafilatura XXE posture

Procedure during implementation:

1. Read the currently-installed trafilatura's source to confirm `bare_extraction` configures the underlying lxml HTMLParser with `resolve_entities=False` (or that lxml's HTMLParser default is safe — lxml's HTMLParser, distinct from XMLParser, does not resolve external entities by default).
2. Find the earliest trafilatura release on PyPI that ships that configuration. If current trafilatura already has it, pin a recent stable release (e.g. the version pulled by `pip install trafilatura` today) as a "tested baseline" floor.
3. Add a lower bound to `pyproject.toml`'s `trafilatura` dependency line (currently unbounded). Format: `trafilatura>=<min-version>`.
4. Add a one-line comment in `articles.py` near the import noting the dependency on safe lxml defaults and the minimum-version contract.

**Escalation path:** if the verification surfaces that current trafilatura uses an unsafe parser configuration, stop and report to the project owner. That would change v0.4.4 scope and likely require pre-processing HTML to strip `<!DOCTYPE>` declarations before handing to trafilatura, or switching extraction libraries.

## 11. Section H — atomic-write helper and User-Agent bump

### 11.1 Atomic-write helper

New `src/bsky_saves/_io.py`:

```python
"""Low-level inventory I/O helpers shared by every write callsite."""
from __future__ import annotations

import json
import os
from pathlib import Path


def atomic_write_inventory(path: Path, inv: dict) -> None:
    """Write inv to path via temp-file + os.replace. Crash-safe.

    Same JSON formatting as every other inventory writer in the package:
    indent=2, sort_keys=True, ensure_ascii=False, trailing newline.
    os.replace is atomic on POSIX and cross-platform on Windows (unlike
    os.rename, which fails if the destination exists on Windows).
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(inv, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
```

Five migrations:

- `threads.py`: delete the local `_atomic_write_inventory`; `from ._io import atomic_write_inventory`. Update both callsites (the per-iteration and end-of-run flush).
- `images.py`: replace the inline temp+rename block at `images.py:158-164`. Drive-by upgrade from `os.rename` to `os.replace`.
- `fetch.py`: replace `inventory_path.write_text(...)` at `fetch.py:345` with `atomic_write_inventory(inventory_path, inv)`.
- `enrich.py`: replace the direct write at `enrich.py:84`.
- `articles.py`: replace the direct write at `articles.py:194`.

After this, every inventory write in the codebase goes through one function. Future hardening (fsync, retry on EBUSY, etc.) lands in one place.

### 11.2 User-Agent bump

`images.py` and `articles.py` each currently define:

```python
DEFAULT_USER_AGENT = "bsky-saves/0.2 (+https://github.com/tenorune/bsky-saves)"
# or 0.1 in articles.py
```

Both modules switch to:

```python
from . import __version__
DEFAULT_USER_AGENT = f"bsky-saves/{__version__} (+https://github.com/tenorune/bsky-saves)"
```

`__version__` is sourced from package metadata via `importlib.metadata` (already wired in `__init__.py`), so this never goes stale.

## 12. Coordinated spec edit for the GUI team

The MVP spec §5.3 currently includes:

> **URL allowlist**: any `http://` or `https://` URL. Articles are user-saved Bluesky-linked URLs; the URL space is the open web by definition. The Origin allowlist (§4.4), not a URL allowlist, is the protective layer here.

Proposed addition (to be relayed by the project owner; not in scope for this repo to merge):

> **SSRF guard**: independent of the open-web URL allowlist, the daemon rejects URLs whose hostname is an IP literal in a private (RFC 1918), loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, `fe80::/10`), CGNAT (`100.64.0.0/10`), multicast, or otherwise reserved range. Hostnames that resolve to any address in those ranges are rejected the same way. This includes the cloud-metadata IP `169.254.169.254` (AWS / GCP / Azure / DigitalOcean), `localhost` and `localhost.*` aliases, and IPv4-mapped IPv6 forms. Rejected URLs return `400 {"error":"url not allowed"}`. The guard exists to prevent a compromised allowlisted origin or an attacker-supplied save record from probing the user's internal network or exfiltrating cloud credentials.

The same paragraph applies implicitly to the `pds` field on `/fetch` and `/hydrate-threads` (§5.4 and §5.6); §5.6's note on credential-as-gate is unchanged.

## 13. Test plan

### 13.1 Coverage strategy

Three layers:

1. **Unit tests for new pure helpers** (`tests/test_net.py`, `tests/test_io.py`). High-coverage, fast, no I/O.
2. **HTTP-handler tests in `tests/test_serve.py`** for the security gate, body cap, headers, and per-endpoint SSRF rejections. Use `respx` to mock outbound calls; use the existing `serve_in_background` helper to exercise the gate against real socket I/O.
3. **CLI-path SSRF tests in `tests/test_articles.py` and `tests/test_images.py`**: assert that a save with a bad URL is silently skipped and the rest of the inventory still hydrates.

### 13.2 New tests

`tests/test_net.py` (new, ~15 tests):

- Public hostname (e.g. `example.com`) passes.
- `127.0.0.1`, `localhost`, `localhost.` (trailing dot) reject.
- `10.0.0.1`, `192.168.0.1`, `172.16.0.1` reject (RFC 1918).
- `169.254.169.254` rejects (link-local, AWS metadata).
- `100.64.0.1` rejects (CGNAT).
- `[::1]`, `[fe80::1]` reject.
- `[::ffff:127.0.0.1]` rejects (IPv4-mapped IPv6 loopback).
- `0177.0.0.1` (octal), `0x7f.0.0.1` (hex), `2130706433` (decimal) all reject — Python's `ipaddress` does not accept these; need explicit normalisation OR `socket.getaddrinfo` which does. Confirm behavior; document in test.
- `metadata.google.internal` rejects (DNS aliases for metadata; needs network-mocked `getaddrinfo`).
- `http://example.com` with `allow_http=False` rejects.
- `ftp://example.com` rejects (unsupported scheme).
- `safe_http_get` happy path returns httpx.Response.
- `safe_http_get` rejects when initial URL is unsafe.
- `safe_http_get` rejects when redirect target is unsafe (303/302 to private IP).
- `safe_http_get` rejects when `hop_check` raises on a redirect target.
- `safe_http_get` raises `TooManyRedirectsError` (or similar) after exceeding `max_redirects`.

`tests/test_io.py` (new, ~2 tests):

- `atomic_write_inventory` writes correct content.
- After successful write, the `.tmp` sidecar does not exist on disk.

`tests/test_serve.py` (~20 new tests):

- Host accepted: `127.0.0.1:<port>` → 200.
- Host accepted: `localhost:<port>` → 200.
- Host rejected (wrong domain) → 421.
- Host rejected (wrong port) → 421.
- Host rejected (IPv6 brackets) → 421.
- Host rejected (trailing dot) → 421.
- Host missing → 421.
- Origin disallowed → 403 (on `/ping`).
- Origin disallowed → 403 (on `/fetch-image`).
- Origin disallowed OPTIONS preflight → 403, not 204.
- Origin missing on `/ping` (curl-style) → 200.
- Default allowlist accepts `http://127.0.0.1:<port>` without `--allow-origin`.
- `--allow-origin https://example.com` keeps defaults and admits example.com.
- POST body > 10 MB → 413.
- POST body at 10 MB → success (boundary).
- Response includes `X-Content-Type-Options: nosniff`.
- Response includes `Cache-Control: no-store`.
- `/extract-article` rejects `http://127.0.0.1/x` → 400 url not allowed.
- `/extract-article` rejects `http://169.254.169.254/x` → 400 url not allowed.
- `/fetch` rejects `pds: "http://127.0.0.1"` → 400.
- `/fetch` rejects `pds: "https://169.254.169.254"` → 400.
- `/hydrate-threads` rejects `pds: "http://10.0.0.1"` → 400.
- `/fetch-image` rejects a redirect to a non-bsky host (mocked via respx).
- Verbose mode escapes control characters in path log.

`tests/test_articles.py` (~2 new tests):

- `_extract_article("http://169.254.169.254/x")` returns `(None, "fetch_error:UnsafeURLError:...")`.
- `hydrate_articles` skips an entry with an unsafe embed URL and still processes a good one in the same run.

`tests/test_images.py` (~2 new tests):

- `download_to("http://127.0.0.1/x", dest)` raises `UnsafeURLError`.
- `hydrate_images` records `failed += 1` for an unsafe URL and still downloads a safe one.

### 13.3 Existing test churn

- `test_serve.py`: rename and flip assertions on three tests (per §4.5).
- `test_images.py`: existing inventory-write tests pass through the new atomic helper unchanged (they assert end-state inventory contents, not write mechanism).
- `test_threads.py`: imports might change (`_atomic_write_inventory` moves to `_io`); update test-side imports if any.

### 13.4 Final test count

182 (current) + ~41 new − 0 dropped ≈ **223 tests** post-v0.4.4. All green is the bar.

## 14. Acceptance criteria

The release ships when all of the following hold:

1. `python -m pytest tests/ -v` reports ≥223 tests, all passing.
2. `python -m build` produces a wheel that imports cleanly in a fresh venv (`python -c "import bsky_saves; print(bsky_saves.__version__)"` prints `0.4.4`).
3. `bsky-saves --help` and `bsky-saves serve --help` render correctly with updated `--allow-origin` description.
4. Manual smoke against a running daemon:
   - `curl -sS http://127.0.0.1:47826/ping` returns the expected JSON.
   - `curl -sS http://127.0.0.1:47826/ping -H "Origin: https://evil.com"` returns `403 {"error":"Origin not allowed"}`.
   - `curl -sS http://127.0.0.1:47826/ping -H "Host: evil.com"` returns `421 {"error":"misdirected request"}`.
   - `curl -sS -X POST http://127.0.0.1:47826/extract-article -H "Content-Type: application/json" -d '{"url":"http://169.254.169.254/"}'` returns `400 {"error":"url not allowed"}`.
5. `bsky-saves serve --allow-origin https://custom.example` keeps `https://saves.lightseed.net`, `http://127.0.0.1:47826`, and `http://localhost:47826` in the effective allowlist alongside `custom.example`.
6. Every response (success and error) includes `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`.
7. The handoff doc's "Inconsistent atomic-write coverage" item is resolved: every inventory writer goes through `_io.atomic_write_inventory`.
8. `images.py` and `articles.py` `User-Agent` strings interpolate `__version__` (verified by inspection or a unit test).

## 15. Release process

Standard for this project:

1. Land all changes on `claude/bsky-saves-next-phase-o7mOi` via small commits (one per task in the implementation plan).
2. Open PR from `claude/bsky-saves-next-phase-o7mOi` to `main`. Self-review; user reviews.
3. Merge to `main` via the GitHub UI (the sandbox proxy 403s direct pushes to main).
4. User creates the `v0.4.4` tag on the merge commit via the GitHub UI.
5. `release.yml` publishes `bsky-saves==0.4.4` to PyPI via trusted publishing.
6. After PyPI publishes, project owner relays the §12 spec-edit proposal to the GUI team.
7. v0.5.0 work (GUI vendoring + `--gui`) starts on a new dev branch.

## 16. Decisions log

| Date | Decision | Rationale |
|---|---|---|
| 2026-05-12 | Split security hardening from GUI vendoring into separate releases | Two independently-reviewable chunks; security work has heavy existing test coverage and ships cleanly on its own. |
| 2026-05-12 | Fold the v0.4.3 handoff drive-bys (atomic-write coverage, UA bump) into v0.4.4 | They touch the same files; cheap to include; future-proofs the UA via `__version__` interpolation. |
| 2026-05-12 | Run a full VibeSec-style security audit; include all findings rated ≥ defensive-low | Project owner explicitly opted in. Audit produced 9 actionable items beyond the GUI team's spec (F1, F2, F6, F7, F8, F9, F10, F11, F12). |
| 2026-05-12 | SSRF guard rejects private/loopback/link-local/CGNAT/multicast/reserved on `/extract-article`, narrowing the MVP spec's "open web" position | The spec's rationale (Origin allowlist is the protective layer) doesn't hold when an allowlisted origin can supply attacker-influenced URLs. Private IPs are not "the open web" by definition. GUI team to update §5.3 in lockstep. |
| 2026-05-12 | Uniform 10 MB request-body cap rather than per-route differentiation | Simpler; covers worst-case `/fetch` batches; bounds DoS without affecting any realistic use. |
| 2026-05-12 | `nosniff` + `no-store` applied uniformly to every response (success and error) | One policy across the daemon. Cost is two header lines per response. |
| 2026-05-12 | `/fetch-image` switches to manual redirect-walking with per-hop allowlist + SSRF check; `max_redirects=3` | Existing single-URL allowlist check missed redirect targets. Three hops is generous for any plausible CDN failover. |
| 2026-05-12 | Pin a minimum trafilatura version after verifying XXE-safe defaults | Eliminates the unverified-dependency-default risk. |
| 2026-05-12 | DNS-rebinding mitigation by pinned-IP connect deferred to v0.5.x | The pre-resolve + reject approach has a millisecond TOCTOU window; closing it requires a custom httpx transport. Out of scope for v0.4.4. |
| 2026-05-12 | Skip findings F3, F4, F5, F13 | Verified not vulnerable. Documented in audit summary for traceability. |

## 17. Open questions

None outstanding for v0.4.4 scope. The v0.5.0 brainstorming session will pick up build-hook approach, `_gui/` population strategy, `--gui` runtime behavior on empty `_gui/`, and the marker-file format.
