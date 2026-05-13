# bsky-saves v0.5.0 — GUI vendoring and `--gui` serve flag

> **Status:** approved 2026-05-13. Implementation pending.
> **Branch:** `claude/bsky-saves-next-phase-o7mOi` in `tenorune/bsky-saves`.
> **Releases as:** PyPI `bsky-saves==0.5.0`. Consumers: the `bsky-saves-gui` static PWA.
> **External contract:** `bsky-saves-gui/docs/bsky-saves-mvp-spec.md`. That document is canonical for the build pipeline, daemon-side static-serving rules, security headers, and acceptance criteria. This document is canonical for the bsky-saves-side implementation.
> **Builds on:** `docs/superpowers/specs/2026-05-12-bsky-saves-v0.4.4-security-and-write-hygiene.md` (the security baseline this release depends on).

---

## 1. Context

`bsky-saves-gui` is a Svelte/Vite PWA. It compiles to a small static `dist/` tree that runs identically when hosted on `saves.lightseed.net` and when served from a local `bsky-saves serve --gui` daemon. The GUI team's v0.5.x releases attach `dist.tar.gz` + `dist.tar.gz.sha256` to each GitHub release.

v0.5.0 of bsky-saves does the minimum work needed to consume that artifact:

1. A build-time hook vendors the GUI tarball into `src/bsky_saves/_gui/`, with SHA-256 integrity verification against a checked-in pin.
2. `bsky-saves serve` gains a `--gui` flag that mounts the bundled GUI at `/` on the same loopback port that already serves the JSON API.
3. The security gate built in v0.4.4 (Host, Origin, body-cap, response-headers) is unchanged. Static-file responses gain a CSP + `X-Frame-Options` + COOP + `Referrer-Policy` set tuned to the bundle.

After v0.5.0 ships, a `pipx install bsky-saves` user can open the GUI by running `bsky-saves serve --gui` without ever provisioning a browser tab on `saves.lightseed.net`.

## 2. Scope

bsky-saves remains an *ingestion package*. v0.5.0 is purely additive at the public-surface level: no new endpoints, no new CLI subcommands beyond the existing `serve`, no inventory-schema changes.

### In scope

- **Build pipeline:** pinned `GUI_VERSION` + `gui-dist.sha256`, a `scripts/fetch_gui.py` script that fetches and verifies the GUI tarball, and a Hatch custom build hook (`hatch_build.py`) that invokes the script as part of any wheel/sdist build.
- **`--gui` flag on `serve`:** opt-in static-file mount of `_gui/` at `/`, with SPA fallback to `index.html`, `Cache-Control` rules per asset class, and per-response security headers (CSP / X-Frame-Options / Referrer-Policy / COOP). API routes take precedence; static-file resolution rejects path traversal.
- **Runtime guard:** `--gui` startup verifies `_gui/` exists and is non-empty; refuses to start otherwise with a clear actionable error.
- **CI smoke test:** new pre-release smoke job that builds the wheel, installs into a fresh venv, runs the daemon, and exercises four endpoints + one negative-pin variant.
- **Drive-by cleanups** (small, related, free to fold in):
  - Delete the now-unused `import httpx` from `src/bsky_saves/images.py`.
  - Bump `threads.py`'s stale User-Agent string to derive from `__version__` (same pattern as v0.4.4 Task 9).

### Out of scope (explicitly deferred)

- **Reproducible-builds work** (pinning `SOURCE_DATE_EPOCH`, deterministic tar packing). Future hardening once SHA-256 pinning is in place.
- **Sigstore / cosign signing** of the GUI tarball. Stretch goal noted in GUI MVP spec §7.
- **A `--no-gui` flag** for explicit disable. `--gui` opt-in already covers the use case; `--no-gui` would be a no-op.
- **`POST /run` endpoint** (the v1-spec's Phase 2 one-shot combiner). Still planned, still deferred.
- **Configurable URL allowlist for `/fetch-image`.** Hardcoded to bsky.app.
- **Streaming responses** for static files. Files are small (~270 KB total bundle, single largest asset is the JS chunk at low hundreds of KB); single-buffer responses are fine.
- **System tray / autostart / OS installers.** CLI only.

## 3. Architecture and module layout

### Files created

| File | Responsibility |
|---|---|
| `scripts/fetch_gui.py` | Standalone Python script. Reads `[tool.bsky-saves] gui_version` from `pyproject.toml`. Downloads `dist.tar.gz` from the corresponding GitHub release over HTTPS. Verifies SHA-256 against checked-in `gui-dist.sha256`. Extracts into `src/bsky_saves/_gui/`, stripping `CNAME` and any `dist/` prefix. Writes a 2-line `_gui/.gui-version` marker `{version}\n{sha256}\n` for idempotency. Re-runs are no-ops if marker matches pin. |
| `hatch_build.py` (project root) | Hatch custom build hook. Defines `class GuiBuildHook(BuildHookInterface)` whose `initialize` method imports and calls `scripts.fetch_gui.fetch_gui()`. Glue only; logic lives in the script. |
| `gui-dist.sha256` (project root) | Byte-for-byte copy of `dist.tar.gz.sha256` from the GitHub release. One line: `<hex>  dist.tar.gz`. Reviewed-on-change. |
| `src/bsky_saves/_gui_serve.py` | New module. Exposes `GuiNotInstalledError`, `resolve_gui_root() -> Path`, and the static-file resolver `serve_static_or_spa(handler, path) -> bool`. All static-file handling logic lives here, out of `serve.py`. |
| `tests/test_fetch_gui.py` | Unit tests for the script (~10 tests). Mock HTTP via `respx`; build in-memory tar.gz fixtures. |
| `tests/test_serve_gui.py` | Unit tests for the static-file + SPA-fallback dispatcher path (~12 tests). Uses a temp `_gui/` populated from an in-memory tarball fixture and monkeypatches `_gui_serve.resolve_gui_root`. |
| `tests/conftest.py` additions | A `gui_tarball_fixture` factory yielding both an in-memory tar.gz bytes blob and the corresponding SHA-256 hex string, for reuse across both new test files. |
| `.github/workflows/smoke.yml` | New workflow. On every push to `main` and every `v*` tag: build wheel, install in fresh venv, start daemon with `--gui`, curl four endpoints, kill daemon. Failure blocks the release-publish workflow. |

### Files modified

| File | Change |
|---|---|
| `pyproject.toml` | Add `[tool.bsky-saves] gui_version = "0.5.3"`. Add `[tool.hatch.build.hooks.custom] path = "hatch_build.py"`. Add `[tool.hatch.build] artifacts = ["src/bsky_saves/_gui/**"]` so hatchling includes the gitignored tree in the built wheel (`artifacts` is hatchling's escape hatch for files normally excluded by `.gitignore`). Bump `version = "0.5.0"`. |
| `.gitignore` | Add `src/bsky_saves/_gui/`. |
| `src/bsky_saves/cli.py` | `serve` subcommand gains `--gui` flag. Help text mentions "Serve the bundled GUI from / in addition to the JSON API." Wires through to `run_serve(..., gui=args.gui)`. |
| `src/bsky_saves/serve.py` | `make_handler` and `run_serve` accept a new `gui: bool = False` parameter. `run_serve` calls `resolve_gui_root()` at startup if `gui=True`; on failure prints the spec'd error and returns exit code 2. `Handler._dispatch` (or a new `_dispatch_or_static` wrapper called from the existing `do_*` entrypoints after `_security_gate`) routes static-file requests via `_gui_serve.serve_static_or_spa` AFTER the ROUTES lookup. API routes take precedence. HEAD is supported for static files via the existing `__getattr__` fallback. |
| `src/bsky_saves/images.py` | Drop the now-unused `import httpx`. |
| `src/bsky_saves/threads.py` | Replace the stale User-Agent literal with `f"bsky-saves/{__version__} (+...)"`. Add `from . import __version__` if not already present. Same pattern as v0.4.4 Task 9 applied to `images.py` and `articles.py`. |
| `.github/workflows/verify.yml` | Add `python scripts/fetch_gui.py` as a pre-pytest step so PR CI builds against a populated `_gui/`. |
| `.github/workflows/release.yml` | No code change required — `python -m build` already triggers Hatch hooks. Verify the `pypa/gh-action-pypi-publish` step still works with the larger wheel size. |

### Module-boundary intent

`_gui_serve.py` is a new private module (leading-underscore convention matching `_io.py` and `_net.py` from v0.4.4). It owns *all* static-file logic for the daemon — content-type detection, path-traversal defence, cache-header policy, SPA fallback, security-header set. `serve.py` calls into it as one thin dispatch branch; nothing else in `serve.py` knows about file extensions or CSP directives. This keeps `serve.py` from growing past its current size and isolates the static-file behaviour for future evolution (e.g. when the GUI team adds a service worker that needs separate caching rules).

## 4. Section A — build pipeline

### 4.1 Pin storage

`pyproject.toml` gains a new table:

```toml
[tool.bsky-saves]
gui_version = "0.5.3"
```

Note no leading `v`. The URL template adds it:
```
https://github.com/tenorune/bsky-saves-gui/releases/download/v{gui_version}/dist.tar.gz
```

`gui-dist.sha256` at repo root is byte-identical to the file the GitHub release ships:

```
1d42f62ed3ac63d0ebea1ebb0d6a2d7faf3245eeaf2e733b8601b0f1f56791d8  dist.tar.gz
```

(The hex above is the v0.5.2 value, kept here for shape illustration; the v0.5.3 value is populated when the GUI team cuts that release.)

Bumping the GUI version requires two edits:
1. Change `gui_version` in `pyproject.toml`.
2. Replace `gui-dist.sha256` with the byte-for-byte download from the new release.

CI verifies the pair matches a real release on every PR (see §4.3).

### 4.2 `scripts/fetch_gui.py`

Single entry point `fetch_gui(root: Path | None = None) -> None`:

1. Resolve `root` to the project root (default: walk up from the script's location to find `pyproject.toml`).
2. Parse `pyproject.toml` via `tomllib` (Python 3.11+ stdlib). Read `[tool.bsky-saves] gui_version`. Raise on missing or non-string.
3. Read `gui-dist.sha256`. Expect format `<hex>  dist.tar.gz`. Extract the hex.
4. Compute the target marker path: `root / "src" / "bsky_saves" / "_gui" / ".gui-version"`.
5. **Idempotency check.** If the marker file exists AND its first line equals `gui_version` AND its second line equals the expected sha256, return early (no download).
6. Download `https://github.com/tenorune/bsky-saves-gui/releases/download/v{gui_version}/dist.tar.gz` via `urllib.request.urlopen` (stdlib — avoids adding a build-time dep on httpx). Follow GitHub's standard redirect to the release-asset CDN (`*.githubusercontent.com` or `release-assets.githubusercontent.com`).
7. **Redirect-host check.** After download, inspect the final response URL. Allowed hosts: `github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com`, plus the actual subdomains GitHub redirects to. The script keeps a small allowlist; non-matching hosts abort with a clear message.
8. Verify SHA-256 of downloaded bytes against the expected hex. On mismatch: abort with a message naming both expected and actual hashes. Exit code 1.
9. Wipe `src/bsky_saves/_gui/` (atomic via temp dir + rename). Extract the tarball with `tarfile.open(...)`, applying:
   - **Path safety.** Reject any member whose normalised path escapes the extraction root (defence against tar-slip). Use the `tarfile.data_filter` introduced in Python 3.12 if available, otherwise a manual check.
   - **Strip a single leading `dist/` directory prefix** if every member starts with it. (The GUI team's bundle may or may not include this prefix; the script handles either layout.)
   - **Skip `CNAME`** (GitHub-Pages-specific).
   - **Skip dotfiles at the tarball root** other than the marker we'll write ourselves.
10. Write the marker `_gui/.gui-version` containing `{version}\n{sha256}\n`.
11. Print one summary line: `bsky-saves: vendored GUI bundle v{version} ({sha256_first_16}...)`.

Script is invocable standalone: `python scripts/fetch_gui.py`. Exits 0 on success, non-zero on any failure. No CLI args.

### 4.3 Hatch hook

`hatch_build.py` at project root:

```python
"""Build hook that vendors the GUI tarball before wheel/sdist packaging."""
from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

from scripts.fetch_gui import fetch_gui


class GuiBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:  # noqa: D401
        fetch_gui(Path(self.root))
```

`pyproject.toml` adds:

```toml
[tool.hatch.build.hooks.custom]
path = "hatch_build.py"
```

This wiring means:
- `python -m build` runs the hook → fetches+verifies → packages `_gui/` into the wheel.
- `pip install bsky-saves` from an sdist runs the hook → same flow.
- `pip install bsky-saves` from a wheel (the PyPI happy path) doesn't run the hook — the wheel already contains `_gui/`.
- `pip install -e .` runs the hook in setuptools-compat mode (Hatch handles this). Editable installs get a populated `_gui/`.

### 4.4 `.gitignore` and package data

Add to `.gitignore`:
```
src/bsky_saves/_gui/
```

`pyproject.toml`'s wheel-build target ensures `_gui/` is included in the built wheel. The exact key depends on Hatch's current API; implementation will check.

### 4.5 CI integration

`.github/workflows/verify.yml` (existing): add a step before `pytest`:
```yaml
- name: Fetch GUI bundle
  run: python scripts/fetch_gui.py
```

This ensures every PR builds against a populated `_gui/`, catches pin mismatches at PR time (failure modes: deleted GUI release, corrupted tarball, bad checksum pin).

`.github/workflows/release.yml`: no change. The Hatch hook fires during `python -m build`.

### 4.6 Security rationale

The pinned SHA-256 is the integrity boundary between the GUI release and the wheel. A compromised intermediate (CDN cache poisoning, malicious Action, MITM) cannot ship a different bundle through this pipeline without changing the bytes — and the SHA-256 mismatch aborts the build before the wheel is published. Reviewer signs off on the byte-exact pair `(gui_version, gui-dist.sha256)` at PR time.

## 5. Section B — `--gui` flag and static-file serving

### 5.1 CLI flag

`bsky-saves serve` gains:

```python
p_serve.add_argument(
    "--gui",
    action="store_true",
    default=False,
    help="Also serve the bundled GUI from / on the same port.",
)
```

`main()` passes `gui=args.gui` to `run_serve`.

### 5.2 Startup guard

`run_serve(*, port, allow_origins, verbose, gui)`:

```python
if gui:
    try:
        gui_root = resolve_gui_root()
    except GuiNotInstalledError as e:
        print(
            f"bsky-saves: --gui requires the bundled GUI tarball; "
            f"{e}. Reinstall from a wheel (pip install bsky-saves) "
            f"or run scripts/fetch_gui.py.",
            file=sys.stderr,
        )
        return 2
else:
    gui_root = None

handler_cls = make_handler(
    port=port, allow_origins=origins, verbose=verbose, gui_root=gui_root
)
```

`resolve_gui_root()` returns the `Path` to `src/bsky_saves/_gui/` resolved relative to the installed package (`importlib.resources` or `Path(__file__).parent / "_gui"`). Raises `GuiNotInstalledError("…/_gui/ is missing or empty")` if the directory doesn't exist or contains no `index.html`.

### 5.3 Handler integration

`make_handler` signature gains `gui_root: Path | None`. When `gui_root is None`, the handler behaves exactly as today (current v0.4.4 behaviour, no static serving). When `gui_root` is a `Path`, the dispatcher adds a static-file branch.

Dispatcher flow (under `--gui`):

```
Security gate (Host, Origin) → already exists, unchanged
  ↓
Method routing:
  POST → existing API dispatch only (POST never serves static files)
  GET / HEAD →
    1. Exact match in ROUTES table (e.g. /ping)
       → existing API handler
    2. Else if gui_root is not None:
         static_or_spa(self, self.path)
         → may serve a file under _gui/, fall back to index.html for SPA paths, or return False for /api-like paths not in ROUTES
    3. Else 404 (current behaviour)
  Other verbs → existing __getattr__ fallback path
```

API path precedence is guaranteed by step 1 running first. Even if the GUI bundle ever included a file named `ping`, the API handler wins.

### 5.4 `_gui_serve.serve_static_or_spa(handler, path) -> bool`

```python
def serve_static_or_spa(handler, request_path: str) -> bool:
    """Try to serve a static file from _gui/ for the given request path.
    Returns True if a response was sent (200 with file bytes, or 200 with
    index.html as SPA fallback). Returns False only when the caller should
    treat the request as a 404 (path looks like a stray API call, e.g.
    /undocumented-api-endpoint, rather than an SPA route).
    """
```

Algorithm:

1. Normalise `request_path` by stripping the query string and decoding percent-escapes.
2. **Path safety.** Resolve `gui_root / request_path.lstrip("/")` and call `.resolve()`. If the resolved path is not under `gui_root` (escape via `..`), serve `404` and return `True`.
3. If `request_path == "/"`, serve `_gui/index.html` with `Cache-Control: no-store`.
4. If resolved path exists and is a regular file: serve it with the cache-control class for its location (see §5.5). Set `Content-Type` via `mimetypes.guess_type` plus a small override dict for `.webmanifest` → `application/manifest+json`. Send security headers (§5.6). Return `True`.
5. If resolved path doesn't exist:
   - If `request_path` starts with any documented API prefix (`/ping`, `/fetch-image`, etc.) — return `False` (caller sends 404 via existing JSON-error path).
   - Else SPA fallback: serve `_gui/index.html` with `Cache-Control: no-store` and 200 status (the browser fragment router takes over). Return `True`.

For HEAD requests, send the same headers and `Content-Length` but write zero body bytes.

### 5.5 Cache-Control policy

| Path pattern | Header |
|---|---|
| `/assets/*` (Vite-hashed, content-immutable) | `Cache-Control: public, max-age=31536000, immutable` |
| `/` and SPA fallback (always `index.html`) | `Cache-Control: no-store` |
| Other unhashed static files (`manifest.webmanifest`, `favicon.ico`, `sw.js`, `icons/*.png`, `icons/*.svg`) | `Cache-Control: no-cache` |

`Cache-Control: no-cache` revalidates per request (still benefits from `If-None-Match` once we ever ship ETags; not in scope here). `no-store` is strictly stricter and prevents `index.html` from ever being cached — necessary because it's the SPA shell that loads the hashed assets, and a stale shell breaks deploys.

### 5.6 Security headers on static-file responses

Every `--gui` static-file response (including HEAD and the SPA fallback) carries:

```
Content-Security-Policy: default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; font-src 'self'; connect-src 'self' https: http://127.0.0.1:* http://localhost:*; worker-src 'self' blob:; manifest-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Cross-Origin-Opener-Policy: same-origin
```

The CSP string is the exact value from MVP spec §4.6; do not vary. `frame-ancestors 'none'` is the key directive only available via header (not meta tag) — that's the main win of daemon-served over Pages-hosted.

The v0.4.4 `X-Content-Type-Options: nosniff` header continues to apply (set by `_security_headers` already; both API and static responses get it). `Cache-Control` is set by the static-file branch (overriding `_security_headers`' `no-store` for `/assets/*`).

JSON API responses (`/ping`, `/fetch-image`, etc.) do NOT get the static-file security headers — they're not HTML, no CSP applies. Existing v0.4.4 behaviour unchanged.

### 5.7 Path-traversal defence

`serve_static_or_spa` resolves the candidate file path with `(gui_root / user_path).resolve()` and confirms it's under `gui_root.resolve()`. Any escape (via `..`, symlinks, etc.) returns 404. Tested in `test_serve_gui.py`.

## 6. Section C — drive-by cleanups

### 6.1 Remove unused `import httpx` from `images.py`

After v0.4.4 Task 8, `images.download_to` switched from `httpx.get` to `safe_http_get`. The `import httpx` line at the top of `src/bsky_saves/images.py` is now unused. Delete it. Existing tests cover image-download behaviour and will continue to pass.

### 6.2 Bump `threads.py` User-Agent

`src/bsky_saves/threads.py` currently sends a User-Agent string with a stale version literal (similar to the issue v0.4.4 fixed in `articles.py` and `images.py`). Apply the same pattern: import `from . import __version__` and build the UA as an f-string.

If `threads.py` does NOT currently set a custom User-Agent, this drive-by is a no-op; the implementer should verify and report.

## 7. Section D — pre-release CI smoke test

New workflow `.github/workflows/smoke.yml`:

**Triggers:**
- `push` to `main`.
- `push` of `v*` tags (gates the release-publish workflow).
- Optional `workflow_dispatch` input `gui_pin_corrupt: bool` for the negative-pin variant; default `false`.

**Job:**

```yaml
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - name: Optionally corrupt the GUI pin
        if: ${{ github.event.inputs.gui_pin_corrupt == 'true' }}
        run: sed -i 's/^./0/' gui-dist.sha256  # flips first hex char
      - name: Build the wheel
        run: |
          pip install build
          python -m build
      - name: Install in fresh venv
        run: |
          python -m venv /tmp/smoke
          /tmp/smoke/bin/pip install dist/bsky_saves-*-py3-none-any.whl
      - name: Start daemon
        run: |
          /tmp/smoke/bin/bsky-saves serve --gui --port 47826 &
          echo $! > /tmp/smoke-pid
          sleep 1
      - name: Smoke endpoints
        run: |
          curl -fsS http://127.0.0.1:47826/ | grep -q '<title>'
          curl -fsS http://127.0.0.1:47826/ping | python -c 'import sys, json; d = json.load(sys.stdin); assert d["name"] == "bsky-saves"'
          ASSET=$(ls /tmp/smoke/lib/python*/site-packages/bsky_saves/_gui/assets/ | head -1)
          curl -fsS "http://127.0.0.1:47826/assets/$ASSET" > /dev/null
          STATUS=$(curl -sS -o /dev/null -w "%{http_code}" -X POST \
              http://127.0.0.1:47826/fetch-image \
              -H 'Content-Type: application/json' \
              -H "Origin: http://127.0.0.1:47826" \
              -d '{"url":"https://evil.com/x.png"}')
          test "$STATUS" = "400"
      - name: Kill daemon
        if: always()
        run: kill "$(cat /tmp/smoke-pid)" || true
```

The `gui_pin_corrupt` input is the negative-pin variant called out by acceptance criterion 8: an operator manually triggers the workflow with the corruption flag set; the `python -m build` step should fail with the SHA-256 mismatch message.

## 8. Test plan

### 8.1 `tests/test_fetch_gui.py` (new, ~10 tests)

- Reads `gui_version` from a synthetic `pyproject.toml` in a `tmp_path`.
- Aborts on SHA-256 mismatch (asserts no `_gui/` created, no marker written).
- Aborts on non-GitHub redirect host (mock `urlopen` to return a `Location: https://evil.com/...` redirect).
- Idempotency: second call with matching marker skips the download (assert `urlopen` not called).
- Idempotency miss on version bump: marker has old version → re-fetches.
- Idempotency miss on sha256 bump: marker has matching version but different sha256 → re-fetches.
- Strips `dist/` prefix correctly: tarball with `dist/index.html` produces `_gui/index.html`.
- No-prefix layout: tarball with `index.html` at root produces `_gui/index.html`.
- Skips `CNAME` at any depth.
- Tar-slip defence: tarball member with `../../../etc/passwd` path aborts.
- Final summary line is printed on success.

Fixtures: `tests/conftest.py` adds a `gui_tarball_fixture` factory that takes a dict of `{relative_path: bytes}` and returns a tuple of `(bytes_blob, sha256_hex_string)`. Used by both this file and `test_serve_gui.py`.

### 8.2 `tests/test_serve_gui.py` (new, ~12 tests)

Uses a temp `_gui/` populated from `gui_tarball_fixture` and monkeypatches `_gui_serve.resolve_gui_root` to point at the temp dir.

- `bsky-saves serve --gui` mounts `index.html` at `/` with `Cache-Control: no-store` and the spec'd security headers.
- `/assets/<hashed>.js` returns 200 with `Cache-Control: public, max-age=31536000, immutable` and `Content-Type: application/javascript`.
- `/manifest.webmanifest` returns 200 with `Cache-Control: no-cache` and `Content-Type: application/manifest+json`.
- SPA fallback: `/some/non-existent/route` returns `index.html` 200 (not 404).
- API precedence: `/ping` returns the JSON ping, NOT a fallback `index.html`, even when `--gui` is on.
- Path traversal: `/../../../etc/passwd` returns 404 (or similar safe status).
- HEAD `/` returns same headers, zero body.
- Security headers on static responses include CSP, X-Frame-Options, Referrer-Policy, Cross-Origin-Opener-Policy with the exact values from spec §4.6.
- POST to `/` is rejected by the dispatcher (404 from the ROUTES table; static branch only fires on GET/HEAD).
- `bsky-saves serve --gui` startup with empty `_gui/` fails with the spec'd error message and exit code 2 (test by mocking `resolve_gui_root` to raise).
- `--gui` doesn't break the v0.4.4 behaviour: Origin, Host, body-cap checks still apply.
- Without `--gui` (default), the static-file branch is dead code: `/` returns 404 (existing behaviour).

### 8.3 Regression coverage

Every test currently in `tests/test_serve.py` (~109) continues to run with `gui=False` and pass unchanged. No assertions about `--gui` are added there; that's `test_serve_gui.py`'s job.

### 8.4 CI smoke as integration test

Per §7. The smoke job is a separate workflow but counts toward acceptance criterion 1, 2, 5–7.

### 8.5 Final test count projection

244 (v0.4.4 baseline after PR merge) + 22 (10 fetch_gui + 12 serve_gui) ≈ **266 tests** after v0.5.0.

## 9. Acceptance criteria

Mapped to GUI MVP spec §8:

1. **`pip install bsky-saves && bsky-saves serve --gui` mounts GUI at `http://127.0.0.1:47826/`.** Verified by CI smoke (§7 step 4) + `test_serve_gui.py`.
2. **Page renders; `GET /ping` returns expected shape.** Verified by smoke + existing `test_serve.py::test_handle_ping_returns_features_array`.
3. **GUI's `probeHelper()` resolves on served origin.** GUI-side responsibility; we ship a daemon that matches the contract.
4. **`OutdatedHelperBanner` does NOT render against current wheel.** v0.5.0 ≥ `MIN_HELPER_VERSION = 0.4.1` (per GUI spec §5.9). Holds.
5. **Cross-origin request from `evil.com` to `/fetch-image` is blocked.** Verified by `test_serve.py::test_cors_disallowed_origin_returns_403` (v0.4.4 test).
6. **`Host: evil.com` returns `421`.** Verified by `test_serve.py::test_host_unknown_domain_returns_421` (v0.4.4 test).
7. **`POST /fetch-image` with `{"url":"https://evil.com/x.png"}` returns `400 {"error":"url not allowed"}`.** Verified by existing test + CI smoke step 5.
8. **Build-hook CI failure on a deliberately corrupted `gui-dist.sha256` pin.** Verified by `smoke.yml` with `gui_pin_corrupt=true` input.

## 10. Release process

Standard for this project:

1. Land all changes on `claude/bsky-saves-next-phase-o7mOi` via small commits (one per task in the implementation plan).
2. Open PR to `main`. Self-review; user reviews; merge via GitHub UI.
3. User creates `v0.5.0` tag on the merge commit via GitHub UI.
4. `release.yml` publishes `bsky-saves==0.5.0` to PyPI via trusted publishing.
5. Post-publish: the bsky-saves-gui team can wire up their deferred runtime-smoke and version-coordination gates against the real wheel.

**GUI-side coordination:** the v0.5.0 implementation pins against `gui_version = "0.5.3"`. That release must exist before the wheel can be built. The user has indicated v0.5.3 is imminent; the fetch script and CI will be parameterised against `0.5.3` from the start, and `gui-dist.sha256` will be populated from the release at the moment the implementation reaches the build-pipeline step.

## 11. Decisions log

| Date | Decision | Rationale |
|---|---|---|
| 2026-05-13 | Single monolithic v0.5.0 release covering all Bucket B work plus the unused-httpx-import and stale-threads-UA cleanups | Build hook has no consumer until `--gui` ships; splitting them would mean a useless intermediate release. Cleanups are 10-line changes touching same neighbourhood. |
| 2026-05-13 | Hybrid build-hook backend: `scripts/fetch_gui.py` script invoked by both a Hatch hook (`hatch_build.py`) and direct contributor use | Single implementation, multiple entry points. CI invokes the script for clear log output; pip-install-from-sdist invokes via the hook. Editable installs work too. |
| 2026-05-13 | `GUI_VERSION` in `pyproject.toml` `[tool.bsky-saves]`; `gui-dist.sha256` as standalone file in GitHub-release byte-format | Sha256 file is byte-identical to what `curl` downloads, simplifying verification to `shasum -c`. Version lives with other build metadata. |
| 2026-05-13 | `--gui` startup with empty `_gui/` exits with code 2 and clear actionable error | Hard-fail matches v0.4.4's behaviour for port-bind failures. Silently degrading or rendering a placeholder masks CI failures. |
| 2026-05-13 | Static-file logic in new `_gui_serve.py` module rather than inside `serve.py` | Keeps `serve.py` from growing past ~700 lines. Mirrors the `_io.py` and `_net.py` module-boundary pattern established in v0.4.4. |
| 2026-05-13 | `serve_static_or_spa` returns `True`/`False`: `True` = response sent, `False` = caller should 404 | Lets the dispatcher distinguish "this looks like an unrelated API call we don't have" from "this looks like an SPA route." API-prefix paths fall to 404; everything else falls to SPA index.html. |
| 2026-05-13 | Initial pin target is `gui_version = "0.5.3"`; the GUI team will cut that release on signal | v0.5.2 was a workflow test; v0.5.3 is the first "real" pin. Pin and `gui-dist.sha256` are populated together when the release is live. |
| 2026-05-13 | Smoke test in CI gates the release-publish workflow; negative-pin variant is `workflow_dispatch`-only | Every push to main runs the positive smoke; the negative-pin variant runs on demand to avoid breaking every PR. AC#8 is satisfied by manual exercise. |

## 12. Open questions

None outstanding for v0.5.0 scope. Implementation may surface incidental questions about Hatch hook semantics or `mimetypes` quirks; those go in the plan, not back to the spec.
