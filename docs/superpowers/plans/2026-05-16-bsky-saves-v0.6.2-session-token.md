# bsky-saves v0.6.2 — Session-token implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-installation session-token authentication to the local helper daemon, gated on a persistent on-disk secret, so `bsky-saves-gui` can pair once and stay paired across daemon restarts.

**Architecture:** Token lives at platform-conventional `<config_dir>/bsky-saves/token` (0600 perms, atomic-write). `serve.py::_security_gate` gains a token check after Host + Origin; `/ping` is exempt for pre-pairing diagnostic probes. The bundled GUI's `index.html` ships with a sentinel placeholder (`__BSKY_SAVES_TOKEN__`) that `_gui_serve.py` substitutes per-request. A new `bsky-saves token [--rotate]` CLI surfaces the token for hosted-PWA pairing. Protocol bumps from `"1"` to `"2"`.

**Tech Stack:** Python 3.11+, stdlib only (no new deps). `hatchling` build backend. `pytest` for tests. `httpx`/`respx` for the existing HTTP test patterns.

**Spec:** `docs/superpowers/specs/2026-05-16-bsky-saves-v0.6.2-session-token.md`

**Branch:** `claude/installer-prep` (extends PR #9).

---

## File map

| File | Disposition | Responsibility |
|---|---|---|
| `src/bsky_saves/_io.py` | Modify | Add `config_dir()` and `read_or_create_token()`; existing `atomic_write_inventory` unchanged. |
| `src/bsky_saves/serve.py` | Modify | `_check_token()` method; `EXEMPT_ROUTES` constant; wire into `_security_gate`; bump `_PROTOCOL_VERSION = "2"`. |
| `src/bsky_saves/_gui_serve.py` | Modify | `_send_file` substitutes `__BSKY_SAVES_TOKEN__` in `index.html` and SPA fallback bodies. Add token kwarg to `serve_static_or_spa`. |
| `src/bsky_saves/cli.py` | Modify | New `token` subparser with `--rotate` flag; new `main()` branch dispatching to it. |
| `tests/test_io.py` | Modify | New tests for `config_dir()` (platform branches via `sys.platform` monkeypatch) and `read_or_create_token()` (lazy-create, idempotent read, 0600 perms, atomic-write). |
| `tests/test_serve.py` | Modify | New `paired_helper` fixture; retrofit every existing credentialed-endpoint test to send `Authorization: Bearer <token>`; new auth-specific tests (401 missing, 401 wrong, `/ping` exempt, OPTIONS exempt, rotate-invalidates). |
| `tests/test_gui_serve.py` (or extend `test_serve.py`'s `--gui` block) | Modify | New tests for placeholder substitution in served `index.html` and SPA fallback; non-index files unchanged. |
| `tests/test_token_cli.py` | Create | Tests for `bsky-saves token` (prints existing, lazy-generates) and `bsky-saves token --rotate` (writes new value, invalidates prior). |
| `pyproject.toml` | Modify | `version = "0.6.2"`. |
| `docs/protocol-versioning.md` | Modify | Add Changelog subsection: `"1"` → v0.6.1, `"2"` → v0.6.2. |
| `README.md` | Modify | New `### Pairing` subsection under `## bsky-saves serve`; one-line upgrade note in `## Upgrade`. |

---

## Task 1: Token storage primitives in `_io.py`

**Files:**
- Modify: `src/bsky_saves/_io.py`
- Test: `tests/test_io.py`

- [ ] **Step 1.1: Write failing test for `config_dir()` on Linux (default + XDG)**

Add to `tests/test_io.py`:

```python
import sys
from bsky_saves._io import config_dir, read_or_create_token


def test_config_dir_linux_default(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_dir() == tmp_path / ".config" / "bsky-saves"


def test_config_dir_linux_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_dir() == tmp_path / "xdg" / "bsky-saves"


def test_config_dir_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_dir() == tmp_path / "Library" / "Application Support" / "bsky-saves"


def test_config_dir_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    assert config_dir() == tmp_path / "AppData" / "Roaming" / "bsky-saves"
```

- [ ] **Step 1.2: Run tests, verify they fail**

```bash
/tmp/venv/bin/pytest tests/test_io.py -v -k config_dir
```

Expected: 4 failures with `ImportError: cannot import name 'config_dir' from 'bsky_saves._io'`.

- [ ] **Step 1.3: Implement `config_dir()` in `_io.py`**

Add to `src/bsky_saves/_io.py`:

```python
import sys
from pathlib import Path


def config_dir() -> Path:
    """Return the platform-conventional bsky-saves config directory.

    - Linux/*BSD: $XDG_CONFIG_HOME/bsky-saves or ~/.config/bsky-saves
    - macOS:      ~/Library/Application Support/bsky-saves
    - Windows:    %APPDATA%\\bsky-saves

    The directory is NOT created by this function; callers that need to
    write should mkdir(parents=True, exist_ok=True) themselves.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "bsky-saves"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "bsky-saves"
        return Path.home() / "AppData" / "Roaming" / "bsky-saves"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "bsky-saves"
    return Path.home() / ".config" / "bsky-saves"
```

- [ ] **Step 1.4: Run tests, verify pass**

```bash
/tmp/venv/bin/pytest tests/test_io.py -v -k config_dir
```

Expected: 4 passed.

- [ ] **Step 1.5: Write failing test for `read_or_create_token()`**

Add to `tests/test_io.py`:

```python
import re


def test_read_or_create_token_lazy_creates(monkeypatch, tmp_path):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    token = read_or_create_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]{42,44}", token), token
    assert (tmp_path / "token").read_text(encoding="utf-8").strip() == token


def test_read_or_create_token_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    first = read_or_create_token()
    second = read_or_create_token()
    assert first == second


def test_read_or_create_token_file_perms(monkeypatch, tmp_path):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    read_or_create_token()
    perms = (tmp_path / "token").stat().st_mode & 0o777
    if sys.platform != "win32":
        assert perms == 0o600, oct(perms)


def test_read_or_create_token_strips_trailing_newline(monkeypatch, tmp_path):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "token").write_text("preexisting-token-value\n", encoding="utf-8")
    assert read_or_create_token() == "preexisting-token-value"


def test_read_or_create_token_multiline_returns_first_line(monkeypatch, tmp_path):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "token").write_text("first-line\nsecond-line\n", encoding="utf-8")
    assert read_or_create_token() == "first-line"
```

- [ ] **Step 1.6: Run tests, verify they fail**

```bash
/tmp/venv/bin/pytest tests/test_io.py -v -k read_or_create
```

Expected: 5 failures, mostly `ImportError: cannot import name 'read_or_create_token'`.

- [ ] **Step 1.7: Implement `read_or_create_token()` in `_io.py`**

Add to `src/bsky_saves/_io.py`:

```python
import base64
import secrets


def read_or_create_token() -> str:
    """Return the on-disk session token, lazy-generating if absent.

    Format: 32 random bytes, base64url-encoded without padding (~43 chars).
    Location: <config_dir>/token. File perms: 0o600. Atomic-write via temp
    file + os.replace. Returns the first non-empty line of the file, stripped.

    If multiple bsky-saves processes race to create the file, the loser's
    os.replace overwrites the winner; whichever token wins the race becomes
    canonical. The user re-pairs at most once.
    """
    cdir = config_dir()
    path = cdir / "token"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        # Empty file → fall through and regenerate.

    cdir.mkdir(parents=True, exist_ok=True)
    fresh = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (fresh + "\n").encode("ascii"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return fresh
```

- [ ] **Step 1.8: Run all `_io` tests, verify pass**

```bash
/tmp/venv/bin/pytest tests/test_io.py -v
```

Expected: 9 passed (3 existing + 6 new).

- [ ] **Step 1.9: Commit**

```bash
git add src/bsky_saves/_io.py tests/test_io.py
git commit -m "feat(_io): add config_dir() and read_or_create_token() for v0.6.2 session token

Platform-conventional config dir resolution (XDG / macOS Application
Support / Windows APPDATA), and lazy-generated 32-byte base64url
session token at <config_dir>/token with 0600 perms and atomic write.

Foundation for the helper-side session-token auth landing in v0.6.2
(see docs/superpowers/specs/2026-05-16-bsky-saves-v0.6.2-session-token.md)."
```

---

## Task 2: `bsky-saves token` CLI subcommand

**Files:**
- Modify: `src/bsky_saves/cli.py`
- Create: `tests/test_token_cli.py`

- [ ] **Step 2.1: Write failing tests in new `tests/test_token_cli.py`**

```python
"""Tests for the `bsky-saves token` CLI subcommand."""
from __future__ import annotations

import re

from bsky_saves.cli import main


def test_token_prints_existing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "token").write_text("hardcoded-test-token\n", encoding="utf-8")
    rc = main(["token"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "hardcoded-test-token"


def test_token_lazy_generates_on_first_call(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    rc = main(["token"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert re.fullmatch(r"[A-Za-z0-9_-]{42,44}", out), out
    assert (tmp_path / "token").exists()


def test_token_rotate_changes_value(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    rc1 = main(["token"])
    first = capsys.readouterr().out.strip()
    rc2 = main(["token", "--rotate"])
    second = capsys.readouterr().out.strip()
    assert rc1 == 0 and rc2 == 0
    assert first != second
    assert (tmp_path / "token").read_text(encoding="utf-8").strip() == second


def test_token_rotate_on_empty_state_generates(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: tmp_path)
    rc = main(["token", "--rotate"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert re.fullmatch(r"[A-Za-z0-9_-]{42,44}", out), out
```

- [ ] **Step 2.2: Run tests, verify they fail**

```bash
/tmp/venv/bin/pytest tests/test_token_cli.py -v
```

Expected: 4 failures (`SystemExit` from argparse — "invalid choice: 'token'").

- [ ] **Step 2.3: Add `token` subparser to `_build_parser()` in `cli.py`**

Insert after the `p_serve` block (around line 178, before `return parser`):

```python
    p_token = sub.add_parser(
        "token",
        help="Print the session token used by bsky-saves-gui to pair with this helper.",
    )
    p_token.add_argument(
        "--rotate",
        action="store_true",
        help="Generate a fresh token, invalidating any paired GUI sessions.",
    )
```

- [ ] **Step 2.4: Add `token` dispatch branch in `main()`**

Insert in `main()` (after the `serve` branch, before any catch-all return):

```python
    if args.cmd == "token":
        from ._io import config_dir, read_or_create_token
        import base64
        import os as _os
        import secrets

        if args.rotate:
            cdir = config_dir()
            cdir.mkdir(parents=True, exist_ok=True)
            fresh = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
            path = cdir / "token"
            tmp = path.with_suffix(".tmp")
            fd = _os.open(str(tmp), _os.O_CREAT | _os.O_TRUNC | _os.O_WRONLY, 0o600)
            try:
                _os.write(fd, (fresh + "\n").encode("ascii"))
            finally:
                _os.close(fd)
            _os.replace(tmp, path)
            print(fresh)
            return 0

        print(read_or_create_token())
        return 0
```

Note: the rotate path duplicates a small amount of `read_or_create_token`'s body because it needs `O_TRUNC` (overwrite existing) rather than `O_EXCL` (fail if exists). Acceptable duplication — extracting a `_write_token()` helper is YAGNI given two callers.

- [ ] **Step 2.5: Run tests, verify pass**

```bash
/tmp/venv/bin/pytest tests/test_token_cli.py -v
```

Expected: 4 passed.

- [ ] **Step 2.6: Run full suite to confirm no regression**

```bash
/tmp/venv/bin/pytest tests/ -q
```

Expected: all 357+ passed (355 prior + 4 new + a few from Task 1).

- [ ] **Step 2.7: Commit**

```bash
git add src/bsky_saves/cli.py tests/test_token_cli.py
git commit -m "feat(cli): add 'bsky-saves token [--rotate]' subcommand

Prints the current session token; lazy-generates if none exists.
--rotate writes a fresh token, invalidating any paired GUI sessions.

Companion CLI to the helper-side auth check landing in the next
commit. Surfaces the pairing primitive for the hosted-PWA flow
(saves.lightseed.net needs the token pasted by the user) and gives
incident-response a single command for credential rotation."
```

---

## Task 3: Token enforcement in `serve.py` + test retrofit

This is the largest task. It (a) adds the auth check, (b) bumps `_PROTOCOL_VERSION`, (c) retrofits ~15 existing tests to send `Authorization: Bearer`, and (d) adds new auth-specific tests. All in one commit so the suite is green at task boundaries.

**Files:**
- Modify: `src/bsky_saves/serve.py`
- Modify: `tests/test_serve.py`

- [ ] **Step 3.1: Add `EXEMPT_ROUTES` and `_check_token` and bump `_PROTOCOL_VERSION` in `serve.py`**

Replace the existing `_PROTOCOL_VERSION = "1"` line with:

```python
# Bump rules: docs/protocol-versioning.md
_PROTOCOL_VERSION = "2"
```

Then add (after the `_BODY_REJECTED` block, near `_PROTOCOL_VERSION`):

```python
# Routes that bypass token authentication. /ping is the pre-pairing
# diagnostic surface (probeHelper reads it before the GUI has any
# token); see docs/superpowers/specs/2026-05-16-bsky-saves-v0.6.2-session-token.md §5.
EXEMPT_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/ping"),
})
```

Then add the check inside the `Handler` class in `make_handler()`. Find `_check_origin` and add this method after it:

```python
        def _check_token(self, method: str) -> bool:
            """Reject requests missing or carrying a wrong session token.
            EXEMPT_ROUTES bypass; OPTIONS preflight always bypasses (CORS
            requires preflight to succeed before custom headers can be
            sent). Static-file requests (gui_root is not None, method is
            GET/HEAD, path is not in ROUTES) also bypass — the GUI loads
            index.html before it can read the meta tag, and static assets
            contain no user data."""
            from ._io import read_or_create_token
            import hmac

            if method == "OPTIONS":
                return True
            if (method, self.path) in EXEMPT_ROUTES:
                return True
            # Static-file branch: GET/HEAD requests to non-API paths in
            # --gui mode are served from disk without auth.
            if (
                gui is not None
                and method in ("GET", "HEAD")
                and (method, self.path) not in ROUTES
            ):
                return True

            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not header.startswith(prefix):
                self._send_json_error(401, "authentication required")
                return False
            presented = header[len(prefix):]
            expected = read_or_create_token()
            if not hmac.compare_digest(presented, expected):
                self._send_json_error(401, "authentication required")
                return False
            return True
```

Then modify `_security_gate` to call it (after the Origin check):

```python
        def _security_gate(self, method: str) -> bool:
            if not self._check_host():
                return False
            if not self._check_origin():
                return False
            if not self._check_token(method):
                return False
            return True
```

- [ ] **Step 3.2: Run full suite to see the carnage**

```bash
/tmp/venv/bin/pytest tests/test_serve.py -q 2>&1 | tail -30
```

Expected: many failures — every existing credentialed-endpoint test now gets 401 because they don't send `Authorization`. `/ping` tests should still pass. OPTIONS tests should still pass. This confirms the gate works; we now retrofit.

- [ ] **Step 3.3: Add `paired_helper` fixture to `tests/test_serve.py`**

Insert near the top of `tests/test_serve.py` (after the existing `serve_in_background` definition):

```python
import pytest


TEST_TOKEN = "test-session-token-please-ignore-aaaaaaaaaaa"


@pytest.fixture
def paired_helper(monkeypatch, tmp_path):
    """Configure the helper to use a known test token. Yields the token
    string so tests can include it in Authorization headers.

    Monkeypatches _io.config_dir to a per-test temp dir and writes the
    token there. After test teardown, monkeypatch reverts both.
    """
    cdir = tmp_path / "bsky-saves"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "token").write_text(TEST_TOKEN + "\n", encoding="utf-8")
    monkeypatch.setattr("bsky_saves._io.config_dir", lambda: cdir)
    yield TEST_TOKEN


def _auth_headers(token: str, extra: dict | None = None) -> dict:
    """Build a request headers dict that includes the paired Authorization."""
    headers = {"Authorization": f"Bearer {token}"}
    if extra:
        headers.update(extra)
    return headers
```

- [ ] **Step 3.4: Retrofit existing credentialed-endpoint tests**

For each existing test that calls a route in `ROUTES` other than `("GET", "/ping")`, add `paired_helper` to the signature and thread it through. The retrofit pattern:

Before:
```python
def test_fetch_image_happy_path():
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/fetch-image",
            method="POST",
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
```

After:
```python
def test_fetch_image_happy_path(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
```

Tests to retrofit (all in `tests/test_serve.py`):

```
test_fetch_image_happy_path
test_fetch_image_subdomain_wildcard_allowed
test_fetch_image_bare_bsky_app_rejected
test_fetch_image_lookalike_domain_rejected
test_fetch_image_http_scheme_rejected
test_fetch_image_missing_url_rejected
test_fetch_image_upstream_4xx_passed_through
test_fetch_image_network_error_returns_502
test_extract_article_happy_path
test_extract_article_empty_body_returns_200_with_note
test_extract_article_disallowed_scheme
test_extract_article_missing_url
test_extract_article_upstream_5xx_passed_through
test_extract_article_network_error_returns_502
```

For every test that sends `Origin` or other custom headers, fold them into `_auth_headers(paired_helper, {"Origin": "..."})`.

Run `grep -n "def test_" tests/test_serve.py` to spot any other tests touching POST routes; retrofit each one.

For the OPTIONS preflight tests (`test_options_preflight_returns_204_with_cors`, etc.) — leave alone, OPTIONS is exempt.

For the `/ping` tests (`test_ping_returns_full_shape`, `test_ping_includes_gui_bundled_when_marker_present`, `test_ping_origin_disallowed_returns_403`, `test_verbose_logs_request_to_stderr`, etc.) — leave alone, `/ping` is exempt.

The `test_cors_404_response_still_carries_cors_headers` test sends `GET /admin` — that's a path not in `ROUTES`. With the static-file branch bypass in `_check_token` *only* when `gui is not None`, this test (which does NOT use `gui=True`) will see 401 instead of 404. The fix: this test asserts CORS on a 404 specifically. We need to update it to add `paired_helper` and auth so it actually exercises the 404 path through to a real not-found response:

Before:
```python
def test_cors_404_response_still_carries_cors_headers():
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port, "/admin", headers={"Origin": DEFAULT_ORIGIN}
        )
    assert status == 404
    assert headers.get("Access-Control-Allow-Origin") == DEFAULT_ORIGIN
```

After:
```python
def test_cors_404_response_still_carries_cors_headers(paired_helper):
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port, "/admin",
            headers=_auth_headers(paired_helper, {"Origin": DEFAULT_ORIGIN}),
        )
    assert status == 404
    assert headers.get("Access-Control-Allow-Origin") == DEFAULT_ORIGIN
```

- [ ] **Step 3.5: Run the suite, confirm the retrofit is complete**

```bash
/tmp/venv/bin/pytest tests/test_serve.py -q
```

Expected: all existing tests pass (or only obvious post-retrofit failures remain).

If any tests still fail, the most common cause is forgotten `paired_helper` or a test that previously didn't send `Authorization` and now needs to. Fix iteratively.

- [ ] **Step 3.6: Update `test_ping_returns_full_shape` to assert `protocol: "2"`**

In `tests/test_serve.py`, find the test and change `"protocol": "1"` to `"protocol": "2"`. There's only one occurrence in the suite.

- [ ] **Step 3.7: Run /ping tests**

```bash
/tmp/venv/bin/pytest tests/test_serve.py -v -k ping
```

Expected: all `/ping` tests pass, including the now-protocol-2 assertion.

- [ ] **Step 3.8: Add new auth-specific tests**

Add to `tests/test_serve.py`:

```python
def test_credentialed_endpoint_401_on_missing_authorization(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/fetch-image",
            method="POST",
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
    assert status == 401
    assert json.loads(body) == {"error": "authentication required"}


def test_credentialed_endpoint_401_on_wrong_token(paired_helper):
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/fetch-image",
            method="POST",
            headers=_auth_headers("not-the-real-token"),
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
    assert status == 401
    assert json.loads(body) == {"error": "authentication required"}


def test_credentialed_endpoint_401_on_non_bearer_scheme(paired_helper):
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/fetch-image",
            method="POST",
            headers={"Authorization": f"Basic {paired_helper}"},
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
    assert status == 401


def test_ping_does_not_require_token(paired_helper):
    with serve_in_background() as (port, _):
        status, _, _ = _request(port, "/ping")
    assert status == 200


def test_options_preflight_does_not_require_token(paired_helper):
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/fetch-image",
            method="OPTIONS",
            headers={"Origin": DEFAULT_ORIGIN, "Access-Control-Request-Method": "POST"},
        )
    assert status == 204


def test_rotate_invalidates_running_daemon(paired_helper, tmp_path):
    """If --rotate is called from a separate process while serve is running,
    the next request from the now-stale-token client gets 401. Implementation
    detail this verifies: _check_token reads the token on every request, not
    once at startup."""
    with serve_in_background() as (port, _):
        # Sanity: original token works.
        status, _, _ = _request(
            port, "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
        # 200/400/502 all acceptable here — point is "not 401."
        assert status != 401

        # Simulate --rotate by overwriting the token file.
        cdir = tmp_path / "bsky-saves"
        (cdir / "token").write_text("a-new-rotated-token\n", encoding="utf-8")

        # Old token now invalid.
        status, _, _ = _request(
            port, "/fetch-image",
            method="POST",
            headers=_auth_headers(paired_helper),
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
        assert status == 401

        # New token works.
        status, _, _ = _request(
            port, "/fetch-image",
            method="POST",
            headers=_auth_headers("a-new-rotated-token"),
            body={"url": "https://cdn.bsky.app/img/abc"},
        )
        assert status != 401
```

- [ ] **Step 3.9: Run new tests, verify pass**

```bash
/tmp/venv/bin/pytest tests/test_serve.py -v -k "token or auth or rotate or 401"
```

Expected: all pass.

- [ ] **Step 3.10: Run full suite, verify green**

```bash
/tmp/venv/bin/pytest tests/ -q
```

Expected: 365+ passed.

- [ ] **Step 3.11: Commit**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): require Authorization: Bearer on credentialed endpoints

v0.6.2 session-token enforcement. _security_gate gains a third check
after Host + Origin: every route except /ping (and OPTIONS preflight,
and static-file paths when --gui is on) requires Authorization: Bearer
<token>, where <token> is the value at <config_dir>/bsky-saves/token
(lazy-generated via _io.read_or_create_token).

Wrong / missing token → 401 {'error': 'authentication required'}.
Constant-time comparison via hmac.compare_digest. Token is re-read
from disk on every request so bsky-saves token --rotate from a
separate shell invalidates a running daemon's accepted token without
restart.

Protocol bumps from \"1\" to \"2\" (auth-requirement change per
docs/protocol-versioning.md). Existing /ping shape unchanged.

All existing credentialed-endpoint tests retrofitted to use a new
paired_helper fixture that points config_dir at a temp dir with a
known test token. New auth-specific tests cover the missing-header,
wrong-token, non-Bearer, /ping-exemption, OPTIONS-exemption, and
rotate-invalidation paths.

Spec: docs/superpowers/specs/2026-05-16-bsky-saves-v0.6.2-session-token.md §5, §9."
```

---

## Task 4: Token injection into served `index.html`

**Files:**
- Modify: `src/bsky_saves/_gui_serve.py`
- Modify: `tests/test_serve.py`

- [ ] **Step 4.1: Write failing tests for substitution behavior**

Add to `tests/test_serve.py`:

```python
def test_index_html_substitutes_token_placeholder(paired_helper, tmp_path, monkeypatch):
    """When --gui is on, GET / serves index.html with the sentinel
    __BSKY_SAVES_TOKEN__ replaced by the current session token."""
    from bsky_saves import _gui_serve
    gui_root = tmp_path / "_gui"
    gui_root.mkdir()
    (gui_root / "index.html").write_text(
        '<html><head><meta name="bsky-saves-token" content="__BSKY_SAVES_TOKEN__"></head></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui_root)

    with serve_in_background(gui=True) as (port, _):
        status, _, body = _request(port, "/")
    assert status == 200
    text = body.decode("utf-8")
    assert "__BSKY_SAVES_TOKEN__" not in text
    assert paired_helper in text


def test_index_html_substitutes_in_spa_fallback(paired_helper, tmp_path, monkeypatch):
    from bsky_saves import _gui_serve
    gui_root = tmp_path / "_gui"
    gui_root.mkdir()
    (gui_root / "index.html").write_text(
        '<html><head><meta name="bsky-saves-token" content="__BSKY_SAVES_TOKEN__"></head></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui_root)

    with serve_in_background(gui=True) as (port, _):
        status, _, body = _request(port, "/some/spa/route")
    assert status == 200
    assert paired_helper in body.decode("utf-8")


def test_non_index_static_files_are_not_substituted(paired_helper, tmp_path, monkeypatch):
    """A CSS file containing the literal sentinel must NOT be substituted —
    only index.html and the SPA fallback go through the substitution path."""
    from bsky_saves import _gui_serve
    gui_root = tmp_path / "_gui"
    gui_root.mkdir()
    (gui_root / "index.html").write_text("<html></html>", encoding="utf-8")
    assets = gui_root / "assets"
    assets.mkdir()
    css_body = "/* token sentinel: __BSKY_SAVES_TOKEN__ */"
    (assets / "style.css").write_text(css_body, encoding="utf-8")
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui_root)

    with serve_in_background(gui=True) as (port, _):
        status, _, body = _request(port, "/assets/style.css")
    assert status == 200
    assert body.decode("utf-8") == css_body
```

- [ ] **Step 4.2: Run tests, verify they fail**

```bash
/tmp/venv/bin/pytest tests/test_serve.py -v -k "substitut"
```

Expected: index/SPA tests fail with `'__BSKY_SAVES_TOKEN__' in text` (no substitution yet). Non-index test passes spuriously (no substitution yet means nothing to substitute).

- [ ] **Step 4.3: Implement substitution in `_gui_serve.py`**

In `src/bsky_saves/_gui_serve.py`, modify `_send_file` to accept an `is_spa_fallback` (already there) and `rel_path` (already there) and apply substitution. Find:

```python
def _send_file(
    handler,
    path: Path,
    *,
    rel_path: str,
    is_spa_fallback: bool = False,
) -> None:
    body = path.read_bytes()
    handler.send_response(200)
```

Replace with:

```python
_TOKEN_PLACEHOLDER = b"__BSKY_SAVES_TOKEN__"


def _send_file(
    handler,
    path: Path,
    *,
    rel_path: str,
    is_spa_fallback: bool = False,
) -> None:
    body = path.read_bytes()
    # Substitute the session-token placeholder in index.html (root or SPA
    # fallback). Other files served verbatim. Idempotent: if the placeholder
    # is absent (older GUI bundle), body is unchanged.
    if rel_path == "index.html" or is_spa_fallback:
        if _TOKEN_PLACEHOLDER in body:
            from ._io import read_or_create_token
            body = body.replace(_TOKEN_PLACEHOLDER, read_or_create_token().encode("ascii"))
    handler.send_response(200)
```

(The rest of `_send_file` stays exactly as it was; only the body is mutated.)

- [ ] **Step 4.4: Run tests, verify pass**

```bash
/tmp/venv/bin/pytest tests/test_serve.py -v -k "substitut"
```

Expected: 3 passed.

- [ ] **Step 4.5: Run full suite, verify green**

```bash
/tmp/venv/bin/pytest tests/ -q
```

Expected: all green.

- [ ] **Step 4.6: Commit**

```bash
git add src/bsky_saves/_gui_serve.py tests/test_serve.py
git commit -m "feat(_gui_serve): substitute session-token placeholder in served index.html

When _gui_serve serves index.html (root or SPA fallback), replace
the literal sentinel __BSKY_SAVES_TOKEN__ with the current session
token. The bsky-saves-gui bundle ships a <meta name=\"bsky-saves-token\"
content=\"__BSKY_SAVES_TOKEN__\"> tag in <head>; substitution is per-
request so token rotation is reflected on the next page load.

Static assets (CSS, JS, images, fonts) served byte-for-byte — only
index.html and SPA fallbacks go through the substitution path.
Idempotent: if the placeholder is absent (older GUI bundle), body
is unchanged.

Spec: docs/superpowers/specs/2026-05-16-bsky-saves-v0.6.2-session-token.md §7."
```

---

## Task 5: Version bump + documentation

**Files:**
- Modify: `pyproject.toml`
- Modify: `docs/protocol-versioning.md`
- Modify: `README.md`

- [ ] **Step 5.1: Bump version in `pyproject.toml`**

Change line 7:

```toml
version = "0.6.2"
```

- [ ] **Step 5.2: Add Changelog subsection to `docs/protocol-versioning.md`**

Append before the existing "## Cross-repo coupling" section:

```markdown
## Changelog

- `"1"` — `bsky-saves` v0.6.1. Initial value when `protocol` was added to `/ping`.
- `"2"` — `bsky-saves` v0.6.2. `Authorization: Bearer <token>` now required on
  all credentialed endpoints (`/fetch`, `/fetch-image`, `/extract-article`,
  `/enrich`, `/hydrate-threads`). `/ping` and OPTIONS preflight remain unauth.
  See `docs/superpowers/specs/2026-05-16-bsky-saves-v0.6.2-session-token.md`.
```

Also update the "## Current value" section's line to read:

```markdown
`protocol = "2"` — current as of `bsky-saves` v0.6.2.
```

- [ ] **Step 5.3: Add `### Pairing` subsection to `README.md`**

Insert under `## bsky-saves serve` (around the "endpoints" table), after the existing prose about Host/Origin validation:

```markdown
### Pairing

Since v0.6.2 the helper requires a session token on every API request
(except `GET /ping`, which stays unauth so the GUI can probe whether
the helper is running before pairing). The token lives at:

- Linux / *BSD: `$XDG_CONFIG_HOME/bsky-saves/token` (defaulting to `~/.config/bsky-saves/token`)
- macOS: `~/Library/Application Support/bsky-saves/token`
- Windows: `%APPDATA%\bsky-saves\token`

It is generated lazily on the first `bsky-saves serve` (or the first
`bsky-saves token`) and persisted across daemon restarts and bsky-saves
upgrades. File perms are `0600`.

The bundled GUI (`bsky-saves serve --gui`) reads the token from a
`<meta name="bsky-saves-token">` tag in the served `index.html` — no
user action is needed for the bundled flow.

For the hosted GUI at `https://saves.lightseed.net`, the SPA prompts
for the token on first connect. Run:

```
bsky-saves token
```

to print the current token, then paste it into the SPA's pairing
modal. To regenerate (invalidating any paired session — useful if you
suspect the token leaked):

```
bsky-saves token --rotate
```
```

Also update the `## Upgrade` section, append one line:

```markdown
**v0.6.x → v0.6.2:** the GUI will prompt for a one-time pairing the first
time it connects to the upgraded helper. See [Pairing](#pairing).
```

- [ ] **Step 5.4: Run full suite for sanity**

```bash
/tmp/venv/bin/pytest tests/ -q
```

Expected: all green (no test changes; this is just docs + version).

- [ ] **Step 5.5: Commit**

```bash
git add pyproject.toml docs/protocol-versioning.md README.md
git commit -m "release(v0.6.2): version bump, protocol changelog, README pairing docs

Bumps package to 0.6.2. Documents the session-token pairing model
in README under '### Pairing' and adds an upgrade note pointing v0.6.x
users at the one-time pairing step. Updates docs/protocol-versioning.md
with the protocol \"1\" → \"2\" changelog entry."
```

---

## Task 6: Final verification

**Files:** none modified; this task is a verification gate before pushing.

- [ ] **Step 6.1: Run full test suite**

```bash
/tmp/venv/bin/pytest tests/ -q
```

Expected: 365+ passed, 0 failed.

- [ ] **Step 6.2: Build a wheel locally and verify the bundled-GUI flow**

```bash
/tmp/venv/bin/pip install -q build
/tmp/venv/bin/python -m build --wheel
ls dist/bsky_saves-0.6.2-*.whl
```

Expected: wheel built without errors.

- [ ] **Step 6.3: Install in a fresh venv and exercise `token` + `serve`**

```bash
python3.11 -m venv /tmp/smoke && /tmp/smoke/bin/pip install -q dist/bsky_saves-0.6.2-*.whl
/tmp/smoke/bin/bsky-saves token
# Expect: a base64url string printed to stdout. File created at user config dir.

T=$(/tmp/smoke/bin/bsky-saves token)
/tmp/smoke/bin/bsky-saves serve --port 47826 &
SERVE_PID=$!
sleep 1

# /ping unauth still works
curl -fsS -H "Origin: http://127.0.0.1:47826" http://127.0.0.1:47826/ping | python3 -c '
import sys, json; d = json.load(sys.stdin); assert d["protocol"] == "2", d; print("ping ok, protocol=2")'

# Credentialed endpoint without auth → 401
STATUS=$(curl -sS -o /dev/null -w "%{http_code}" -X POST \
    -H "Origin: http://127.0.0.1:47826" \
    -H "Content-Type: application/json" \
    -d '{"url":"https://cdn.bsky.app/foo"}' \
    http://127.0.0.1:47826/fetch-image)
test "$STATUS" = "401" || { echo "expected 401, got $STATUS"; kill $SERVE_PID; exit 1; }
echo "no-auth 401 ok"

# Credentialed endpoint with token → not 401 (probably 502 since the URL is fake)
STATUS=$(curl -sS -o /dev/null -w "%{http_code}" -X POST \
    -H "Origin: http://127.0.0.1:47826" \
    -H "Authorization: Bearer $T" \
    -H "Content-Type: application/json" \
    -d '{"url":"https://cdn.bsky.app/foo"}' \
    http://127.0.0.1:47826/fetch-image)
test "$STATUS" != "401" || { echo "auth rejected, got $STATUS"; kill $SERVE_PID; exit 1; }
echo "auth not-401 ok"

kill $SERVE_PID
```

Expected: all four checks pass.

- [ ] **Step 6.4: Sanity-check that `~/.config/bsky-saves/token` was created with 0o600 perms (or platform equivalent)**

```bash
ls -la ~/.config/bsky-saves/token
# Expect: -rw-------  (or whatever your platform's token path)
```

- [ ] **Step 6.5: Push to `claude/installer-prep`**

```bash
git push -u origin claude/installer-prep
```

Pushes the 5 task commits to PR #9. The PR now carries the full v0.6.2 implementation on top of the v0.6.1 + receiver-workflow content.

---

## Self-Review

**Spec coverage check** (per spec §2, §4–§9):

- §4 token storage (config_dir, lazy-create, atomic-write, 0o600) → Task 1 ✓
- §5 Authorization: Bearer enforcement + /ping exempt + OPTIONS exempt + static-file exempt → Task 3 ✓
- §6 `bsky-saves token` + `--rotate` → Task 2 ✓
- §7 placeholder substitution in index.html + SPA fallback → Task 4 ✓
- §8 tests (paired_helper fixture, 401 missing, 401 wrong, /ping exempt, OPTIONS exempt, substitution behavior, rotate-invalidates) → Tasks 1–4 ✓
- §9 protocol bump to "2" + protocol-versioning.md changelog → Tasks 3, 5 ✓
- §10 CSP unchanged ✓ (no code change needed)
- §11 backward compatibility / migration → README upgrade note in Task 5 ✓
- §12 `bsky-saves token` programmatic-consumer example in README → Task 5 ✓ (covered in Pairing subsection)
- §13 sequencing → reflected in task ordering ✓

**Placeholder scan:** none — every step has concrete code or commands.

**Type / signature consistency:**
- `config_dir()` returns `Path`; used as `Path` in `read_or_create_token` and `paired_helper` fixture ✓
- `read_or_create_token()` returns `str`; consumed as `str` in cli.py and `_check_token` ✓
- `EXEMPT_ROUTES` is `frozenset[tuple[str, str]]`; membership tested with `(method, path)` tuples ✓
- `_TOKEN_PLACEHOLDER` is `bytes`; used in `body.replace(bytes, bytes)` ✓

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-16-bsky-saves-v0.6.2-session-token.md`.
