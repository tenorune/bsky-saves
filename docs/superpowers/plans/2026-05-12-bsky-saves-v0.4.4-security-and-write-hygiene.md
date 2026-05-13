# bsky-saves v0.4.4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tighten the daemon's security posture (Host/Origin enforcement, SSRF guard, body cap, response headers, verbose-log sanitization), close two latent code-hygiene items (atomic-write helper extraction, `__version__`-derived User-Agent), and pin a XXE-safe trafilatura floor — shipping as PyPI release `bsky-saves==0.4.4`.

**Architecture:** Two new private helper modules: `src/bsky_saves/_io.py` (single atomic-write function shared by every inventory-writing callsite) and `src/bsky_saves/_net.py` (SSRF guard + redirect-walking httpx wrapper used by every user-URL outbound fetch). `serve.py` gains a `_security_gate` method called before route dispatch on every request, plus body-size and response-header changes that flow through its existing `_send_json` / `_send_bytes` / `_read_json_body` plumbing. No new endpoints; no new CLI subcommands; no new inventory fields.

**Tech Stack:** Python 3.x, `httpx`, `trafilatura`, `respx` (test mocks), `pytest`, `hatchling` (build backend). All sync I/O for Pyodide compatibility.

**Spec:** `docs/superpowers/specs/2026-05-12-bsky-saves-v0.4.4-security-and-write-hygiene.md`

**Test invocation:** Always `python -m pytest` from the repo root — never bare `pytest`. The system-`pytest` uses a different Python that does not have `bsky-saves` installed (per the v0.4.3 handoff).

---

## File map

**New files:**
- `src/bsky_saves/_io.py` — atomic inventory write helper.
- `src/bsky_saves/_net.py` — SSRF guard + safe HTTP get helper.
- `tests/test_io.py` — unit tests for `_io`.
- `tests/test_net.py` — unit tests for `_net`.

**Modified files:**
- `src/bsky_saves/serve.py` — security gate, body cap, response headers, SSRF on `pds` field, `/fetch-image` redirect handling, verbose-log sanitization.
- `src/bsky_saves/articles.py` — switch to `safe_http_get`, derive UA from `__version__`, route writes through `atomic_write_inventory`.
- `src/bsky_saves/images.py` — switch to `safe_http_get`, derive UA from `__version__`, route writes through `atomic_write_inventory` (drive-by upgrade `os.rename` → `os.replace`).
- `src/bsky_saves/threads.py` — delete local `_atomic_write_inventory`, import from `_io`.
- `src/bsky_saves/fetch.py` — route inventory write through `atomic_write_inventory`.
- `src/bsky_saves/enrich.py` — route inventory write through `atomic_write_inventory`.
- `src/bsky_saves/cli.py` — `--allow-origin` help text update.
- `pyproject.toml` — bump version to `0.4.4`, pin minimum `trafilatura` version.
- `tests/test_serve.py` — flip three assertions; add ~20 new tests.
- `tests/test_articles.py`, `tests/test_images.py` — add SSRF rejection tests.

---

## Phase 1 — Atomic-write foundation

### Task 1: Create `_io.py` with `atomic_write_inventory`

**Files:**
- Create: `src/bsky_saves/_io.py`
- Test: `tests/test_io.py`

**Context:** Two modules currently have inventory atomic-write logic — `threads.py` (added in v0.4.2, uses `os.replace`) and `images.py` (uses inferior `os.rename`). Three other modules (`fetch.py`, `enrich.py`, `articles.py`) use direct `write_text`. Extract one canonical helper so all five callsites share it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_io.py`:

```python
"""Unit tests for bsky_saves._io.atomic_write_inventory."""
from __future__ import annotations

import json

from bsky_saves._io import atomic_write_inventory


def test_atomic_write_inventory_writes_expected_content(tmp_path):
    target = tmp_path / "inv.json"
    inventory = {"saves": [{"uri": "at://example", "saved_at": "2026-05-12T00:00:00Z"}]}

    atomic_write_inventory(target, inventory)

    written = target.read_text(encoding="utf-8")
    # Trailing newline is part of the contract.
    assert written.endswith("\n")
    assert json.loads(written) == inventory
    # JSON is formatted (indented), sort_keys=True.
    assert "  " in written  # indent
    # Keys are sorted: "saves" is the only top-level key, so check inside.
    save = json.loads(written)["saves"][0]
    keys = list(save.keys())
    assert keys == sorted(keys)


def test_atomic_write_inventory_leaves_no_tmp_sidecar(tmp_path):
    target = tmp_path / "inv.json"
    atomic_write_inventory(target, {"saves": []})

    sidecar = target.with_suffix(target.suffix + ".tmp")
    assert not sidecar.exists()


def test_atomic_write_inventory_overwrites_existing(tmp_path):
    target = tmp_path / "inv.json"
    target.write_text('{"saves": [{"uri": "old"}]}\n', encoding="utf-8")

    atomic_write_inventory(target, {"saves": [{"uri": "new"}]})

    assert json.loads(target.read_text(encoding="utf-8"))["saves"][0]["uri"] == "new"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_io.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bsky_saves._io'`.

- [ ] **Step 3: Implement `_io.py`**

Create `src/bsky_saves/_io.py`:

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_io.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/_io.py tests/test_io.py
git commit -m "feat(_io): extract atomic_write_inventory helper"
```

---

### Task 2: Migrate `threads.py` to use `_io`

**Files:**
- Modify: `src/bsky_saves/threads.py` — delete `_atomic_write_inventory` (lines 57-69), import from `_io`, update two callsites (around lines 269 and 278).

**Context:** `threads.py` has its own `_atomic_write_inventory` with identical semantics to the new shared helper. Replace it with a re-export delegate or just import the new helper directly.

- [ ] **Step 1: Read current `threads.py` callsites**

Run: `grep -n "_atomic_write_inventory\|^def _atomic\|^import os\|^import json" src/bsky_saves/threads.py`
Expected output: lines around 57 (definition), 269 and 278 (callsites), and the `os`/`json` imports near the top.

- [ ] **Step 2: Delete the local helper and update imports**

In `src/bsky_saves/threads.py`:

Add to the existing imports block (alphabetical with the other `from .` imports):
```python
from ._io import atomic_write_inventory
```

Delete lines 57-69 (the `def _atomic_write_inventory` definition and its docstring).

If `import os` and `import json` are no longer used elsewhere in the file after this deletion, leave them — `threads.py` likely still uses both. Verify by running `grep -n '^import os\|^import json\|os\.\|json\.' src/bsky_saves/threads.py` after the deletion.

- [ ] **Step 3: Update callsites**

Replace both occurrences of `_atomic_write_inventory(inventory_path, inv)` with `atomic_write_inventory(inventory_path, inv)`. There should be exactly two — one in the `finally` block (per-iteration flush) and one at end-of-run.

- [ ] **Step 4: Run threads tests to verify no regression**

Run: `python -m pytest tests/test_threads.py -v`
Expected: ALL PASS (22 tests, all the same that passed before).

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/threads.py
git commit -m "refactor(threads): use shared atomic_write_inventory helper"
```

---

### Task 3: Migrate `images.py` to use `_io` (drive-by os.rename → os.replace)

**Files:**
- Modify: `src/bsky_saves/images.py` — replace inline temp+rename block (lines 158-164), drop `import os` if unused.

**Context:** `images.py` has its own inline temp-file + `os.rename` block at lines 158-164. `os.rename` has Windows quirks (fails if destination exists); `os.replace` in the new helper fixes that.

- [ ] **Step 1: Inspect current write block**

Run: `sed -n '155,168p' src/bsky_saves/images.py`
Expected: the `if changed:` block ending with `os.rename(tmp_path, inventory_path)`.

- [ ] **Step 2: Replace the write block**

In `src/bsky_saves/images.py`:

Add to the imports block:
```python
from ._io import atomic_write_inventory
```

Replace this block (around lines 157-164):
```python
    if changed:
        inv["fetched_at"] = _now_iso()
        tmp_path = inventory_path.with_suffix(inventory_path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(inv, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.rename(tmp_path, inventory_path)
```

With:
```python
    if changed:
        inv["fetched_at"] = _now_iso()
        atomic_write_inventory(inventory_path, inv)
```

If `import os` is no longer needed (check with `grep -n 'os\.' src/bsky_saves/images.py`), delete the `import os` line. If `import json` is still used (e.g. for `json.loads` when reading the inventory), leave it.

- [ ] **Step 3: Run image tests to verify no regression**

Run: `python -m pytest tests/test_images.py -v`
Expected: ALL PASS.

- [ ] **Step 4: Commit**

```bash
git add src/bsky_saves/images.py
git commit -m "refactor(images): use shared atomic_write_inventory helper (os.replace upgrade)"
```

---

### Task 4: Migrate `fetch.py`, `enrich.py`, `articles.py` to use `_io`

**Files:**
- Modify: `src/bsky_saves/fetch.py` — write at ~line 345.
- Modify: `src/bsky_saves/enrich.py` — write at ~line 84.
- Modify: `src/bsky_saves/articles.py` — write at ~line 194.

**Context:** Three mechanical replacements. Each module currently does `inventory_path.write_text(json.dumps(inv, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")` directly. After the migration, each goes through `atomic_write_inventory`.

- [ ] **Step 1: Find the exact lines in each module**

Run: `grep -n "inventory_path.write_text\|inv_path.write_text" src/bsky_saves/fetch.py src/bsky_saves/enrich.py src/bsky_saves/articles.py`

Expected: one match per file.

- [ ] **Step 2: Update `fetch.py`**

Add import: `from ._io import atomic_write_inventory` to the existing imports block.

Replace the `inventory_path.write_text(...)` call (around line 345) with:
```python
    atomic_write_inventory(inventory_path, inv)
```

The full surrounding context (after edit) should look like:
```python
    inv["fetched_at"] = _now_iso()
    atomic_write_inventory(inventory_path, inv)
```

- [ ] **Step 3: Update `enrich.py`**

Same pattern. Add import, replace the `inventory_path.write_text(...)` call (around line 84) with `atomic_write_inventory(inventory_path, inv)`.

- [ ] **Step 4: Update `articles.py`**

Same pattern. Add import, replace the call (around line 194) with `atomic_write_inventory(inventory_path, inv)`.

- [ ] **Step 5: Run all relevant tests**

Run: `python -m pytest tests/test_fetch.py tests/test_enrich.py tests/test_articles.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Run full suite as sanity check**

Run: `python -m pytest -q`
Expected: All ~187 existing tests PASS (we've added 3 new tests in Task 1).

- [ ] **Step 7: Commit**

```bash
git add src/bsky_saves/fetch.py src/bsky_saves/enrich.py src/bsky_saves/articles.py
git commit -m "refactor: route fetch/enrich/articles writes through atomic_write_inventory"
```

---

## Phase 2 — SSRF guard foundation

### Task 5: Create `_net.py` with `assert_public_http_url`

**Files:**
- Create: `src/bsky_saves/_net.py`
- Test: `tests/test_net.py`

**Context:** This is the core SSRF guard used by every user-URL fetch (`/extract-article`, `articles._extract_article` CLI path, `images.download_to` CLI path, `/fetch-image` per-redirect-hop, `/fetch` and `/hydrate-threads` `pds` field).

The guard parses the URL, validates the scheme, normalises the hostname, and:
- If hostname is an IP literal: check `ipaddress.ip_address(host)` against `is_private`, `is_loopback`, `is_link_local`, `is_multicast`, `is_reserved`, `is_unspecified`, and explicit CGNAT range `100.64.0.0/10`.
- If hostname is a DNS name: `socket.getaddrinfo(host, None)` and apply the same checks to every returned address.
- IPv4-mapped IPv6 addresses (`::ffff:x.x.x.x`) get unwrapped and re-checked.

Python's `ipaddress.ip_address` does not accept obscure IPv4 notations like `0177.0.0.1` (octal) or `2130706433` (decimal-as-int-string); those flow through `socket.getaddrinfo` and get resolved into canonical IPv4Address values, which then fail the IP checks. We rely on this behavior — tests verify it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_net.py`:

```python
"""Unit tests for bsky_saves._net.assert_public_http_url."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from bsky_saves._net import UnsafeURLError, assert_public_http_url


# Happy path

def test_public_https_url_passes():
    # example.com resolves to public IPs; no exception.
    assert_public_http_url("https://example.com/path")


def test_public_https_url_with_port_passes():
    assert_public_http_url("https://example.com:8443/x")


# IP-literal rejections — IPv4

def test_ipv4_loopback_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://127.0.0.1/x")


def test_ipv4_private_10_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://10.0.0.1/x")


def test_ipv4_private_192_168_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://192.168.0.1/x")


def test_ipv4_private_172_16_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://172.16.0.1/x")


def test_ipv4_link_local_metadata_rejected():
    """169.254.169.254 is the AWS/GCP/Azure metadata IP."""
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://169.254.169.254/latest/meta-data/")


def test_ipv4_cgnat_rejected():
    """100.64.0.0/10 is RFC 6598 CGNAT; not is_private in older Python."""
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://100.64.0.1/x")


def test_ipv4_unspecified_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://0.0.0.0/x")


# IP-literal rejections — IPv6

def test_ipv6_loopback_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://[::1]/x")


def test_ipv6_link_local_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://[fe80::1]/x")


def test_ipv6_mapped_ipv4_loopback_rejected():
    """::ffff:127.0.0.1 wraps an IPv4 loopback in IPv6; must reject."""
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://[::ffff:127.0.0.1]/x")


# Hostname rejections

def test_localhost_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://localhost/x")


def test_localhost_trailing_dot_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://localhost./x")


def test_dns_alias_resolving_to_loopback_rejected():
    """A hostname that getaddrinfo says points at 127.0.0.1 must reject."""
    with patch("bsky_saves._net.socket.getaddrinfo") as gai:
        # getaddrinfo returns list of 5-tuples; we only care about index 4 (sockaddr).
        gai.return_value = [
            (0, 0, 0, "", ("127.0.0.1", 0)),
        ]
        with pytest.raises(UnsafeURLError):
            assert_public_http_url("https://malicious.example/x")


def test_dns_alias_resolving_to_metadata_rejected():
    """metadata.google.internal-style alias rejection."""
    with patch("bsky_saves._net.socket.getaddrinfo") as gai:
        gai.return_value = [
            (0, 0, 0, "", ("169.254.169.254", 0)),
        ]
        with pytest.raises(UnsafeURLError):
            assert_public_http_url("https://metadata.google.internal/x")


# Scheme + format rejections

def test_http_rejected_when_allow_http_false():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("http://example.com/x", allow_http=False)


def test_http_allowed_when_allow_http_true():
    assert_public_http_url("http://example.com/x", allow_http=True)


def test_ftp_always_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("ftp://example.com/x", allow_http=True)


def test_javascript_scheme_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("javascript:alert(1)", allow_http=True)


def test_empty_url_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("", allow_http=True)


def test_url_without_hostname_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https:///path", allow_http=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_net.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bsky_saves._net'`.

- [ ] **Step 3: Implement `_net.py`**

Create `src/bsky_saves/_net.py`:

```python
"""Network safety helpers for outbound user-URL fetches.

Centralises SSRF defence: every user-supplied URL flows through
``assert_public_http_url`` before httpx ever sees it. ``safe_http_get`` wraps
``httpx.get`` to walk redirects manually with the guard re-applied per hop.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    """Raised when a URL targets a non-public IP or is otherwise unsafe."""


# RFC 6598 CGNAT — Python's IPv4Address.is_private doesn't include this in
# 3.11 and earlier. Explicit check below.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if the IP is in any range we refuse to fetch from."""
    # Unwrap IPv4-mapped IPv6 first so the checks below see the IPv4 form.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_private:
        return True
    if ip.is_loopback:
        return True
    if ip.is_link_local:
        return True
    if ip.is_multicast:
        return True
    if ip.is_reserved:
        return True
    if ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return True
    return False


def assert_public_http_url(url: str, *, allow_http: bool = False) -> None:
    """Raise UnsafeURLError if url is malformed, uses a disallowed scheme,
    or resolves to a private/loopback/link-local/multicast/reserved IP.

    Args:
        url: The URL to validate.
        allow_http: If True, permit ``http://`` URLs (used by /extract-article
            which targets the open web). If False, require ``https://``.
    """
    if not url or not isinstance(url, str):
        raise UnsafeURLError("empty or non-string URL")

    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise UnsafeURLError(f"unparseable URL: {e}")

    scheme = (parsed.scheme or "").lower()
    allowed = ("https",) if not allow_http else ("http", "https")
    if scheme not in allowed:
        raise UnsafeURLError(f"scheme not allowed: {scheme!r}")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURLError("URL has no hostname")

    if host == "localhost":
        raise UnsafeURLError("hostname 'localhost' not allowed")

    # IP literal? Check directly. Otherwise: DNS-resolve and check every address.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if _is_unsafe_ip(ip):
            raise UnsafeURLError(f"unsafe IP literal: {host}")
        return

    # DNS lookup. getaddrinfo returns a list of 5-tuples; index 4 is the
    # sockaddr (ip-string, port) for IPv4 or (ip-string, port, flow, scope) for IPv6.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeURLError(f"DNS resolution failed: {e}")

    for info in infos:
        sockaddr = info[4]
        try:
            resolved = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_unsafe_ip(resolved):
            raise UnsafeURLError(
                f"hostname {host!r} resolves to unsafe address {resolved}"
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_net.py -v`
Expected: 22 PASS.

If any test fails, fix the helper (don't relax the test).

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/_net.py tests/test_net.py
git commit -m "feat(_net): add SSRF guard helper assert_public_http_url"
```

---

### Task 6: Add `safe_http_get` to `_net.py`

**Files:**
- Modify: `src/bsky_saves/_net.py` — add `safe_http_get` and a redirect-budget error class.
- Modify: `tests/test_net.py` — add ~5 tests for the new function.

**Context:** `assert_public_http_url` only checks the URL given to it. httpx's default `follow_redirects=True` would happily walk into a `Location: http://127.0.0.1/x` from a compromised upstream. `safe_http_get` walks redirects manually, re-applying the guard (and a caller-provided allowlist callback if given) to every hop.

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_net.py`:

```python
import httpx
import respx

from bsky_saves._net import TooManyRedirectsError, safe_http_get


@respx.mock
def test_safe_http_get_happy_path_returns_response():
    route = respx.get("https://example.com/x").mock(
        return_value=httpx.Response(200, content=b"ok")
    )
    r = safe_http_get("https://example.com/x")
    assert r.status_code == 200
    assert r.content == b"ok"
    assert route.called


def test_safe_http_get_rejects_unsafe_initial_url():
    with pytest.raises(UnsafeURLError):
        safe_http_get("https://127.0.0.1/x")


@respx.mock
def test_safe_http_get_follows_safe_redirect():
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/b"})
    )
    respx.get("https://example.com/b").mock(
        return_value=httpx.Response(200, content=b"final")
    )
    r = safe_http_get("https://example.com/a")
    assert r.status_code == 200
    assert r.content == b"final"


@respx.mock
def test_safe_http_get_rejects_redirect_to_private_ip():
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://127.0.0.1/b"})
    )
    with pytest.raises(UnsafeURLError):
        safe_http_get("https://example.com/a")


@respx.mock
def test_safe_http_get_hop_check_runs_on_each_target():
    """hop_check raises -> safe_http_get propagates the exception."""
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://other.com/b"})
    )

    def reject_other(url: str) -> None:
        if "other.com" in url:
            raise UnsafeURLError("not allowed by hop_check")

    with pytest.raises(UnsafeURLError):
        safe_http_get("https://example.com/a", hop_check=reject_other)


@respx.mock
def test_safe_http_get_too_many_redirects():
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/b"})
    )
    respx.get("https://example.com/b").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/c"})
    )
    respx.get("https://example.com/c").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/d"})
    )
    with pytest.raises(TooManyRedirectsError):
        safe_http_get("https://example.com/a", max_redirects=2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_net.py -v`
Expected: 22 PASS, 6 FAIL on `cannot import name 'TooManyRedirectsError'` or `'safe_http_get'`.

- [ ] **Step 3: Implement `safe_http_get`**

Append to `src/bsky_saves/_net.py`:

```python
from typing import Callable
from urllib.parse import urljoin

import httpx


class TooManyRedirectsError(Exception):
    """safe_http_get exceeded its redirect budget."""


def safe_http_get(
    url: str,
    *,
    allow_http: bool = False,
    max_redirects: int = 5,
    hop_check: Callable[[str], None] | None = None,
    **httpx_kwargs,
) -> httpx.Response:
    """Like httpx.get, but walks redirects manually with assert_public_http_url
    re-applied per hop. ``hop_check`` runs before the SSRF check on each hop
    (used to enforce per-endpoint allowlists in addition to the SSRF guard).
    Disables httpx's own redirect-following.

    Raises:
        UnsafeURLError: any hop fails ``hop_check`` or the SSRF guard.
        TooManyRedirectsError: more than ``max_redirects`` 3xx responses chained.
    """
    httpx_kwargs.pop("follow_redirects", None)  # we follow manually
    current = url
    for _ in range(max_redirects + 1):
        if hop_check is not None:
            hop_check(current)
        assert_public_http_url(current, allow_http=allow_http)
        r = httpx.get(current, follow_redirects=False, **httpx_kwargs)
        if 300 <= r.status_code < 400 and "location" in (h.lower() for h in r.headers):
            location = r.headers.get("Location") or r.headers.get("location")
            if not location:
                return r
            current = urljoin(current, location)
            continue
        return r
    raise TooManyRedirectsError(f"exceeded {max_redirects} redirects starting from {url!r}")
```

Note: the `httpx` import is added near the top of the file alongside the existing imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_net.py -v`
Expected: 28 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/_net.py tests/test_net.py
git commit -m "feat(_net): add safe_http_get redirect-walking helper"
```

---

## Phase 3 — Apply SSRF guard at outbound callsites

### Task 7: Apply `safe_http_get` in `articles._extract_article` (F1, F10)

**Files:**
- Modify: `src/bsky_saves/articles.py` — replace `httpx.get` with `safe_http_get`.
- Modify: `tests/test_articles.py` — add SSRF rejection tests.

**Context:** `_extract_article` is shared between the HTTP endpoint (`serve._handle_extract_article`) and the CLI path (`hydrate articles`). Replacing one `httpx.get` call closes both F1 and F10. The spec allows `http://` for article URLs (open-web policy in MVP spec §5.3); use `allow_http=True`.

- [ ] **Step 1: Add failing tests for the CLI path**

Append to `tests/test_articles.py`:

```python
def test_extract_article_rejects_loopback_url():
    """SSRF guard rejects http://127.0.0.1 before any HTTP call is made."""
    from bsky_saves.articles import _extract_article

    extraction, error = _extract_article("http://127.0.0.1/secret")
    assert extraction is None
    assert error is not None
    assert "UnsafeURLError" in error or "fetch_error" in error


def test_extract_article_rejects_metadata_ip():
    """AWS-style metadata IP must be blocked."""
    from bsky_saves.articles import _extract_article

    extraction, error = _extract_article(
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
    )
    assert extraction is None
    assert error is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_articles.py::test_extract_article_rejects_loopback_url tests/test_articles.py::test_extract_article_rejects_metadata_ip -v`
Expected: FAIL or unexpectedly PASS depending on local DNS — most likely they pass because the network is unreachable, but the assertion path may differ. Either way, after the next step, both tests pass via the SSRF guard.

- [ ] **Step 3: Update `articles.py` to use `safe_http_get`**

In `src/bsky_saves/articles.py`:

Add import:
```python
from ._net import UnsafeURLError, safe_http_get
```

Replace the `httpx.get(...)` call in `_extract_article` (around lines 67-75):

```python
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.8"},
            follow_redirects=True,
            timeout=TIMEOUT,
        )
    except Exception as e:
        return None, f"fetch_error:{type(e).__name__}:{str(e)[:120]}"
```

With:
```python
    try:
        r = safe_http_get(
            url,
            allow_http=True,
            max_redirects=5,
            headers={"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.8"},
            timeout=TIMEOUT,
        )
    except UnsafeURLError as e:
        return None, f"fetch_error:UnsafeURLError:{str(e)[:120]}"
    except Exception as e:
        return None, f"fetch_error:{type(e).__name__}:{str(e)[:120]}"
```

If `httpx` is no longer used elsewhere in `articles.py`, the `import httpx` line can stay — it's a tiny cost and may be used in future. Don't optimise.

- [ ] **Step 4: Run all article tests to verify**

Run: `python -m pytest tests/test_articles.py -v`
Expected: ALL PASS (existing + 2 new).

- [ ] **Step 5: Run full suite**

Run: `python -m pytest -q`
Expected: All previously-passing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/bsky_saves/articles.py tests/test_articles.py
git commit -m "feat(articles): block private/loopback URLs via SSRF guard (F1, F10)"
```

---

### Task 8: Apply `safe_http_get` in `images.download_to` (F12)

**Files:**
- Modify: `src/bsky_saves/images.py` — replace `httpx.get` in `download_to`.
- Modify: `tests/test_images.py` — add SSRF rejection tests.

**Context:** `download_to` is the CLI image-hydration HTTP call. The HTTP endpoint `/fetch-image` will get its own treatment in Task 17 (via `safe_http_get` with `hop_check`). Bsky CDN URLs are HTTPS in practice, so use `allow_http=False`.

- [ ] **Step 1: Add failing tests**

Append to `tests/test_images.py`:

```python
def test_download_to_rejects_loopback_url(tmp_path):
    from bsky_saves.images import download_to
    from bsky_saves._net import UnsafeURLError

    dest = tmp_path / "img-x.jpg"
    with pytest.raises(UnsafeURLError):
        download_to("https://127.0.0.1/x.jpg", dest)


def test_download_to_rejects_metadata_ip(tmp_path):
    from bsky_saves.images import download_to
    from bsky_saves._net import UnsafeURLError

    dest = tmp_path / "img-x.jpg"
    with pytest.raises(UnsafeURLError):
        download_to("https://169.254.169.254/img", dest)
```

If `pytest` isn't already imported at the top of `tests/test_images.py`, add `import pytest`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_images.py::test_download_to_rejects_loopback_url tests/test_images.py::test_download_to_rejects_metadata_ip -v`
Expected: FAIL with `ImportError` or `Failed: DID NOT RAISE`.

- [ ] **Step 3: Update `images.py`**

In `src/bsky_saves/images.py`:

Add import:
```python
from ._net import safe_http_get
```

Replace the `httpx.get(...)` call inside `download_to` (around lines 75-81):

```python
def download_to(url: str, dest: Path, *, user_agent: str = DEFAULT_USER_AGENT) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = httpx.get(
        url,
        headers={"User-Agent": user_agent, "Accept": "image/*"},
        follow_redirects=True,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    dest.write_bytes(r.content)
```

With:
```python
def download_to(url: str, dest: Path, *, user_agent: str = DEFAULT_USER_AGENT) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = safe_http_get(
        url,
        allow_http=False,
        max_redirects=3,
        headers={"User-Agent": user_agent, "Accept": "image/*"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    dest.write_bytes(r.content)
```

- [ ] **Step 4: Run all image tests**

Run: `python -m pytest tests/test_images.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/images.py tests/test_images.py
git commit -m "feat(images): block private/loopback URLs via SSRF guard (F12)"
```

---

## Phase 4 — User-Agent bump

### Task 9: Derive `User-Agent` from `__version__`

**Files:**
- Modify: `src/bsky_saves/images.py`
- Modify: `src/bsky_saves/articles.py`

**Context:** Both modules have stale UA strings hardcoded at old versions (`bsky-saves/0.1` and `bsky-saves/0.2`). Derive from `__version__` (which is `importlib.metadata`-sourced in `__init__.py`) so they never go stale.

- [ ] **Step 1: Update `images.py`**

Replace lines 61-63 (current `DEFAULT_USER_AGENT` assignment):
```python
DEFAULT_USER_AGENT = (
    "bsky-saves/0.2 (+https://github.com/tenorune/bsky-saves)"
)
```

With:
```python
from . import __version__

DEFAULT_USER_AGENT = f"bsky-saves/{__version__} (+https://github.com/tenorune/bsky-saves)"
```

Place the `from . import __version__` line with the other relative imports near the top (after `from ._io import atomic_write_inventory`).

- [ ] **Step 2: Update `articles.py`**

Replace lines 25-27 (current `DEFAULT_USER_AGENT` assignment):
```python
DEFAULT_USER_AGENT = (
    "bsky-saves/0.1 (+https://github.com/tenorune/bsky-saves)"
)
```

With:
```python
from . import __version__

DEFAULT_USER_AGENT = f"bsky-saves/{__version__} (+https://github.com/tenorune/bsky-saves)"
```

- [ ] **Step 3: Add a sanity check test**

Append to `tests/test_version.py` (or create one if it doesn't exist):

```python
def test_image_user_agent_contains_version():
    from bsky_saves import __version__
    from bsky_saves.images import DEFAULT_USER_AGENT
    assert __version__ in DEFAULT_USER_AGENT


def test_article_user_agent_contains_version():
    from bsky_saves import __version__
    from bsky_saves.articles import DEFAULT_USER_AGENT
    assert __version__ in DEFAULT_USER_AGENT
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_version.py tests/test_images.py tests/test_articles.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/images.py src/bsky_saves/articles.py tests/test_version.py
git commit -m "refactor: derive User-Agent from __version__"
```

---

## Phase 5 — Trafilatura pin

### Task 10: Verify trafilatura's lxml defaults; pin minimum version

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/bsky_saves/articles.py` (one-line comment)

**Context:** trafilatura uses lxml to parse HTML. We rely on its default parser being XXE-safe (no external-entity resolution). Verify this against the currently-installed version, then pin a minimum in `pyproject.toml`.

- [ ] **Step 1: Find currently-installed trafilatura version**

Run: `python -c "import trafilatura; print(trafilatura.__version__)"`
Record the version (e.g. `2.0.0`).

- [ ] **Step 2: Inspect trafilatura's parser configuration**

Run: `python -c "import trafilatura, inspect; print(inspect.getsourcefile(trafilatura))"`
Then explore the package:
```bash
TRAFILATURA_DIR="$(python -c "import trafilatura, os; print(os.path.dirname(trafilatura.__file__))")"
grep -rn "HTMLParser\|resolve_entities\|fromstring" "$TRAFILATURA_DIR" | head -20
```

Look for `etree.HTMLParser(...)` or `html.fromstring(...)` usages. lxml's `HTMLParser` (distinct from `XMLParser`) does not resolve external entities by default — confirm trafilatura uses `HTMLParser`, not `XMLParser`, for HTML inputs.

If you find that trafilatura uses `XMLParser` with `resolve_entities=True` anywhere in the HTML-extraction path, **stop and escalate to the user** — this changes v0.4.4 scope and may require pre-processing or a library switch.

If verification passes (trafilatura's HTML extraction is XXE-safe), continue.

- [ ] **Step 3: Pin minimum version in `pyproject.toml`**

Find the `trafilatura` line in `[project.dependencies]` (or wherever dependencies are declared). It currently has no lower bound, e.g.:

```toml
dependencies = [
    "httpx",
    "trafilatura",
]
```

Change to (substituting the installed version found in Step 1; example uses `2.0.0`):

```toml
dependencies = [
    "httpx",
    "trafilatura>=2.0.0",
]
```

Use the actual installed version, not the example.

- [ ] **Step 4: Add a comment in `articles.py`**

Near the `import trafilatura` line, add:

```python
# trafilatura's HTML extraction relies on lxml's HTMLParser default, which
# does not resolve external entities (XXE-safe). pyproject.toml pins a
# minimum version known to ship that default.
import trafilatura
```

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/bsky_saves/articles.py
git commit -m "chore(deps): pin minimum trafilatura version (XXE-safe defaults)"
```

---

## Phase 6 — Daemon security gate (Host)

### Task 11: Add `_security_gate` with Host header validation

**Files:**
- Modify: `src/bsky_saves/serve.py` — add `_security_gate` method, call from all `do_*` entrypoints.
- Modify: `tests/test_serve.py` — add Host validation tests.

**Context:** Today the daemon accepts any `Host` header. Spec §4.3 of the MVP spec requires `421 Misdirected Request` for anything other than `127.0.0.1:<port>` or `localhost:<port>`. This defends against DNS rebinding.

The `_security_gate(method)` method is called from `do_GET`, `do_POST`, `do_OPTIONS`, and the `__getattr__` unknown-verb fallback. It returns `True` to allow the request to proceed, or `False` after having already sent a rejection response. In Task 11 it only checks Host; Task 12 adds Origin.

The handler needs to know its bound port to validate the Host header. Pass `port` through `make_handler` so it's captured in the closure.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_serve.py` (near existing CORS / Origin tests):

```python
def test_host_loopback_with_correct_port_accepted():
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/ping", headers={"Host": f"127.0.0.1:{port}"}
        )
    assert status == 200


def test_host_localhost_with_correct_port_accepted():
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/ping", headers={"Host": f"localhost:{port}"}
        )
    assert status == 200


def test_host_unknown_domain_returns_421():
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/ping", headers={"Host": "evil.example.com"}
        )
    assert status == 421
    assert json.loads(body) == {"error": "misdirected request"}


def test_host_wrong_port_returns_421():
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/ping", headers={"Host": f"127.0.0.1:{port + 1}"}
        )
    assert status == 421


def test_host_ipv6_brackets_returns_421():
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/ping", headers={"Host": f"[::1]:{port}"}
        )
    assert status == 421


def test_host_trailing_dot_returns_421():
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/ping", headers={"Host": f"localhost.:{port}"}
        )
    assert status == 421
```

Note: the `_request` helper in `tests/test_serve.py` should accept a `headers` kwarg. Check the existing definition — if it doesn't, extend it. (The existing test `test_cors_allowed_origin_echoed_on_normal_response` already passes `headers={"Origin": ...}`, so the helper supports it.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_serve.py -v -k "host_"`
Expected: 4 FAIL (loopback and localhost host pass because the Python http server is permissive by default; the others currently return 200 instead of 421).

- [ ] **Step 3: Wire `port` into `make_handler`**

In `src/bsky_saves/serve.py`:

Change `make_handler` signature (around line 503) to add a `port` parameter:

```python
def make_handler(
    *,
    port: int,
    allow_origins: list[str],
    verbose: bool = False,
) -> type[BaseHTTPRequestHandler]:
```

In the function body, just below the existing `origins = list(allow_origins)` line, capture the port in the closure scope (already implicit via the parameter).

Inside the `Handler` class, add the `_security_gate` method (somewhere logically grouped — e.g. after `_read_json_body` and before `do_OPTIONS`):

```python
def _security_gate(self, method: str) -> bool:
    """Validate Host (and later Origin). Returns True if the request may
    proceed to _dispatch; returns False after sending a rejection response.
    Called from every do_* entrypoint before route handling."""
    if not self._check_host():
        return False
    return True

def _check_host(self) -> bool:
    """Reject DNS-rebinding: Host must equal 127.0.0.1:<port> or
    localhost:<port>. Anything else → 421 Misdirected Request."""
    host = self.headers.get("Host", "")
    expected = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if host not in expected:
        self._send_json_error(421, "misdirected request")
        return False
    return True
```

- [ ] **Step 4: Call the gate from every entrypoint**

Modify each `do_*` method to call `_security_gate` first:

```python
def do_OPTIONS(self) -> None:
    self._log_request()
    if not self._security_gate("OPTIONS"):
        return
    self.send_response(204)
    self._cors_headers()
    self.end_headers()

def do_GET(self) -> None:
    self._log_request()
    if not self._security_gate("GET"):
        return
    self._dispatch("GET")

def do_POST(self) -> None:
    self._log_request()
    if not self._security_gate("POST"):
        return
    self._dispatch("POST")

def __getattr__(self, name: str):
    if name.startswith("do_"):
        method = name[3:]

        def _unknown_verb():
            self._log_request()
            if not self._security_gate(method):
                return
            self._dispatch(method)

        return _unknown_verb
    raise AttributeError(name)
```

- [ ] **Step 5: Update `run_serve` to pass `port` to `make_handler`**

In `run_serve` (around line 624):

```python
handler_cls = make_handler(
    port=port, allow_origins=origins, verbose=verbose
)
```

- [ ] **Step 6: Run Host tests to verify they pass**

Run: `python -m pytest tests/test_serve.py -v -k "host_"`
Expected: 6 PASS.

- [ ] **Step 7: Run full serve suite to check for regressions**

Run: `python -m pytest tests/test_serve.py -v`
Expected: most tests pass. Some may fail because the existing tests don't set a Host header (Python's http.client sets one automatically — typically `127.0.0.1:<port>`). Verify by looking at any failures.

If existing tests fail because they sent a wrong Host, they should be updated (but in practice http.client populates Host correctly).

- [ ] **Step 8: Commit**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): validate Host header, reject non-loopback with 421"
```

---

## Phase 7 — Daemon security gate (Origin)

### Task 12: Origin enforcement → 403 (incl. OPTIONS preflight)

**Files:**
- Modify: `src/bsky_saves/serve.py` — extend `_security_gate` with Origin check.
- Modify: `tests/test_serve.py` — add tests, flip 3 existing assertions.

**Context:** Today, a disallowed Origin silently has its CORS header omitted (browser fails closed). Spec §4.4 requires explicit `403 Forbidden` with body `{"error":"Origin not allowed"}`, applied uniformly across every endpoint including `OPTIONS` preflight. Missing Origin (curl-style) remains permitted.

- [ ] **Step 1: Identify the three existing tests to flip**

Run: `grep -n "test_cors_disallowed_origin_omits_allow_origin_header\|test_allow_origin_override_replaces_default\|test_multiple_allow_origins_all_allowed" tests/test_serve.py`

Note the line numbers — these tests will be edited in this task.

- [ ] **Step 2: Write new Origin tests + flip the three existing ones**

In `tests/test_serve.py`, **replace** the existing `test_cors_disallowed_origin_omits_allow_origin_header` function with:

```python
def test_cors_disallowed_origin_returns_403():
    """Non-allowlisted origins receive an explicit 403, not just a missing
    Allow-Origin header."""
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port, "/ping", headers={"Origin": "https://evil.example"}
        )
    assert status == 403
    assert json.loads(body) == {"error": "Origin not allowed"}
```

**Replace** the existing `test_multiple_allow_origins_all_allowed` to update the line that asserted "third unlisted origin gets no header":

```python
def test_multiple_allow_origins_all_allowed():
    a = "https://a.example"
    b = "https://b.example"
    with serve_in_background(allow_origins=(a, b)) as (port, _):
        status_a, h_a, _ = _request(port, "/ping", headers={"Origin": a})
        status_b, h_b, _ = _request(port, "/ping", headers={"Origin": b})
        status_c, _, body_c = _request(
            port, "/ping", headers={"Origin": "https://c.example"}
        )
    assert status_a == 200
    assert h_a["Access-Control-Allow-Origin"] == a
    assert status_b == 200
    assert h_b["Access-Control-Allow-Origin"] == b
    assert status_c == 403
    assert json.loads(body_c) == {"error": "Origin not allowed"}
```

Leave `test_allow_origin_override_replaces_default` alone for now — it'll be replaced in Task 13 (where the behavior actually flips from replacement to additive).

Add these new tests below the existing CORS-related tests:

```python
def test_options_preflight_from_disallowed_origin_returns_403():
    """Spec §4.4: OPTIONS from disallowed origin also returns 403, not 204."""
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="OPTIONS",
            headers={"Origin": "https://evil.example"},
        )
    assert status == 403
    assert json.loads(body) == {"error": "Origin not allowed"}


def test_options_preflight_from_allowed_origin_returns_204():
    """Allowed origin still gets the 204 with echoed Allow-Origin."""
    with serve_in_background() as (port, _):
        status, headers, _ = _request(
            port,
            "/fetch-image",
            method="OPTIONS",
            headers={"Origin": DEFAULT_ORIGIN},
        )
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == DEFAULT_ORIGIN


def test_ping_origin_disallowed_returns_403():
    """Spec §5.1 (post-2026-05-12 revision): /ping enforces Origin like every
    other endpoint."""
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port, "/ping", headers={"Origin": "https://attacker.example"}
        )
    assert status == 403
```

Verify that `_request` supports a `method` kwarg for OPTIONS. If not, extend it — look for `_request` definition in `tests/test_serve.py` and use `urllib.request.Request(..., method=method)`.

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_serve.py -v -k "disallowed_origin or options_preflight or ping_origin or multiple_allow_origins"`
Expected: multiple FAIL.

- [ ] **Step 4: Implement Origin enforcement**

In `src/bsky_saves/serve.py`, extend `_security_gate`:

```python
def _security_gate(self, method: str) -> bool:
    if not self._check_host():
        return False
    if not self._check_origin():
        return False
    return True

def _check_origin(self) -> bool:
    """Reject disallowed-origin requests with 403. Missing Origin is allowed
    (curl-style is permitted per spec §4.4)."""
    origin = self.headers.get("Origin", "")
    if not origin:
        return True
    if origin not in origins:
        self._send_json_error(403, "Origin not allowed")
        return False
    return True
```

(Note: `origins` is the closure-captured list from `make_handler`.)

The existing `_cors_headers` method already only echoes `Access-Control-Allow-Origin` when the origin is in `origins`. It can stay as-is — the disallowed-origin path now never reaches it (the gate returned first).

- [ ] **Step 5: Run the targeted tests**

Run: `python -m pytest tests/test_serve.py -v -k "disallowed_origin or options_preflight or ping_origin"`
Expected: ALL PASS.

- [ ] **Step 6: Run full serve suite**

Run: `python -m pytest tests/test_serve.py -v`
Expected: ALL PASS (the three flipped tests now match the new behavior).

- [ ] **Step 7: Commit**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): reject disallowed Origin with 403 (incl. OPTIONS preflight)"
```

---

## Phase 8 — Default allowlist becomes additive

### Task 13: `--allow-origin` adds to defaults instead of replacing

**Files:**
- Modify: `src/bsky_saves/serve.py` — change default-allowlist logic in `run_serve`.
- Modify: `src/bsky_saves/cli.py` — update `--allow-origin` help text.
- Modify: `tests/test_serve.py` — flip `test_allow_origin_override_replaces_default`, add new tests.

**Context:** Today, passing `--allow-origin foo` replaces the default allowlist entirely, silently dropping `saves.lightseed.net` and the loopback origins. Per spec §4.4 this is a footgun. After this task, `--allow-origin` is additive: defaults always present, custom origins added.

The default list now also includes the two loopback origins keyed by port: `http://127.0.0.1:<port>` and `http://localhost:<port>`.

- [ ] **Step 1: Write the new tests**

In `tests/test_serve.py`:

**Replace** `test_allow_origin_override_replaces_default` with:

```python
def test_allow_origin_additive_keeps_defaults():
    """Custom --allow-origin entries are added to (not replace) the default
    allowlist. The default origin (https://saves.lightseed.net) must still
    be allowed after passing --allow-origin."""
    custom = "https://custom.example"
    with serve_in_background(allow_origins=(custom,)) as (port, _):
        # Default origin still allowed.
        status_default, h_default, _ = _request(
            port, "/ping", headers={"Origin": DEFAULT_ORIGIN}
        )
        # Custom origin allowed.
        status_custom, h_custom, _ = _request(
            port, "/ping", headers={"Origin": custom}
        )
        # Unlisted origin still rejected.
        status_other, _, _ = _request(
            port, "/ping", headers={"Origin": "https://unlisted.example"}
        )
    assert status_default == 200
    assert h_default["Access-Control-Allow-Origin"] == DEFAULT_ORIGIN
    assert status_custom == 200
    assert h_custom["Access-Control-Allow-Origin"] == custom
    assert status_other == 403
```

Add:

```python
def test_default_allowlist_includes_loopback_origins():
    """The default allowlist now includes http://127.0.0.1:<port> and
    http://localhost:<port> in addition to https://saves.lightseed.net."""
    with serve_in_background() as (port, _):
        loopback_v4 = f"http://127.0.0.1:{port}"
        loopback_dns = f"http://localhost:{port}"
        status_v4, h_v4, _ = _request(
            port, "/ping", headers={"Origin": loopback_v4}
        )
        status_dns, h_dns, _ = _request(
            port, "/ping", headers={"Origin": loopback_dns}
        )
    assert status_v4 == 200
    assert h_v4["Access-Control-Allow-Origin"] == loopback_v4
    assert status_dns == 200
    assert h_dns["Access-Control-Allow-Origin"] == loopback_dns
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_serve.py::test_allow_origin_additive_keeps_defaults tests/test_serve.py::test_default_allowlist_includes_loopback_origins -v`
Expected: FAIL.

- [ ] **Step 3: Update `run_serve` to compute additive defaults**

In `src/bsky_saves/serve.py`, replace the existing `run_serve` body (around lines 616-642). The key change is the `origins = ...` line; everything else stays.

Add a helper just above `run_serve`:

```python
def _default_origins(port: int) -> list[str]:
    """Default origin allowlist, computed from the bound port. Spec §4.4."""
    return [
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        "https://saves.lightseed.net",
    ]
```

In `run_serve`, change:

```python
    origins = list(allow_origins or ["https://saves.lightseed.net"])
```

To:

```python
    origins = _default_origins(port) + list(allow_origins or [])
```

- [ ] **Step 4: Update CLI help text in `cli.py`**

Find the `--allow-origin` argument definition (around lines 123-131). Update its `help` text to mention the additive behavior. Example:

```python
p_serve.add_argument(
    "--allow-origin",
    action="append",
    default=None,
    help=(
        "Additional Origin to permit, in addition to the defaults "
        "(http://127.0.0.1:<port>, http://localhost:<port>, "
        "https://saves.lightseed.net). May be specified multiple times."
    ),
)
```

If the docstring at the top of `cli.py` (around line 16) shows usage like `bsky-saves serve [--allow-origin ORIGIN]...`, the usage stays the same; the help text now explains the new semantics.

- [ ] **Step 5: Run targeted tests**

Run: `python -m pytest tests/test_serve.py::test_allow_origin_additive_keeps_defaults tests/test_serve.py::test_default_allowlist_includes_loopback_origins -v`
Expected: PASS.

- [ ] **Step 6: Run full serve suite**

Run: `python -m pytest tests/test_serve.py -v`
Expected: ALL PASS.

- [ ] **Step 7: Commit**

```bash
git add src/bsky_saves/serve.py src/bsky_saves/cli.py tests/test_serve.py
git commit -m "feat(serve): --allow-origin adds to defaults instead of replacing"
```

---

## Phase 9 — Request body size cap

### Task 14: Cap request body size at 10 MB

**Files:**
- Modify: `src/bsky_saves/serve.py` — add `_MAX_BODY_BYTES`, `_BODY_REJECTED` sentinel, update `_read_json_body` and every POST handler.
- Modify: `tests/test_serve.py` — add 413 tests.

**Context:** Spec §6. A uniform 10 MB cap applied to every endpoint. Oversize bodies get `413` immediately without being read into memory. The `_BODY_REJECTED` sentinel distinguishes "already-sent-413" from "missing body (handler sends its own 400)".

- [ ] **Step 1: Write failing tests**

Add to `tests/test_serve.py`:

```python
def test_body_at_cap_succeeds():
    """A body well under the 10 MB cap is processed normally."""
    payload = json.dumps({"uris": ["at://x"] * 1000}).encode("utf-8")
    assert len(payload) < 10 * 1024 * 1024
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port,
            "/enrich",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            },
            data=payload,
        )
    # /enrich tolerates invalid URIs and returns 200 with errors[].
    assert status == 200


def test_body_over_cap_returns_413():
    """A body over 10 MB is rejected with 413."""
    # ~11 MB of well-formed JSON. The exact byte count just needs to exceed
    # 10 * 1024 * 1024 = 10,485,760 bytes.
    payload = b'{"uris":[' + b'"x",' * 2_700_000 + b'"y"]}'
    assert len(payload) > 10 * 1024 * 1024
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/enrich",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            },
            data=payload,
        )
    assert status == 413
    assert json.loads(body) == {"error": "request too large"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_serve.py::test_body_at_cap_succeeds tests/test_serve.py::test_body_over_cap_returns_413 -v`
Expected: `test_body_at_cap_succeeds` passes; `test_body_over_cap_returns_413` FAILS (currently the server happily reads any size).

- [ ] **Step 3: Add the constant, sentinel, and updated `_read_json_body`**

In `src/bsky_saves/serve.py`:

Add module-level constants near the top (after imports):

```python
_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB uniform cap on every POST body.
_BODY_REJECTED: object = object()    # Sentinel for "413 already sent."
```

Modify `_read_json_body` (around lines 563-575):

```python
def _read_json_body(self) -> dict | None | object:
    """Read and parse the JSON request body.

    Returns:
        - dict: successfully parsed body.
        - None: body missing, empty, malformed, or not a dict; caller sends 400.
        - _BODY_REJECTED: Content-Length exceeded 10 MB; this method already
          sent 413. Caller must return without sending another response.
    """
    try:
        length = int(self.headers.get("Content-Length", "0"))
    except (TypeError, ValueError):
        return None
    if length > _MAX_BODY_BYTES:
        self._send_json_error(413, "request too large")
        return _BODY_REJECTED
    if length <= 0:
        return None
    try:
        raw = self.rfile.read(length)
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None
```

- [ ] **Step 4: Update every POST handler to check the sentinel**

The five POST handlers are `_handle_fetch_image`, `_handle_extract_article`, `_handle_fetch`, `_handle_enrich`, `_handle_hydrate_threads`.

In each, the very first line that reads the body is currently:
```python
body = handler._read_json_body()
```

Change to:
```python
body = handler._read_json_body()
if body is _BODY_REJECTED:
    return
```

`_BODY_REJECTED` needs to be importable into the handler scope. The cleanest place: at module top alongside the other constants. The handler accesses it via the module-level name.

In each handler, the existing `if not isinstance(body, dict)` or `(body or {}).get(...)` patterns continue to work for the `None` case (caller sends its own 400 for missing fields).

- [ ] **Step 5: Run targeted tests**

Run: `python -m pytest tests/test_serve.py -v -k "body_"`
Expected: BOTH PASS.

- [ ] **Step 6: Run full serve suite**

Run: `python -m pytest tests/test_serve.py -v`
Expected: ALL PASS.

- [ ] **Step 7: Commit**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): cap request body size at 10 MB (413 on overflow)"
```

---

## Phase 10 — Response security headers

### Task 15: Add `X-Content-Type-Options: nosniff` + `Cache-Control: no-store`

**Files:**
- Modify: `src/bsky_saves/serve.py` — add two headers to `_send_json`, `_send_bytes`, `do_OPTIONS`.
- Modify: `tests/test_serve.py` — add header presence tests.

**Context:** Spec §7. Belt-and-suspenders defense applied uniformly to every response (success, error, preflight).

- [ ] **Step 1: Write failing tests**

Add to `tests/test_serve.py`:

```python
def test_responses_include_nosniff_header():
    with serve_in_background() as (port, _):
        _, headers, _ = _request(port, "/ping")
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_responses_include_cache_control_no_store():
    with serve_in_background() as (port, _):
        _, headers, _ = _request(port, "/ping")
    assert headers["Cache-Control"] == "no-store"


def test_error_responses_include_security_headers():
    with serve_in_background() as (port, _):
        _, headers, _ = _request(port, "/does-not-exist")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"


def test_options_preflight_includes_security_headers():
    with serve_in_background() as (port, _):
        _, headers, _ = _request(
            port,
            "/ping",
            method="OPTIONS",
            headers={"Origin": DEFAULT_ORIGIN},
        )
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_serve.py -v -k "nosniff or cache_control or security_headers"`
Expected: FAIL.

- [ ] **Step 3: Add a `_security_headers` helper and call it from response paths**

In the `Handler` class inside `make_handler`, add:

```python
def _security_headers(self) -> None:
    """Headers applied to every response. Tightly bounded defense-in-depth."""
    self.send_header("X-Content-Type-Options", "nosniff")
    self.send_header("Cache-Control", "no-store")
```

Call it from `_send_json`, `_send_bytes`, and `do_OPTIONS`, before `end_headers()`:

```python
def _send_json(self, code: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    self.send_response(code)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self._cors_headers()
    self._security_headers()
    self.end_headers()
    self.wfile.write(body)

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
    self._security_headers()
    self.end_headers()
    self.wfile.write(body)

def do_OPTIONS(self) -> None:
    self._log_request()
    if not self._security_gate("OPTIONS"):
        return
    self.send_response(204)
    self._cors_headers()
    self._security_headers()
    self.end_headers()
```

- [ ] **Step 4: Run tests to verify**

Run: `python -m pytest tests/test_serve.py -v -k "nosniff or cache_control or security_headers"`
Expected: PASS.

- [ ] **Step 5: Run full serve suite**

Run: `python -m pytest tests/test_serve.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): add X-Content-Type-Options and Cache-Control to responses"
```

---

## Phase 11 — Apply SSRF guard to daemon endpoints

### Task 16: SSRF guard on `credentials.pds` field (F2)

**Files:**
- Modify: `src/bsky_saves/serve.py` — apply `assert_public_http_url` in `_validate_creds`.
- Modify: `tests/test_serve.py` — add `pds`-field rejection tests.

**Context:** Spec §5.3. The `pds` field on `/fetch` and `/hydrate-threads` credentials is user-controlled. Today it could be `http://169.254.169.254` and the daemon would happily POST credentials there. After this task, bad `pds` values are caught in `_validate_creds` (which already returns `None` on validation failure, causing the existing `400 {"error":"missing credentials"}` response).

- [ ] **Step 1: Write failing tests**

Add to `tests/test_serve.py`:

```python
def test_fetch_rejects_pds_pointing_at_loopback():
    body = {
        "credentials": {
            "handle": "user.bsky.social",
            "app_password": "xxxx-xxxx-xxxx-xxxx",
            "pds": "http://127.0.0.1:8080",
        }
    }
    with serve_in_background() as (port, _):
        status, _, body_resp = _request(
            port,
            "/fetch",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            },
            data=json.dumps(body).encode("utf-8"),
        )
    assert status == 400
    assert json.loads(body_resp) == {"error": "missing credentials"}


def test_fetch_rejects_pds_pointing_at_metadata_ip():
    body = {
        "credentials": {
            "handle": "user.bsky.social",
            "app_password": "xxxx-xxxx-xxxx-xxxx",
            "pds": "https://169.254.169.254",
        }
    }
    with serve_in_background() as (port, _):
        status, _, body_resp = _request(
            port,
            "/fetch",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            },
            data=json.dumps(body).encode("utf-8"),
        )
    assert status == 400


def test_hydrate_threads_rejects_pds_pointing_at_private_ip():
    body = {
        "uris": ["at://example/post/1"],
        "credentials": {
            "handle": "user.bsky.social",
            "app_password": "xxxx-xxxx-xxxx-xxxx",
            "pds": "https://10.0.0.1",
        },
    }
    with serve_in_background() as (port, _):
        status, _, _ = _request(
            port,
            "/hydrate-threads",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            },
            data=json.dumps(body).encode("utf-8"),
        )
    assert status == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_serve.py -v -k "rejects_pds"`
Expected: FAIL (current code happily accepts unsafe `pds` values and proceeds to make outbound HTTP).

- [ ] **Step 3: Apply the SSRF guard in `_validate_creds`**

In `src/bsky_saves/serve.py`:

Add at the top of the file:
```python
from ._net import UnsafeURLError, assert_public_http_url
```

Modify `_validate_creds` (around line 398). After the `pds = DEFAULT_PDS` resolution but before the variant detection, add a validation block:

```python
def _validate_creds(creds: object) -> dict | None:
    if not isinstance(creds, dict):
        return None

    pds = creds.get("pds")
    if not isinstance(pds, str) or not pds:
        pds = DEFAULT_PDS

    # SSRF guard: pds must be a safe HTTPS URL (no plain HTTP, no
    # private/loopback/link-local/metadata IPs).
    try:
        assert_public_http_url(pds, allow_http=False)
    except UnsafeURLError:
        return None

    # ... rest of function unchanged
```

The `DEFAULT_PDS` constant is `https://bsky.social`, which passes the guard, so users who don't override `pds` are unaffected.

- [ ] **Step 4: Run targeted tests**

Run: `python -m pytest tests/test_serve.py -v -k "rejects_pds"`
Expected: PASS.

- [ ] **Step 5: Run full serve suite**

Run: `python -m pytest tests/test_serve.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): SSRF guard on credentials.pds (F2)"
```

---

### Task 17: `/fetch-image` redirect handling with per-hop allowlist (F8)

**Files:**
- Modify: `src/bsky_saves/serve.py` — switch `_handle_fetch_image` to `safe_http_get` with `hop_check`.
- Modify: `tests/test_serve.py` — add redirect-handling tests.

**Context:** Spec §8. Today `/fetch-image` calls `httpx.get(url, follow_redirects=True)`; the bsky.app allowlist runs only on the initial URL. After this task, redirects are walked manually and each hop is re-checked against both the bsky.app allowlist and the SSRF guard.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_serve.py`:

```python
@respx.mock
def test_fetch_image_follows_safe_redirect_to_bsky_cdn():
    # Set up a 302 within bsky.app → 200.
    respx.get("https://cdn.bsky.app/img/a.jpg").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://cdn.bsky.app/img/b.jpg"}
        )
    )
    respx.get("https://cdn.bsky.app/img/b.jpg").mock(
        return_value=httpx.Response(
            200, content=b"\xff\xd8\xff\xe0", headers={"Content-Type": "image/jpeg"}
        )
    )
    with serve_in_background() as (port, _):
        status, headers, body = _request(
            port,
            "/fetch-image",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            },
            data=json.dumps(
                {"url": "https://cdn.bsky.app/img/a.jpg"}
            ).encode("utf-8"),
        )
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert body == b"\xff\xd8\xff\xe0"


@respx.mock
def test_fetch_image_rejects_redirect_to_non_bsky_host():
    respx.get("https://cdn.bsky.app/img/a.jpg").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://evil.example/x.jpg"}
        )
    )
    with serve_in_background() as (port, _):
        status, _, body = _request(
            port,
            "/fetch-image",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": DEFAULT_ORIGIN,
            },
            data=json.dumps(
                {"url": "https://cdn.bsky.app/img/a.jpg"}
            ).encode("utf-8"),
        )
    assert status == 400
    assert json.loads(body) == {"error": "url not allowed"}
```

Confirm `respx` and `httpx` are already imported at the top of `tests/test_serve.py`. If not, add them.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_serve.py -v -k "fetch_image_follows_safe or fetch_image_rejects_redirect"`
Expected: at least one FAIL.

- [ ] **Step 3: Update `_handle_fetch_image` in `serve.py`**

In `src/bsky_saves/serve.py`:

Add to the existing `_net` import line:
```python
from ._net import UnsafeURLError, assert_public_http_url, safe_http_get
```

Modify `_handle_fetch_image` (around lines 68-91):

```python
def _handle_fetch_image(handler) -> None:
    body = handler._read_json_body()
    if body is _BODY_REJECTED:
        return
    url = (body or {}).get("url")
    if not isinstance(url, str) or not url:
        handler._send_json_error(400, "missing url")
        return
    if not _is_allowed_image_url(url):
        handler._send_json_error(400, "url not allowed")
        return

    def enforce_bsky_cdn(u: str) -> None:
        if not _is_allowed_image_url(u):
            raise UnsafeURLError("not a bsky.app CDN URL")

    try:
        r = safe_http_get(
            url,
            allow_http=False,
            max_redirects=3,
            hop_check=enforce_bsky_cdn,
            headers={"User-Agent": _IMAGE_USER_AGENT, "Accept": "image/*"},
            timeout=_IMAGE_TIMEOUT,
        )
    except UnsafeURLError:
        handler._send_json_error(400, "url not allowed")
        return
    except Exception as e:
        handler._send_json_error(502, f"{type(e).__name__}: {str(e)[:200]}")
        return
    if r.status_code >= 400:
        handler._send_json_error(r.status_code, f"upstream {r.status_code}")
        return
    content_type = r.headers.get("Content-Type", "application/octet-stream")
    handler._send_bytes(r.status_code, content_type, r.content)
```

(Note the `_BODY_REJECTED` check — should already be in place from Task 14, but verify.)

- [ ] **Step 4: Run targeted tests**

Run: `python -m pytest tests/test_serve.py -v -k "fetch_image"`
Expected: ALL PASS (existing + 2 new).

- [ ] **Step 5: Run full suite**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): walk /fetch-image redirects with per-hop allowlist (F8)"
```

---

## Phase 12 — Verbose log sanitization

### Task 18: Sanitize control characters in verbose request log

**Files:**
- Modify: `src/bsky_saves/serve.py` — escape control chars in `_log_request`.
- Modify: `tests/test_serve.py` — add a test.

**Context:** Spec §9. `self.path` could contain literal terminal-escape bytes. Sanitize before printing.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_serve.py`:

```python
def test_log_request_escapes_control_chars(capsys):
    """_log_request must escape terminal control bytes via
    encode('ascii', 'backslashreplace') so a request with ESC bytes in the
    path can't reposition the operator's terminal cursor."""
    from bsky_saves.serve import make_handler

    HandlerCls = make_handler(port=1, allow_origins=[], verbose=True)

    # _log_request only reads self.command and self.path, so a minimal stub
    # works. Calling the unbound method directly avoids the BaseHTTPRequestHandler
    # initialization dance (sockets, request parsing, etc.).
    class _Stub:
        command = "GET"
        path = "/ping\x1b[2J"

    HandlerCls._log_request(_Stub())

    captured = capsys.readouterr()
    # The escape byte should appear as its escape sequence in stderr,
    # not as the raw control byte.
    assert "\\x1b" in captured.err
    assert "\x1b" not in captured.err
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_serve.py::test_log_request_escapes_control_chars -v`
Expected: FAIL — the current `_log_request` prints `self.path` raw, so the ESC byte lands in `captured.err` and the second assertion fires.

- [ ] **Step 3: Update `_log_request` in `serve.py`**

Replace (around line 521):

```python
def _log_request(self) -> None:
    if verbose:
        print(
            f"bsky-saves: {self.command} {self.path}",
            file=sys.stderr,
        )
```

With:

```python
def _log_request(self) -> None:
    if verbose:
        safe_path = self.path.encode("ascii", "backslashreplace").decode("ascii")
        print(
            f"bsky-saves: {self.command} {safe_path}",
            file=sys.stderr,
        )
```

- [ ] **Step 4: Run full serve suite to confirm no regression**

Run: `python -m pytest tests/test_serve.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): sanitize control characters in verbose request log (F9)"
```

---

## Phase 13 — Version bump and final verification

### Task 19: Bump version to 0.4.4

**Files:**
- Modify: `pyproject.toml`

**Context:** Final task. Bump the version, run the full test suite, build the wheel, verify the version string appears in package metadata.

- [ ] **Step 1: Bump the version in `pyproject.toml`**

Find the `[project]` block:
```toml
[project]
name = "bsky-saves"
version = "0.4.3"
```

Change to:
```toml
[project]
name = "bsky-saves"
version = "0.4.4"
```

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest -v`
Expected: all tests pass. Target count ~223 (from spec §13.4).

- [ ] **Step 3: Build the wheel and verify version**

Run: `rm -rf dist/ build/ src/bsky_saves.egg-info/ && python -m build`
Expected: build succeeds; `dist/bsky_saves-0.4.4-py3-none-any.whl` and `dist/bsky_saves-0.4.4.tar.gz` exist.

Run: `python -m venv /tmp/v-044-smoke && /tmp/v-044-smoke/bin/pip install dist/bsky_saves-0.4.4-py3-none-any.whl`
Expected: install succeeds.

Run: `/tmp/v-044-smoke/bin/python -c "import bsky_saves; print(bsky_saves.__version__)"`
Expected output: `0.4.4`.

Run: `/tmp/v-044-smoke/bin/bsky-saves --help`
Expected: usage block renders without errors.

- [ ] **Step 4: Live daemon smoke test**

Run (in background): `/tmp/v-044-smoke/bin/bsky-saves serve --port 47840 &`
Sleep 1 second.

Test endpoints:

```bash
# /ping happy path
curl -fsS http://127.0.0.1:47840/ping

# Disallowed origin → 403
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:47840/ping -H "Origin: https://evil.com"
# Expected: 403

# Bad Host header → 421
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:47840/ping -H "Host: evil.com"
# Expected: 421

# SSRF rejection on /extract-article
curl -sS -X POST http://127.0.0.1:47840/extract-article \
    -H 'Content-Type: application/json' \
    -d '{"url":"http://169.254.169.254/"}'
# Expected: {"error":"..."} response (the SSRF guard fired upstream; expect 502)

# Response headers
curl -sI http://127.0.0.1:47840/ping | grep -i "x-content-type-options\|cache-control"
# Expected: both headers present.
```

Kill the daemon: `kill %1`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "chore: bump version to 0.4.4"
```

- [ ] **Step 6: Push the branch**

Run: `git push -u origin claude/bsky-saves-next-phase-o7mOi`
Expected: pushes successfully. The project owner will open the PR via GitHub UI and merge to main; release tag publishes to PyPI via trusted publishing.

---

## Post-implementation checklist

After Task 19 ships:

- [ ] PR opened and merged to `main` via GitHub UI.
- [ ] `v0.4.4` tag created on the merge commit via GitHub UI.
- [ ] `release.yml` workflow run succeeds and `bsky-saves==0.4.4` appears on PyPI.
- [ ] Project owner relays the §12 spec-edit proposal to the bsky-saves-gui team.
- [ ] v0.5.0 brainstorming session is scheduled (GUI vendoring + `--gui` flag).
