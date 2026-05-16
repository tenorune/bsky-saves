# bsky-saves v0.6.2 — Session-token authentication for the local helper

> **Status:** approved 2026-05-16. Implementation pending.
> **Branch:** `claude/installer-prep` in `tenorune/bsky-saves` (PR #9).
> **Releases as:** PyPI `bsky-saves==0.6.2`. Consumers: the `bsky-saves-gui` static PWA (bundled or hosted at `https://saves.lightseed.net`).
> **External contract:** `bsky-saves-gui/docs/bsky-saves-gui-dist-workstream.md` §3 R3 + §4 items 10–12 (origin allowlist, session token, security headers) and §4 item 11's pairing UX prose. That document is canonical for the GUI-side implementation; this document is canonical for the helper side. The two MUST agree on the meta-tag sentinel (§7), the placeholder-substitution rule (§7), the exempt-endpoint list (§5), and the protocol bump value (§9) — that agreement is the anti-drift contract for v0.6.2.

---

## 1. Context

The local helper (`bsky-saves serve`) listens on `127.0.0.1` and exposes a JSON API to `bsky-saves-gui`. As of v0.6.x the access control surface is:

- **Bind**: `127.0.0.1` only, never `0.0.0.0`.
- **Host header**: must equal `127.0.0.1:<port>` or `localhost:<port>`; otherwise `421 Misdirected Request` (`serve.py::_check_host`).
- **Origin header**: must match the allowlist (`http://127.0.0.1:<port>`, `http://localhost:<port>`, `https://saves.lightseed.net`, plus any `--allow-origin` additions); otherwise `403` (`serve.py::_check_origin`).

These three layers together defeat the DNS-rebinding attack of `bsky-saves-gui:docs/bsky-saves-gui-dist-workstream.md §3 R3`: an attacker rebinding `evil.com` to `127.0.0.1` still carries `Origin: https://evil.com`, which fails the allowlist. The browser sets `Origin` from the page's origin, not from the post-rebind connection target, and there is no `fetch()` API that lets a page spoof it.

v0.6.2 adds a fourth layer — **a per-installation session token** — as defense-in-depth on top of the three above. The token is the GUI side's same-machine pairing primitive: any process that can read the token file can call the helper API; any caller without the token gets `401`. The token primarily defends against:

- An endpoint regression that bypasses `_security_gate` (the shared dispatcher gate today; future endpoints might be added that skip it).
- A future hypothetical browser bug or misconfiguration that allows `Origin` spoofing.
- A locally-running malicious app that knows the helper port but cannot read the user's config dir (browsers' fetch limitations don't apply here, but file-system permissions do).

The token is *not* the primary DNS-rebinding defense — Origin/Host already block that. The token's value is in tightening the trust boundary so that "any HTTP client on the same machine" no longer implies "trusted helper consumer." Trust is now gated on read-access to a 0600 file in the user's config dir.

### Why persistent, not random-per-start

The original proposal in `bsky-saves-gui:docs/bsky-saves-gui-dist-workstream.md §4 item 11` was "random session token on daemon startup." That UX is broken for the hosted-PWA case (`https://saves.lightseed.net` → `http://127.0.0.1:47826`): every `bsky-saves serve` invocation regenerates the token, and the hosted SPA has no way to read the meta-tag (it's not loading from the helper), so the user must re-pair manually every time. Terminals get closed; we don't ship a system tray; post-reboot the terminal-display affordance is gone.

Persistent storage solves this. The token is generated once and persists across:
- daemon restarts,
- bsky-saves upgrades (within the v0.7.x line — re-pair on a future protocol bump is acceptable),
- machine reboots.

Re-pairing fires only on three cases: never-paired-on-this-machine, explicit user-requested `bsky-saves token --rotate`, or token file corruption / config-dir clobber. The trust model is the same as `~/.netrc`, `~/.npmrc`, `~/.config/gh/hosts.yml`, and other credential-bearing dotfiles: anyone who can read the file can act as the user.

## 2. Scope

`bsky-saves` remains an ingestion package plus local helper daemon. v0.6.2 adds:

- a token file on disk (read by `serve`, written by `serve` lazily and by the new `token` subcommand explicitly),
- `Authorization: Bearer <token>` enforcement on every credentialed endpoint,
- meta-tag injection into the served `index.html` so the bundled GUI auto-pairs,
- a `bsky-saves token` CLI subcommand (read + `--rotate`).

### In scope

- **Token storage**: lazy-create, atomic-write, 0600 perms, platform-conventional config dir — §4.
- **`Authorization: Bearer` enforcement**: required on all non-exempt endpoints; `401 Unauthorized` on missing/invalid token — §5.
- **`/ping` exemption**: `/ping` remains unauth indefinitely — §5.
- **`bsky-saves token` subcommand**: prints the current token (lazy-generates on first call); `--rotate` regenerates — §6.
- **Meta-tag injection**: `_gui_serve.py::serve_static_or_spa` substitutes `__BSKY_SAVES_TOKEN__` → current token in served `index.html` (and SPA fallback) — §7.
- **Protocol bump**: `_PROTOCOL_VERSION` in `serve.py` bumps from `"1"` to `"2"` — §9.
- **CSP and security headers**: unchanged from v0.6.x (`'wasm-unsafe-eval'` retained — Pyodide path) — §10.
- **`README.md`**: document the pairing model, the `token` subcommand, and the upgrade path from v0.6.x.

### Out of scope (explicitly deferred)

- **Hosted-PWA pairing UX.** The token is the primitive; the hosted SPA's pairing modal, sessionStorage handling, and protocol-mismatch banner are the GUI side's responsibility per the workstream doc. The helper just makes the token reachable via `bsky-saves token`.
- **A `serve --no-auth` escape hatch.** No mode that disables token enforcement. If you need to bypass for ops/debug, read the token file directly or rotate.
- **OAuth / DPoP / cookie sessions.** The helper continues to be unaware of upstream BlueSky session shape; the bearer token is purely the GUI↔helper local trust primitive.
- **Multi-tenancy.** One user → one token → one helper. No support for multiple paired clients with separate tokens; that is `bsky-saves-watch` territory if it ever ships.
- **Network-exposed deployment.** The helper remains 127.0.0.1-bound; the token is *not* the right primitive to expose this over a LAN, and we don't ship that mode.

## 3. Architecture and module layout

### Files modified

| File | Change |
|---|---|
| `src/bsky_saves/_io.py` | Add `config_dir()` (platform-conventional path resolver) and `read_or_create_token()` (lazy-create, atomic-write, 0600 perms, returns the current token string). Both are pure-stdlib. |
| `src/bsky_saves/serve.py` | `_security_gate` gains a token check (after Host + Origin). Read the token once at request time via `read_or_create_token()` (file read is rounding error vs the network I/O each handler does anyway, and re-reading is what makes `--rotate` actually invalidate a running daemon). `/ping` exempt. New `EXEMPT_ROUTES` set used by `_check_token`. `_PROTOCOL_VERSION = "2"`. New `_send_json_error(401, ...)` branch. |
| `src/bsky_saves/_gui_serve.py` | `_send_file` gains a `token` keyword and, when serving `index.html` (root match) or SPA fallback, replaces `b"__BSKY_SAVES_TOKEN__"` with the current token bytes before sending; recomputes `Content-Length`. The substitution is idempotent — if the placeholder is absent, the body is unchanged. The token is read lazily per request via `read_or_create_token()`. |
| `src/bsky_saves/cli.py` | Add `token` subcommand with `--rotate` flag. `token` (no flag) prints the current token (lazy-generates). `token --rotate` writes a fresh random token, invalidating all paired sessions, and prints the new value. |
| `tests/test_serve.py` | Update existing handler tests to set `Authorization: Bearer <test-token>` via a new `paired_helper` fixture. Add: token-required tests (401 on missing/wrong token), `/ping` exemption test, meta-tag substitution test, lazy-generation test, rotate test, atomic-write test. |
| `tests/test_cli.py` (or new `tests/test_token_cli.py`) | Tests for the `token` subcommand: prints existing, lazy-generates on first call, `--rotate` produces a different value and persists. |
| `README.md` | New "Pairing" subsection under `## bsky-saves serve`. Mention `bsky-saves token` + `--rotate`. Update the `## Upgrade` section to note re-pairing is needed on upgrade from v0.6.x. |
| `pyproject.toml` | Bump `version = "0.6.2"`. |
| `docs/protocol-versioning.md` | Add a "Changelog" subsection: v0.6.1 → `"1"`, v0.6.2 → `"2"` (auth requirement added to credentialed endpoints). |

### Files created

None. All work lands in existing modules.

## 4. Token storage

### Location

Platform-conventional, computed by `_io.config_dir()`:

| Platform | Path | Source |
|---|---|---|
| Linux / *BSD | `$XDG_CONFIG_HOME/bsky-saves/` or `~/.config/bsky-saves/` | XDG Base Directory Specification |
| macOS | `~/Library/Application Support/bsky-saves/` | Apple File System Programming Guide |
| Windows | `%APPDATA%\bsky-saves\` (typ. `C:\Users\<user>\AppData\Roaming\bsky-saves\`) | Microsoft known-folders convention |

The token file is `<config_dir>/token`. No subdirectories; no other files (yet — v0.8+ may grow this directory).

### Format

- 32 bytes from `os.urandom(32)`,
- encoded as `base64.urlsafe_b64encode(...).rstrip(b"=").decode("ascii")` — yields ~43 chars, no padding, URL-safe (avoids `+` `/` `=` that confuse copy-paste).
- File content is the token followed by a single trailing `\n`. Trailing whitespace is stripped on read.
- One token per file. Multi-line files are an error condition: read returns the first non-empty line, but `--rotate` always rewrites the whole file.

### Permissions

- File: `0o600` (owner read/write only).
- Directory: `0o700` if `_io.config_dir()` creates it. If it exists with looser perms (user customised their config dir), we do not tighten — that is the user's call and may break other tools.

### Writes are atomic

`read_or_create_token()` writes via the same temp-file + `os.replace` pattern as `_io.atomic_write_inventory`. The temp file is created in the same directory (so `os.replace` is on the same filesystem) with `0o600` perms set *before* the first write (`os.open(..., os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)`). Concurrency: if two `bsky-saves serve` processes start simultaneously and both lazy-create, the loser's `os.replace` overwrites the winner's — the second token sticks. Acceptable; the user re-pairs at most once. We do not file-lock.

### Reads

`read_or_create_token()` semantics:

1. If `<config_dir>/token` exists and is non-empty, return its stripped first line.
2. Else, generate a fresh token, atomic-write it, return the new value.

The function is called on every API request from `_security_gate`. Performance: a single stat + read of a ~44-byte file is ~tens of microseconds; negligible compared to the network round-trip each credentialed handler does. Re-reading is what makes `--rotate` from a separate process actually invalidate a running daemon's accepted token.

## 5. HTTP enforcement

### Where the check fires

`_security_gate` runs in order:

1. `_check_host` → 421 on miss
2. `_check_origin` → 403 on miss
3. `_check_token` (NEW) → 401 on miss

A request that fails any of the three is rejected before route dispatch.

### Exempt endpoints

Token enforcement applies to every route in `ROUTES` *except* `/ping`. Static-file serving (the `--gui` path through `_gui_serve.serve_static_or_spa`) is also exempt — `index.html` is the carrier of the token; the GUI cannot read the meta tag without first loading the page. Static assets (`/assets/*`, etc.) are unauth too; they contain no user data.

Exempt set (codified as `EXEMPT_ROUTES`):

```python
EXEMPT_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/ping"),
    # OPTIONS is handled separately — preflight responses are always
    # unauth so the browser can complete CORS negotiation.
})
```

`OPTIONS` requests bypass `_check_token` regardless of path — preflight cannot carry custom headers, and rejecting preflight breaks CORS for legitimate callers.

### Header format

- Required header: `Authorization: Bearer <token>`.
- Case-sensitive `Bearer` per RFC 6750.
- Tokens are compared with `hmac.compare_digest` to avoid timing-based oracle attacks.

### 401 response

`_send_json_error(401, "authentication required")` with the same JSON envelope used elsewhere:

```json
{"error": "authentication required"}
```

**`WWW-Authenticate: Bearer realm="bsky-saves"`** is emitted on every pairing-401 (i.e., 401s produced by `_check_token`). The wrong-token case includes `error="invalid_token"` per RFC 6750 §3.1:

- Missing / non-`Bearer` `Authorization` header → `WWW-Authenticate: Bearer realm="bsky-saves"`
- `Bearer` present but token value mismatches → `WWW-Authenticate: Bearer realm="bsky-saves", error="invalid_token"`

(Note: only the `Bearer` scheme triggers this header; `Basic` would prompt the browser's native auth dialog, but `Bearer` does not. The earlier spec revision that suppressed the header was based on a misreading of that browser behaviour.)

**Upstream-cause 401s** (e.g., `_handle_fetch`'s `createSession failed` passthrough, or `_handle_hydrate_threads`'s same path) do **not** carry `WWW-Authenticate`. This presence-or-absence is the signal the GUI uses to distinguish pairing failures (trigger pairing recovery — re-fetch `index.html` for bundled, surface pairing modal for hosted) from upstream-PDS auth failures (existing GUI handling).

The 401 body remains intentionally generic — `{"error": "authentication required"}` — for both pairing-401 cases. The header alone carries the distinction-from-upstream signal; the wrong-token-vs-missing-header sub-distinction is informational only (the GUI's recovery flow is the same either way).

**`Access-Control-Expose-Headers: WWW-Authenticate`** is added to `_cors_headers` so the cross-origin GUI's `fetch()` JS can actually read the header. Without this, the browser filters non-simple response headers out of what JS can see.

## 6. CLI surface

### `bsky-saves token`

Print the current token to stdout, one line, no trailing whitespace beyond a single newline. Lazy-generates if no token exists yet (per §4).

```
$ bsky-saves token
gN8K9eP-9d2WqXmM3oxXfLwQjA8r-J0vV7VqLcD6ZcU
$
```

Exit code: 0 on success; 1 on permission errors (e.g., config dir un-writable).

### `bsky-saves token --rotate`

Generate a fresh token, atomic-write it, print the new value. All previously-issued tokens are invalidated; any paired GUI sees 401 on its next request.

```
$ bsky-saves token --rotate
2QlX_pVcRoXwH7tF-N9aBcD0e5z8MhLmK1jPwQrSt-Y
$
```

Idempotent in the sense that calling `--rotate` twice produces two distinct tokens (overwriting); not idempotent in the sense that each call invalidates the previous one.

### `--rotate` and a running daemon

Because `_security_gate` reads the token on every request, `--rotate` from a separate shell immediately invalidates the running daemon's accepted token. No daemon restart needed. The next request from a stale-token-bearing client gets 401; the user re-pairs.

## 7. Token injection into served `index.html`

### Placeholder

`bsky-saves-gui` ships `index.html` containing a literal placeholder line in `<head>`:

```html
<meta name="bsky-saves-token" content="__BSKY_SAVES_TOKEN__">
```

The sentinel string is `__BSKY_SAVES_TOKEN__`. Coordinated with the GUI side (confirmed in this v0.6.2 sequencing thread).

### Substitution

`_gui_serve.py::_send_file`, when serving `index.html` (root match → `rel_path == "index.html"`) or SPA fallback (`is_spa_fallback=True`), performs a single byte-string replacement:

```python
if rel_path == "index.html" or is_spa_fallback:
    body = body.replace(b"__BSKY_SAVES_TOKEN__", current_token.encode("ascii"))
```

The substitution is performed *after* `body = path.read_bytes()` and *before* `Content-Length` is computed. The substitution is idempotent: if the placeholder is absent (e.g., an older GUI bundle, a hosted-PWA case where the helper never serves the HTML), `body` is unchanged.

The placeholder is *not* substituted in any other file. CSS, JS, images, fonts, the web manifest — all served verbatim. The token never appears in any URL or query string.

### Why not pre-substitute at vendor time

The token is per-user-machine, not per-build. Pre-substitution would either bake a constant token into the wheel (broken) or require runtime regeneration of the entire `_gui/` tree (heavy). Per-request substitution touches only `index.html` (small, hot path that's already in OS page cache), and has the property that `--rotate` is reflected on the next page load with zero coordination.

### Hosted-PWA case

The hosted PWA at `https://saves.lightseed.net` loads its own `index.html` from its CDN; the helper is not in that path. The GUI bundle on the CDN still contains the literal placeholder. The GUI's startup code detects `meta.content === '__BSKY_SAVES_TOKEN__'` (or any non-base64url-shaped value) as "no token, prompt to pair" and surfaces the pairing modal. The user runs `bsky-saves token` in a terminal, copies the value, pastes it into the modal; the GUI stashes it in `sessionStorage` and uses it for all subsequent `Authorization` headers. (Pairing UX lives in the GUI repo; this spec only specifies what the helper does — namely, accept the token if presented and reject otherwise.)

## 8. Tests

### Existing tests update

Every `test_serve.py` test that today calls a credentialed endpoint (`/fetch`, `/fetch-image`, `/extract-article`, `/enrich`, `/hydrate-threads`) gains an `Authorization: Bearer <token>` header. Provide a `paired_helper` pytest fixture that:

1. monkeypatches `_io.config_dir` to return a `tmp_path` subdir,
2. writes a known test token to `<tmp>/bsky-saves/token`,
3. yields the token string.

Tests opt in by adding `paired_helper` to the signature and threading the returned token into the `Authorization` header.

### New tests

| Test | Asserts |
|---|---|
| `test_credentialed_endpoint_requires_token` | `POST /fetch-image` with valid body but no `Authorization` returns 401. |
| `test_credentialed_endpoint_rejects_wrong_token` | Same, but with `Authorization: Bearer wrong` returns 401. |
| `test_ping_does_not_require_token` | `GET /ping` with no `Authorization` returns 200. |
| `test_options_preflight_does_not_require_token` | `OPTIONS /fetch-image` with no `Authorization` returns 204. |
| `test_static_assets_do_not_require_token` | (`--gui` mode) `GET /assets/<file>` with no `Authorization` returns 200. |
| `test_index_html_substitutes_token_placeholder` | (`--gui` mode) `GET /` body contains the current token, not the literal placeholder. |
| `test_index_html_substitutes_in_spa_fallback` | (`--gui` mode) `GET /some-spa-route` body contains the current token. |
| `test_non_index_files_do_not_substitute` | (`--gui` mode) `GET /assets/<file>` body unchanged byte-for-byte. |
| `test_read_or_create_token_lazy_generates` | First call writes a fresh ~43-char file; second call returns the same value. |
| `test_read_or_create_token_atomic_write` | After the function returns, the file is at the expected path with `0o600` perms. |
| `test_token_cli_prints_existing` | `bsky-saves token` after a prior generation prints the same value. |
| `test_token_cli_rotate_changes_value` | `bsky-saves token --rotate` produces a value distinct from the prior. |
| `test_token_cli_lazy_generates_on_first_call` | `bsky-saves token` with no prior file generates and prints (gotcha-eliminator). |
| `test_rotate_invalidates_running_daemon_on_next_request` | Boot serve with token A; rotate to token B; request with token A → 401; request with token B → 200. (Requires `_security_gate` to re-read on each request, not cache.) |

### Backward compatibility regression

`test_v06_ping_shape_still_valid` — `GET /ping` still returns the v0.6.1 shape but with `protocol: "2"`. Catches accidental shape changes.

## 9. Protocol bump

`_PROTOCOL_VERSION` in `serve.py` bumps from `"1"` to `"2"`. Per `docs/protocol-versioning.md`'s rules: "authentication requirement changes" is a non-additive bump trigger.

Old GUI versions (those built against v0.6.x helpers) send no `Authorization` header. Against a v0.6.2 helper they get clean 401s on every credentialed endpoint. The GUI side's startup logic reads `protocol` from `/ping`'s response; on `protocol >= "2"` the GUI knows to require pairing before issuing requests. Cross-repo coordination: the GUI side ships its `protocol`-aware code in the GUI release that lands alongside helper v0.6.2.

`docs/protocol-versioning.md` gains a Changelog subsection:

```
## Changelog

- "1" — bsky-saves v0.6.1. Initial value when `protocol` was added to /ping.
- "2" — bsky-saves v0.6.2. `Authorization: Bearer <token>` now required on all
  credentialed endpoints (/fetch, /fetch-image, /extract-article, /enrich,
  /hydrate-threads). /ping remains unauth.
```

## 10. Security headers and CSP

No changes from v0.6.x. `_gui_serve.py`'s CSP retains `script-src 'self' 'wasm-unsafe-eval'` (Pyodide path). Other headers (`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin`, `Cache-Control: no-store` on `index.html`) unchanged.

The token does not appear in any response header. It is only ever in:
- the served `index.html` body (substituted per §7),
- the request's `Authorization` header,
- the on-disk token file.

Server-side logs do not echo the token. The `verbose` log line prints `method` and `path` only (`serve.py::_log_request`); request headers are not logged. Confirm this stays true post-implementation.

## 11. Backward compatibility / migration

### From v0.6.x

- First `bsky-saves serve` after upgrading: lazy-generates the token file.
- First request from a v0.6.x-era GUI bundle (no `Authorization` header): 401. The GUI side ships a paired v0.6.2 bundle in the same coordinated release.
- Coordinated-release gate (per the v0.5.0 spec's vendoring model): `bsky-saves` v0.6.2's wheel bundles the v0.6.2 GUI tag. A user who `pipx upgrade`s bsky-saves to 0.6.2 gets the matching GUI automatically.
- README's `## Upgrade` section gains a one-line note: "v0.6.x → v0.6.2 introduces pairing; the GUI will surface a pairing prompt on first connect."

### Downgrades

Not supported. Downgrading from v0.6.2 to v0.6.x leaves the token file in place (harmless — older versions ignore it). Re-upgrading reuses the same token.

## 12. Out-of-band integrations

### `bsky-saves-watch` (Option C, future)

If/when the watch daemon ships, it will use the same token file (it is the same trust boundary — local-user access to the inventory and helper). The token file therefore needs no rename for v0.8+; the design is single-token-per-installation by intent.

### Programmatic helper consumers

External scripts that wrap the helper need to read the token file directly:

```bash
TOKEN=$(bsky-saves token)
curl -H "Authorization: Bearer $TOKEN" \
     -H "Origin: http://127.0.0.1:47826" \
     http://127.0.0.1:47826/ping
```

(Note: `/ping` doesn't need the header; the example shows the general pattern for credentialed endpoints.)

This is supported and documented in the README.

## 13. Sequencing

1. **GUI side (in parallel, can land now)**: Add `<meta name="bsky-saves-token" content="__BSKY_SAVES_TOKEN__">` to `app/index.html` and ship in a small GUI PR; the wheel build will pick it up on the next coordinated GUI tag.
2. **Helper side (this spec)**: `_io.config_dir` + `read_or_create_token`; `_security_gate` token check; `_gui_serve` placeholder substitution; `token` CLI; tests; README; doc updates.
3. **Doc side (GUI repo, standalone, can land now)**: §4 items 10–12 corrections per the v0.6.2 sequencing thread (CSP `'wasm-unsafe-eval'` retained, `--allow-origin` flag name, `/ping` exemption captured, persistent-secret design).
4. **Coordinated release**: tag `bsky-saves-gui v0.6.2` (carries the placeholder + paired-mode startup logic), bump `gui_version` + `gui-dist.sha256` in `bsky-saves` (via the `gui-version-bump` workflow from PR #9), tag `bsky-saves v0.6.2` (PyPI release).

The gui-version-bump workflow is now load-bearing: step 4 is the first release that exercises the end-to-end auto-bump loop.

## 14. Open questions

None as of approval (2026-05-16). The four push-back points from the v0.6.2 sequencing thread are all resolved.
