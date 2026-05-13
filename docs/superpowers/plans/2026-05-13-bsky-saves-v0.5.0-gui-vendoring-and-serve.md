# bsky-saves v0.5.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Vendor the `bsky-saves-gui` `dist.tar.gz` artifact into the wheel at build time and add a `bsky-saves serve --gui` flag that mounts the bundled GUI at `/` on the same loopback port that serves the JSON API.

**Architecture:** Two new private modules: `scripts/fetch_gui.py` (build-time tarball fetch + SHA-256 verify + extract) and `src/bsky_saves/_gui_serve.py` (static-file serving with SPA fallback, cache classes, and per-bundle security headers). A small Hatch custom build hook (`hatch_build.py`) wires the script into `python -m build` and `pip install bsky-saves` from sdist. The `serve.py` dispatcher gains a new GET/HEAD branch that routes into `_gui_serve` after the existing API-route lookup; API routes still take precedence.

**Tech Stack:** Python 3.11+ (tomllib stdlib), `httpx` (already a dep), `hatchling` (build backend, already in use), `pytest` + `respx` (existing test infrastructure). Stdlib `urllib.request` for the fetch script (avoids adding httpx as a build-time dependency).

**Spec:** `docs/superpowers/specs/2026-05-13-bsky-saves-v0.5.0-gui-vendoring-and-serve.md`

**Test invocation:** Always `python -m pytest` from the repo root — never bare `pytest`. The system-`pytest` uses a different Python that does not have `bsky-saves` installed (per the v0.4.3 handoff carried into v0.4.4).

---

## File map

**New files:**
- `scripts/fetch_gui.py` — standalone build script.
- `hatch_build.py` (project root) — Hatch custom hook glue.
- `gui-dist.sha256` (project root) — byte-identical to GitHub release `.sha256`.
- `src/bsky_saves/_gui_serve.py` — static-file dispatcher and helpers.
- `tests/test_fetch_gui.py` — unit tests for the script.
- `tests/test_serve_gui.py` — unit tests for `_gui_serve.py` + the dispatcher branch.
- `.github/workflows/smoke.yml` — pre-release CI smoke test.

**Modified files:**
- `pyproject.toml` — new `[tool.bsky-saves]` table, `[tool.hatch.build.hooks.custom]` config, `[tool.hatch.build] artifacts = [...]`, version bump.
- `.gitignore` — add `src/bsky_saves/_gui/`.
- `src/bsky_saves/cli.py` — `--gui` flag on `serve`.
- `src/bsky_saves/serve.py` — `gui_root` parameter on `make_handler`/`run_serve`; startup guard; dispatcher branch.
- `src/bsky_saves/images.py` — delete unused `import httpx`.
- `src/bsky_saves/threads.py` — derive User-Agent string from `__version__`.
- `tests/conftest.py` — new `gui_tarball_fixture` factory.
- `.github/workflows/verify.yml` — add `python scripts/fetch_gui.py` step.

---

## Ordering rationale

The fetch script and `_gui_serve` module are testable independently using mocked HTTP + in-memory tarballs. We build all that infrastructure first (Tasks 1-12). Only Task 13 depends on the real `bsky-saves-gui v0.5.3` release existing; the controller-side coordinates that release at that moment. After Task 13, Tasks 14-16 wire the build system and CI to use the real artifact. Final smoke + version bump in Task 17.

---

## Phase 1 — Pin scaffolding

### Task 1: Pin `gui_version`, gitignore `_gui/`, placeholder `gui-dist.sha256`

**Files:**
- Modify: `pyproject.toml` — add new `[tool.bsky-saves]` table.
- Modify: `.gitignore` — add `src/bsky_saves/_gui/`.
- Create: `gui-dist.sha256` at repo root (placeholder content; real value lands in Task 13).

**Context:** No tests in this task; it's pure configuration scaffolding so subsequent tasks can read `gui_version` from `pyproject.toml`. The placeholder sha256 is never consumed (Tasks 2-12 use mocked fixtures); it gets replaced with the real value in Task 13 once the GUI v0.5.3 release exists.

- [ ] **Step 1: Add `[tool.bsky-saves]` table to `pyproject.toml`**

Add this table somewhere logical (e.g., after `[project]` or before `[tool.hatch.*]`):

```toml
[tool.bsky-saves]
# Pinned bsky-saves-gui release. Scripts/fetch_gui.py downloads
# dist.tar.gz from https://github.com/tenorune/bsky-saves-gui/releases/
# download/v{gui_version}/dist.tar.gz and verifies it against gui-dist.sha256.
# Bumping this requires updating gui-dist.sha256 from the same release.
gui_version = "0.5.3"
```

- [ ] **Step 2: Add `_gui/` to `.gitignore`**

Append to `.gitignore`:

```
# GUI bundle vendored by scripts/fetch_gui.py at build time.
# Not source-tracked; ships in the built wheel via [tool.hatch.build] artifacts.
src/bsky_saves/_gui/
```

- [ ] **Step 3: Create placeholder `gui-dist.sha256`**

Create `gui-dist.sha256` at repo root with this exact content (single line, two spaces between hash and filename, trailing newline):

```
0000000000000000000000000000000000000000000000000000000000000000  dist.tar.gz
```

This zero-sha placeholder will be replaced with the real v0.5.3 hash in Task 13.

- [ ] **Step 4: Confirm pyproject still parses**

Run: `python -c "import tomllib; print(tomllib.loads(open('pyproject.toml').read())['tool']['bsky-saves']['gui_version'])"`
Expected: `0.5.3`

- [ ] **Step 5: Run full test suite to confirm no regression**

Run: `python -m pytest -q`
Expected: 244 passed.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore gui-dist.sha256
git commit -m "chore(build): scaffold GUI vendoring pin (gui_version=0.5.3)"
```

---

## Phase 2 — `scripts/fetch_gui.py`

### Task 2: Fetch script — pyproject parsing, sha256 file parsing, download + verify

**Files:**
- Create: `scripts/fetch_gui.py`
- Create: `tests/conftest.py` additions (new `gui_tarball_fixture` factory)
- Create: `tests/test_fetch_gui.py`

**Context:** First slice of the build-time fetch script. Reads `gui_version` from `pyproject.toml`, reads expected SHA-256 from `gui-dist.sha256`, downloads via `urllib.request.urlopen`, verifies the bytes, with a placeholder for extraction (added in Task 3). Tests mock `urllib.request.urlopen` with in-memory bytes — no real network.

- [ ] **Step 1: Add `gui_tarball_fixture` to `tests/conftest.py`**

Look at the existing `tests/conftest.py` structure. Add the following imports near the top:

```python
import hashlib
import io
import tarfile
```

Add this factory function (export it as a pytest fixture):

```python
@pytest.fixture
def gui_tarball_fixture():
    """Factory yielding (tarball_bytes, sha256_hex) for an in-memory GUI bundle.

    Usage in tests:
        tarball, sha = gui_tarball_fixture({"index.html": b"<html>...</html>"})
    """
    def _make(files: dict[str, bytes]) -> tuple[bytes, str]:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, content in files.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        tarball = buf.getvalue()
        sha = hashlib.sha256(tarball).hexdigest()
        return tarball, sha

    return _make
```

- [ ] **Step 2: Write failing tests in `tests/test_fetch_gui.py`**

```python
"""Unit tests for scripts/fetch_gui.py."""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# scripts/ is not on sys.path; import the module by path manipulation.
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))
from scripts.fetch_gui import (
    fetch_gui,
    read_gui_version,
    read_expected_sha256,
    GuiFetchError,
)


def _write_pyproject(root: Path, version: str = "0.5.3") -> None:
    (root / "pyproject.toml").write_text(
        f'[tool.bsky-saves]\ngui_version = "{version}"\n',
        encoding="utf-8",
    )


def _write_sha256(root: Path, sha: str) -> None:
    (root / "gui-dist.sha256").write_text(
        f"{sha}  dist.tar.gz\n", encoding="utf-8"
    )


def test_read_gui_version_returns_string(tmp_path):
    _write_pyproject(tmp_path, "0.5.3")
    assert read_gui_version(tmp_path) == "0.5.3"


def test_read_gui_version_raises_on_missing_table(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n', encoding="utf-8"
    )
    with pytest.raises(GuiFetchError):
        read_gui_version(tmp_path)


def test_read_expected_sha256_parses_hash_and_filename(tmp_path):
    _write_sha256(tmp_path, "a" * 64)
    assert read_expected_sha256(tmp_path) == "a" * 64


def test_read_expected_sha256_raises_on_wrong_format(tmp_path):
    (tmp_path / "gui-dist.sha256").write_text("notahash\n", encoding="utf-8")
    with pytest.raises(GuiFetchError):
        read_expected_sha256(tmp_path)


def test_fetch_gui_downloads_and_verifies(tmp_path, gui_tarball_fixture, monkeypatch):
    """Happy path: pyproject pin + sha256 file match a downloaded tarball."""
    tarball, sha = gui_tarball_fixture({"index.html": b"<html>test</html>"})
    _write_pyproject(tmp_path, "0.5.3")
    _write_sha256(tmp_path, sha)

    # Mock urllib.request.urlopen to return the tarball bytes.
    mock_response = MagicMock()
    mock_response.read.return_value = tarball
    mock_response.url = (
        "https://release-assets.githubusercontent.com/..."
    )
    mock_response.__enter__.return_value = mock_response

    with patch("scripts.fetch_gui.urllib.request.urlopen", return_value=mock_response):
        # Note: extraction is added in Task 3; for now we only verify that
        # fetch_gui completes without raising on byte verification.
        # (Tarball is *not* extracted yet in Task 2's implementation.)
        fetch_gui(tmp_path)


def test_fetch_gui_aborts_on_sha_mismatch(tmp_path, gui_tarball_fixture, monkeypatch):
    """Downloaded bytes whose SHA-256 doesn't match gui-dist.sha256 must abort."""
    tarball, _real_sha = gui_tarball_fixture({"index.html": b"x"})
    _write_pyproject(tmp_path, "0.5.3")
    _write_sha256(tmp_path, "0" * 64)  # deliberately wrong

    mock_response = MagicMock()
    mock_response.read.return_value = tarball
    mock_response.url = "https://release-assets.githubusercontent.com/..."
    mock_response.__enter__.return_value = mock_response

    with patch("scripts.fetch_gui.urllib.request.urlopen", return_value=mock_response):
        with pytest.raises(GuiFetchError) as exc_info:
            fetch_gui(tmp_path)
    assert "SHA-256 mismatch" in str(exc_info.value)


def test_fetch_gui_aborts_on_non_github_redirect(tmp_path, gui_tarball_fixture):
    """If the response.url after redirects is not a GitHub host, abort."""
    tarball, sha = gui_tarball_fixture({"index.html": b"x"})
    _write_pyproject(tmp_path, "0.5.3")
    _write_sha256(tmp_path, sha)

    mock_response = MagicMock()
    mock_response.read.return_value = tarball
    mock_response.url = "https://evil.example/path"
    mock_response.__enter__.return_value = mock_response

    with patch("scripts.fetch_gui.urllib.request.urlopen", return_value=mock_response):
        with pytest.raises(GuiFetchError) as exc_info:
            fetch_gui(tmp_path)
    assert "unexpected" in str(exc_info.value).lower() or "host" in str(exc_info.value).lower()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_fetch_gui.py -v`
Expected: 7 FAIL with `ModuleNotFoundError: No module named 'scripts.fetch_gui'`.

- [ ] **Step 4: Implement `scripts/fetch_gui.py`**

Create `scripts/__init__.py` (empty) to make `scripts` an importable package:

```bash
touch scripts/__init__.py
```

Create `scripts/fetch_gui.py`:

```python
"""Vendor the bsky-saves-gui dist.tar.gz into src/bsky_saves/_gui/.

Run as a build-time hook (via hatch_build.py) or directly:
    python scripts/fetch_gui.py
"""
from __future__ import annotations

import hashlib
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path


class GuiFetchError(Exception):
    """fetch_gui could not vendor the GUI bundle."""


_ALLOWED_REDIRECT_HOSTS = (
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    # GitHub release assets sometimes redirect to S3-backed asset CDNs.
    # The current hostname pattern observed is *.githubusercontent.com.
)


def _release_url(version: str) -> str:
    return (
        f"https://github.com/tenorune/bsky-saves-gui/releases/download/"
        f"v{version}/dist.tar.gz"
    )


def read_gui_version(root: Path) -> str:
    """Read [tool.bsky-saves] gui_version from pyproject.toml."""
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        raise GuiFetchError(f"pyproject.toml not found at {pyproject}")
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    try:
        version = data["tool"]["bsky-saves"]["gui_version"]
    except KeyError as e:
        raise GuiFetchError(
            "pyproject.toml missing [tool.bsky-saves] gui_version"
        ) from e
    if not isinstance(version, str) or not version:
        raise GuiFetchError("gui_version must be a non-empty string")
    return version


def read_expected_sha256(root: Path) -> str:
    """Read the SHA-256 hex from gui-dist.sha256 (GitHub release format).

    Expected file content: '<64-hex>  dist.tar.gz\\n'.
    """
    sha_file = root / "gui-dist.sha256"
    if not sha_file.exists():
        raise GuiFetchError(f"gui-dist.sha256 not found at {sha_file}")
    line = sha_file.read_text(encoding="utf-8").strip()
    parts = line.split()
    if len(parts) < 1 or len(parts[0]) != 64:
        raise GuiFetchError(
            f"gui-dist.sha256 has unexpected format; expected '<64-hex>  dist.tar.gz', got: {line!r}"
        )
    hex_part = parts[0]
    if not all(c in "0123456789abcdef" for c in hex_part.lower()):
        raise GuiFetchError(
            f"gui-dist.sha256 first field is not hex: {hex_part!r}"
        )
    return hex_part.lower()


def _verify_redirect_host(final_url: str) -> None:
    host = urllib.parse.urlparse(final_url).hostname or ""
    if not any(host == h or host.endswith("." + h) for h in _ALLOWED_REDIRECT_HOSTS):
        raise GuiFetchError(
            f"Response redirected to unexpected host: {host!r} "
            f"(allowed: {_ALLOWED_REDIRECT_HOSTS})"
        )


def _download(version: str) -> tuple[bytes, str]:
    """Download dist.tar.gz; return (bytes, actual_sha256_hex)."""
    url = _release_url(version)
    try:
        with urllib.request.urlopen(url) as resp:
            _verify_redirect_host(resp.url)
            data = resp.read()
    except urllib.error.URLError as e:
        raise GuiFetchError(f"download failed: {e}") from e
    actual_sha = hashlib.sha256(data).hexdigest()
    return data, actual_sha


def fetch_gui(root: Path | None = None) -> None:
    """Fetch + verify + extract dist.tar.gz into src/bsky_saves/_gui/.

    Idempotent: if marker file matches the pinned (version, sha256), skip
    the download entirely.

    Args:
        root: Project root (the directory containing pyproject.toml). If
            None, walk up from this script's location.
    """
    if root is None:
        root = Path(__file__).resolve().parent.parent
    root = Path(root)

    version = read_gui_version(root)
    expected_sha = read_expected_sha256(root)

    # NOTE: Tasks 3 and 4 will add extraction and idempotency logic here.
    # For now, just download + verify so the byte-integrity tests pass.
    data, actual_sha = _download(version)
    if actual_sha != expected_sha:
        raise GuiFetchError(
            f"SHA-256 mismatch on dist.tar.gz: expected {expected_sha}, "
            f"got {actual_sha}"
        )

    print(
        f"bsky-saves: downloaded GUI bundle v{version} "
        f"({actual_sha[:16]}...) — extraction in subsequent task",
        file=sys.stderr,
    )


if __name__ == "__main__":
    try:
        fetch_gui()
    except GuiFetchError as e:
        print(f"fetch_gui: {e}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_fetch_gui.py -v`
Expected: 7 PASS.

- [ ] **Step 6: Run full suite**

Run: `python -m pytest -q`
Expected: 251 passed (244 + 7 new).

- [ ] **Step 7: Commit**

```bash
git add scripts/__init__.py scripts/fetch_gui.py tests/conftest.py tests/test_fetch_gui.py
git commit -m "feat(build): scripts/fetch_gui.py core (parse + download + verify)"
```

---

### Task 3: Fetch script — tarball extraction with security defences

**Files:**
- Modify: `scripts/fetch_gui.py` — add extraction step.
- Modify: `tests/test_fetch_gui.py` — add extraction tests.

**Context:** Task 2 downloaded + verified bytes but didn't extract. This task adds extraction with three security/correctness defences:
- Reject tar members with paths that escape the extraction root (tar-slip).
- Strip a leading `dist/` directory prefix if every member has it.
- Skip `CNAME` files at any depth (GitHub Pages artefact).

- [ ] **Step 1: Add failing tests**

Append to `tests/test_fetch_gui.py`:

```python
import tarfile
import io


def _make_tarball_with_paths(paths: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    """Build a tarball with explicit member paths (for path-safety tests)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, content in paths:
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    tarball = buf.getvalue()
    import hashlib
    sha = hashlib.sha256(tarball).hexdigest()
    return tarball, sha


def test_fetch_gui_extracts_index_html(tmp_path, gui_tarball_fixture):
    tarball, sha = gui_tarball_fixture({"index.html": b"<html>hi</html>"})
    _write_pyproject(tmp_path, "0.5.3")
    _write_sha256(tmp_path, sha)

    mock_response = MagicMock()
    mock_response.read.return_value = tarball
    mock_response.url = "https://release-assets.githubusercontent.com/x"
    mock_response.__enter__.return_value = mock_response

    with patch("scripts.fetch_gui.urllib.request.urlopen", return_value=mock_response):
        fetch_gui(tmp_path)

    out = tmp_path / "src" / "bsky_saves" / "_gui"
    assert (out / "index.html").read_bytes() == b"<html>hi</html>"


def test_fetch_gui_strips_dist_prefix(tmp_path):
    tarball, sha = _make_tarball_with_paths([
        ("dist/index.html", b"<html>prefixed</html>"),
        ("dist/assets/x.js", b"console.log(1);"),
    ])
    _write_pyproject(tmp_path, "0.5.3")
    _write_sha256(tmp_path, sha)

    mock_response = MagicMock()
    mock_response.read.return_value = tarball
    mock_response.url = "https://release-assets.githubusercontent.com/x"
    mock_response.__enter__.return_value = mock_response

    with patch("scripts.fetch_gui.urllib.request.urlopen", return_value=mock_response):
        fetch_gui(tmp_path)

    out = tmp_path / "src" / "bsky_saves" / "_gui"
    assert (out / "index.html").read_bytes() == b"<html>prefixed</html>"
    assert (out / "assets" / "x.js").read_bytes() == b"console.log(1);"
    assert not (out / "dist").exists()


def test_fetch_gui_skips_cname(tmp_path):
    tarball, sha = _make_tarball_with_paths([
        ("index.html", b"<html>x</html>"),
        ("CNAME", b"saves.lightseed.net"),
    ])
    _write_pyproject(tmp_path, "0.5.3")
    _write_sha256(tmp_path, sha)

    mock_response = MagicMock()
    mock_response.read.return_value = tarball
    mock_response.url = "https://release-assets.githubusercontent.com/x"
    mock_response.__enter__.return_value = mock_response

    with patch("scripts.fetch_gui.urllib.request.urlopen", return_value=mock_response):
        fetch_gui(tmp_path)

    out = tmp_path / "src" / "bsky_saves" / "_gui"
    assert (out / "index.html").exists()
    assert not (out / "CNAME").exists()


def test_fetch_gui_rejects_tar_slip(tmp_path):
    """A tarball with a member that escapes the extraction root must abort."""
    tarball, sha = _make_tarball_with_paths([
        ("../../../etc/passwd", b"root::0:0:...\n"),
    ])
    _write_pyproject(tmp_path, "0.5.3")
    _write_sha256(tmp_path, sha)

    mock_response = MagicMock()
    mock_response.read.return_value = tarball
    mock_response.url = "https://release-assets.githubusercontent.com/x"
    mock_response.__enter__.return_value = mock_response

    with patch("scripts.fetch_gui.urllib.request.urlopen", return_value=mock_response):
        with pytest.raises(GuiFetchError) as exc_info:
            fetch_gui(tmp_path)
    assert "unsafe" in str(exc_info.value).lower() or "outside" in str(exc_info.value).lower()
```

- [ ] **Step 2: Run tests to verify failure modes**

Run: `python -m pytest tests/test_fetch_gui.py -v -k "extract or strip or skip or tar_slip"`
Expected: at least one FAIL (current code doesn't extract at all).

- [ ] **Step 3: Add extraction to `fetch_gui`**

In `scripts/fetch_gui.py`, add a new helper at module level:

```python
import shutil
import tarfile
import io


def _extract_tarball(data: bytes, dest: Path) -> None:
    """Extract dist.tar.gz into dest, applying security and layout rules.

    - Reject any member whose normalised path escapes dest (tar-slip).
    - Strip a leading 'dist/' directory prefix if every non-skipped member has it.
    - Skip CNAME files.
    """
    dest = dest.resolve()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if Path(m.name).name != "CNAME"]

        # Decide whether to strip a leading 'dist/' prefix.
        strip_prefix = ""
        if members and all(
            m.name == "dist" or m.name.startswith("dist/")
            for m in members
            if m.name
        ):
            strip_prefix = "dist/"

        for m in members:
            name = m.name
            if strip_prefix and name.startswith(strip_prefix):
                name = name[len(strip_prefix):]
            if not name:
                continue
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest)):
                raise GuiFetchError(
                    f"refusing to extract member outside dest: {m.name!r}"
                )
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not m.isfile():
                continue  # skip symlinks, devices, etc.
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(m)
            if extracted is None:
                continue
            target.write_bytes(extracted.read())
```

Update `fetch_gui` to call `_extract_tarball` after verification:

```python
def fetch_gui(root: Path | None = None) -> None:
    if root is None:
        root = Path(__file__).resolve().parent.parent
    root = Path(root)

    version = read_gui_version(root)
    expected_sha = read_expected_sha256(root)

    data, actual_sha = _download(version)
    if actual_sha != expected_sha:
        raise GuiFetchError(
            f"SHA-256 mismatch on dist.tar.gz: expected {expected_sha}, "
            f"got {actual_sha}"
        )

    gui_dir = root / "src" / "bsky_saves" / "_gui"
    _extract_tarball(data, gui_dir)

    print(
        f"bsky-saves: vendored GUI bundle v{version} "
        f"({actual_sha[:16]}...) → {gui_dir.relative_to(root)}",
        file=sys.stderr,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_fetch_gui.py -v`
Expected: 11 PASS (7 from Task 2 + 4 new).

- [ ] **Step 5: Run full suite**

Run: `python -m pytest -q`
Expected: 255 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/fetch_gui.py tests/test_fetch_gui.py
git commit -m "feat(build): fetch_gui extraction with tar-slip defence, dist/ strip, CNAME skip"
```

---

### Task 4: Fetch script — idempotency marker

**Files:**
- Modify: `scripts/fetch_gui.py` — add marker-file check before download.
- Modify: `tests/test_fetch_gui.py` — add idempotency tests.

**Context:** A marker file `_gui/.gui-version` containing two lines (`{version}\n{sha256}\n`) tells subsequent runs they can skip the download entirely. Bumping the pin (either version or sha256) busts the cache. Important for local dev: contributors running `python scripts/fetch_gui.py` repeatedly shouldn't refetch.

- [ ] **Step 1: Add failing tests**

Append to `tests/test_fetch_gui.py`:

```python
def test_fetch_gui_writes_marker_on_success(tmp_path, gui_tarball_fixture):
    tarball, sha = gui_tarball_fixture({"index.html": b"<html>x</html>"})
    _write_pyproject(tmp_path, "0.5.3")
    _write_sha256(tmp_path, sha)

    mock_response = MagicMock()
    mock_response.read.return_value = tarball
    mock_response.url = "https://release-assets.githubusercontent.com/x"
    mock_response.__enter__.return_value = mock_response

    with patch("scripts.fetch_gui.urllib.request.urlopen", return_value=mock_response):
        fetch_gui(tmp_path)

    marker = tmp_path / "src" / "bsky_saves" / "_gui" / ".gui-version"
    assert marker.exists()
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines == ["0.5.3", sha]


def test_fetch_gui_skips_download_when_marker_matches(tmp_path, gui_tarball_fixture):
    """Idempotency: if marker matches pin, urlopen should not be called."""
    tarball, sha = gui_tarball_fixture({"index.html": b"<html>x</html>"})
    _write_pyproject(tmp_path, "0.5.3")
    _write_sha256(tmp_path, sha)

    # Pre-populate _gui/ + marker so the idempotency check fires.
    gui_dir = tmp_path / "src" / "bsky_saves" / "_gui"
    gui_dir.mkdir(parents=True)
    (gui_dir / "index.html").write_bytes(b"<html>cached</html>")
    (gui_dir / ".gui-version").write_text(f"0.5.3\n{sha}\n", encoding="utf-8")

    mock_urlopen = MagicMock()
    with patch("scripts.fetch_gui.urllib.request.urlopen", mock_urlopen):
        fetch_gui(tmp_path)

    mock_urlopen.assert_not_called()
    # Pre-existing content untouched.
    assert (gui_dir / "index.html").read_bytes() == b"<html>cached</html>"


def test_fetch_gui_refetches_when_version_bumped(tmp_path, gui_tarball_fixture):
    """Marker has old version → re-fetch."""
    tarball, sha = gui_tarball_fixture({"index.html": b"<html>new</html>"})
    _write_pyproject(tmp_path, "0.5.3")
    _write_sha256(tmp_path, sha)

    gui_dir = tmp_path / "src" / "bsky_saves" / "_gui"
    gui_dir.mkdir(parents=True)
    (gui_dir / "index.html").write_bytes(b"<html>old</html>")
    (gui_dir / ".gui-version").write_text(f"0.5.2\n{sha}\n", encoding="utf-8")  # old version

    mock_response = MagicMock()
    mock_response.read.return_value = tarball
    mock_response.url = "https://release-assets.githubusercontent.com/x"
    mock_response.__enter__.return_value = mock_response

    with patch("scripts.fetch_gui.urllib.request.urlopen", return_value=mock_response):
        fetch_gui(tmp_path)

    assert (gui_dir / "index.html").read_bytes() == b"<html>new</html>"


def test_fetch_gui_refetches_when_sha_bumped(tmp_path, gui_tarball_fixture):
    """Marker has matching version but different sha → re-fetch."""
    tarball, sha = gui_tarball_fixture({"index.html": b"<html>new</html>"})
    _write_pyproject(tmp_path, "0.5.3")
    _write_sha256(tmp_path, sha)

    gui_dir = tmp_path / "src" / "bsky_saves" / "_gui"
    gui_dir.mkdir(parents=True)
    (gui_dir / "index.html").write_bytes(b"<html>old</html>")
    (gui_dir / ".gui-version").write_text(f"0.5.3\n{'b' * 64}\n", encoding="utf-8")  # different sha

    mock_response = MagicMock()
    mock_response.read.return_value = tarball
    mock_response.url = "https://release-assets.githubusercontent.com/x"
    mock_response.__enter__.return_value = mock_response

    with patch("scripts.fetch_gui.urllib.request.urlopen", return_value=mock_response):
        fetch_gui(tmp_path)

    assert (gui_dir / "index.html").read_bytes() == b"<html>new</html>"
```

- [ ] **Step 2: Run tests to verify failure modes**

Run: `python -m pytest tests/test_fetch_gui.py -v -k "marker or skip or refetch"`
Expected: tests for marker write and re-fetch should fail; the skip test will incorrectly call urlopen.

- [ ] **Step 3: Add marker logic to `fetch_gui`**

Modify `fetch_gui` in `scripts/fetch_gui.py`:

```python
def fetch_gui(root: Path | None = None) -> None:
    if root is None:
        root = Path(__file__).resolve().parent.parent
    root = Path(root)

    version = read_gui_version(root)
    expected_sha = read_expected_sha256(root)

    gui_dir = root / "src" / "bsky_saves" / "_gui"
    marker = gui_dir / ".gui-version"

    # Idempotency: skip download if marker matches pin.
    if marker.exists():
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            lines = []
        if len(lines) >= 2 and lines[0] == version and lines[1] == expected_sha:
            print(
                f"bsky-saves: GUI bundle v{version} already vendored "
                f"(marker matches); skipping download.",
                file=sys.stderr,
            )
            return

    data, actual_sha = _download(version)
    if actual_sha != expected_sha:
        raise GuiFetchError(
            f"SHA-256 mismatch on dist.tar.gz: expected {expected_sha}, "
            f"got {actual_sha}"
        )

    _extract_tarball(data, gui_dir)
    marker.write_text(f"{version}\n{expected_sha}\n", encoding="utf-8")

    print(
        f"bsky-saves: vendored GUI bundle v{version} "
        f"({actual_sha[:16]}...) → {gui_dir.relative_to(root)}",
        file=sys.stderr,
    )
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_fetch_gui.py -v`
Expected: 15 PASS.

- [ ] **Step 5: Run full suite**

Run: `python -m pytest -q`
Expected: 259 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/fetch_gui.py tests/test_fetch_gui.py
git commit -m "feat(build): fetch_gui idempotency via .gui-version marker file"
```

---

## Phase 3 — `_gui_serve.py`

### Task 5: `_gui_serve.py` core — exceptions, root resolution, content-type

**Files:**
- Create: `src/bsky_saves/_gui_serve.py`
- Create: `tests/test_serve_gui.py`

**Context:** New module containing all static-file serving logic. This task adds the foundation: an exception class, a function to resolve the `_gui/` root (with a clear error when missing), and a content-type detector that handles the few file extensions Vite produces.

- [ ] **Step 1: Write failing tests**

Create `tests/test_serve_gui.py`:

```python
"""Unit tests for bsky_saves._gui_serve."""
from __future__ import annotations

from pathlib import Path

import pytest

from bsky_saves._gui_serve import (
    GuiNotInstalledError,
    content_type_for,
    resolve_gui_root,
)


def test_resolve_gui_root_returns_path_when_index_present(tmp_path, monkeypatch):
    gui_dir = tmp_path / "_gui"
    gui_dir.mkdir()
    (gui_dir / "index.html").write_text("<html>x</html>")

    # Monkeypatch the package-relative path resolution.
    monkeypatch.setattr("bsky_saves._gui_serve._gui_root_path", lambda: gui_dir)
    assert resolve_gui_root() == gui_dir


def test_resolve_gui_root_raises_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("bsky_saves._gui_serve._gui_root_path", lambda: tmp_path / "nope")
    with pytest.raises(GuiNotInstalledError):
        resolve_gui_root()


def test_resolve_gui_root_raises_when_index_missing(tmp_path, monkeypatch):
    gui_dir = tmp_path / "_gui"
    gui_dir.mkdir()
    # No index.html.
    monkeypatch.setattr("bsky_saves._gui_serve._gui_root_path", lambda: gui_dir)
    with pytest.raises(GuiNotInstalledError):
        resolve_gui_root()


def test_content_type_html():
    assert content_type_for(Path("index.html")) == "text/html; charset=utf-8"


def test_content_type_javascript():
    assert content_type_for(Path("assets/x.js")) == "application/javascript"


def test_content_type_css():
    assert content_type_for(Path("assets/x.css")) == "text/css; charset=utf-8"


def test_content_type_webmanifest():
    assert content_type_for(Path("manifest.webmanifest")) == "application/manifest+json"


def test_content_type_png():
    assert content_type_for(Path("icons/x.png")) == "image/png"


def test_content_type_svg():
    assert content_type_for(Path("icons/x.svg")) == "image/svg+xml"


def test_content_type_ico():
    assert content_type_for(Path("favicon.ico")) == "image/vnd.microsoft.icon"


def test_content_type_unknown_returns_octet_stream():
    assert content_type_for(Path("mystery.xyz")) == "application/octet-stream"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_serve_gui.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bsky_saves._gui_serve'`.

- [ ] **Step 3: Implement `_gui_serve.py`**

Create `src/bsky_saves/_gui_serve.py`:

```python
"""Static-file serving for `bsky-saves serve --gui`.

All static-file logic lives here so `serve.py` stays focused on the JSON API.
The dispatcher in `serve.py` calls `serve_static_or_spa` (added in Task 6) as
one branch off the existing route table.
"""
from __future__ import annotations

from pathlib import Path


class GuiNotInstalledError(Exception):
    """The bundled GUI tarball is missing or empty.

    Raised when `bsky-saves serve --gui` is requested but `src/bsky_saves/_gui/`
    either does not exist or lacks an `index.html`. The caller (run_serve)
    should print an actionable error and exit with code 2.
    """


def _gui_root_path() -> Path:
    """Return the absolute path to the package-bundled _gui/ directory.

    Wrapped in a thin function so tests can monkeypatch it.
    """
    return Path(__file__).resolve().parent / "_gui"


def resolve_gui_root() -> Path:
    """Return the populated _gui/ directory, or raise GuiNotInstalledError."""
    root = _gui_root_path()
    if not root.exists():
        raise GuiNotInstalledError(f"{root} is missing or empty")
    if not (root / "index.html").is_file():
        raise GuiNotInstalledError(f"{root}/index.html not found")
    return root


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/vnd.microsoft.icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
}


def content_type_for(path: Path) -> str:
    """Best-effort Content-Type for a file path; falls back to octet-stream."""
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_serve_gui.py -v`
Expected: 11 PASS.

- [ ] **Step 5: Run full suite**

Run: `python -m pytest -q`
Expected: 270 passed.

- [ ] **Step 6: Commit**

```bash
git add src/bsky_saves/_gui_serve.py tests/test_serve_gui.py
git commit -m "feat(_gui_serve): module foundation (resolve_gui_root, content_type_for)"
```

---

### Task 6: `_gui_serve` — `serve_static_or_spa` core (path safety + regular file serving)

**Files:**
- Modify: `src/bsky_saves/_gui_serve.py` — add `serve_static_or_spa`.
- Modify: `tests/test_serve_gui.py` — add static-serving tests.

**Context:** The core dispatcher function. Resolves a request path against `_gui/`, defends against path traversal, serves regular files. SPA fallback and API-prefix detection come in Task 7. Cache and security headers come in Task 8.

For testability, `serve_static_or_spa` writes via a small callback interface rather than a full BaseHTTPRequestHandler. The serve.py integration in Task 10 will adapt the real handler.

Actually re-reading the spec: `serve_static_or_spa(handler, request_path)` is the right interface — it takes a handler with `_send_bytes`, `_send_json_error`, etc. methods. In tests we'll use a small stub handler.

- [ ] **Step 1: Add a `_StubHandler` test helper at the top of `tests/test_serve_gui.py`**

Add this class after the imports:

```python
class _StubHandler:
    """Minimal handler stub for testing serve_static_or_spa.

    Captures send_response / send_header / wfile.write calls so tests can
    inspect what the function would have sent over the wire.
    """

    def __init__(self):
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body: bytes = b""
        self.command: str = "GET"  # default; tests can override
        # Stub _send_json_error to record on the same object.
        self._json_errors: list[tuple[int, str]] = []

    def send_response(self, code: int) -> None:
        self.status = code

    def send_header(self, name: str, value: str) -> None:
        self.headers[name] = value

    def end_headers(self) -> None:
        pass

    @property
    def wfile(self):
        outer = self

        class _W:
            def write(self_inner, data: bytes):
                outer.body += data

        return _W()

    def _send_json_error(self, code: int, msg: str) -> None:
        self._json_errors.append((code, msg))
        self.status = code
```

- [ ] **Step 2: Add failing tests for `serve_static_or_spa`**

Append to `tests/test_serve_gui.py`:

```python
from bsky_saves._gui_serve import serve_static_or_spa


def _populate_gui_root(tmp_path):
    gui = tmp_path / "_gui"
    gui.mkdir()
    (gui / "index.html").write_bytes(b"<html>root</html>")
    (gui / "assets").mkdir()
    (gui / "assets" / "main-abc123.js").write_bytes(b"console.log(1);")
    return gui


def test_serve_static_or_spa_serves_existing_file(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()

    result = serve_static_or_spa(h, "/assets/main-abc123.js", gui)

    assert result is True
    assert h.status == 200
    assert h.body == b"console.log(1);"
    assert h.headers["Content-Type"] == "application/javascript"
    assert h.headers["Content-Length"] == str(len(b"console.log(1);"))


def test_serve_static_or_spa_serves_index_at_root(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()

    result = serve_static_or_spa(h, "/", gui)

    assert result is True
    assert h.status == 200
    assert h.body == b"<html>root</html>"
    assert h.headers["Content-Type"] == "text/html; charset=utf-8"


def test_serve_static_or_spa_rejects_path_traversal(tmp_path):
    """A path resolving outside _gui/ must 404 — never serve the target."""
    gui = _populate_gui_root(tmp_path)
    # Create a sibling file that traversal would target.
    (tmp_path / "secret.txt").write_bytes(b"SECRET")
    h = _StubHandler()

    result = serve_static_or_spa(h, "/../secret.txt", gui)

    assert result is True
    assert h.status == 404
    assert b"SECRET" not in h.body


def test_serve_static_or_spa_returns_false_for_api_prefix(tmp_path):
    """A path that looks like an undocumented API call returns False so the
    caller can send a 404 JSON error rather than the SPA index."""
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()

    result = serve_static_or_spa(h, "/api/undocumented", gui)

    assert result is False
    # No response sent.
    assert h.status is None
```

The last test (`test_serve_static_or_spa_returns_false_for_api_prefix`) anticipates Task 7's API-prefix detection. To make it pass in Task 6, the simplest implementation can treat any not-found path that starts with a recognised API prefix as `False` (no response). For now, the file is missing → existing simple behavior is the SPA fallback added in Task 7. **For Task 6, drop this test temporarily** and add it in Task 7. Remove it from the test file here.

Replace the test list with these 3 tests only (drop the api_prefix one):

```python
def test_serve_static_or_spa_serves_existing_file(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    result = serve_static_or_spa(h, "/assets/main-abc123.js", gui)
    assert result is True
    assert h.status == 200
    assert h.body == b"console.log(1);"
    assert h.headers["Content-Type"] == "application/javascript"


def test_serve_static_or_spa_serves_index_at_root(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    result = serve_static_or_spa(h, "/", gui)
    assert result is True
    assert h.status == 200
    assert h.body == b"<html>root</html>"


def test_serve_static_or_spa_rejects_path_traversal(tmp_path):
    gui = _populate_gui_root(tmp_path)
    (tmp_path / "secret.txt").write_bytes(b"SECRET")
    h = _StubHandler()
    result = serve_static_or_spa(h, "/../secret.txt", gui)
    assert result is True
    assert h.status == 404
    assert b"SECRET" not in h.body


def test_serve_static_or_spa_404_for_missing_file(tmp_path):
    """A path that doesn't exist and doesn't match the SPA pattern (added
    later) returns False so the caller can decide. For Task 6's simpler
    implementation, a clearly-non-asset missing path returns 404 directly."""
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    # Pre-SPA-fallback behaviour: missing file → 404. Task 7 changes this
    # for paths that don't look like API prefixes.
    result = serve_static_or_spa(h, "/missing.txt", gui)
    assert result is True
    assert h.status == 404
```

- [ ] **Step 3: Run tests to verify failure modes**

Run: `python -m pytest tests/test_serve_gui.py -v -k "static_or_spa"`
Expected: FAIL — function doesn't exist yet.

- [ ] **Step 4: Implement `serve_static_or_spa`**

Append to `src/bsky_saves/_gui_serve.py`:

```python
from urllib.parse import unquote, urlsplit


def serve_static_or_spa(handler, request_path: str, gui_root: Path) -> bool:
    """Try to serve a static file from gui_root for the given request path.

    Returns True if a response was sent (200 with file bytes, or 404). Returns
    False to defer to the caller (used in Task 7 for API-prefix paths that
    should yield a JSON 404 rather than an SPA fallback).

    Args:
        handler: A BaseHTTPRequestHandler-like object with send_response,
            send_header, end_headers, and a `wfile` attribute supporting write.
        request_path: The request URL path (may contain a query string).
        gui_root: The resolved _gui/ directory.
    """
    # Strip query string; URL-decode.
    parsed_path = urlsplit(request_path).path
    decoded = unquote(parsed_path)

    # Resolve candidate path. Root → index.html.
    rel = decoded.lstrip("/")
    if rel == "":
        rel = "index.html"

    candidate = (gui_root / rel).resolve()
    gui_root_resolved = gui_root.resolve()
    # Path-traversal defence: candidate must be under gui_root.
    if not str(candidate).startswith(str(gui_root_resolved) + "/") and candidate != gui_root_resolved:
        _send_404(handler)
        return True

    if candidate.is_file():
        _send_file(handler, candidate)
        return True

    # File doesn't exist. Task 7 extends this with SPA fallback + API prefix.
    _send_404(handler)
    return True


def _send_file(handler, path: Path) -> None:
    body = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type_for(path))
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if getattr(handler, "command", "GET") != "HEAD":
        handler.wfile.write(body)


def _send_404(handler) -> None:
    handler.send_response(404)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", "9")
    handler.end_headers()
    if getattr(handler, "command", "GET") != "HEAD":
        handler.wfile.write(b"not found")
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_serve_gui.py -v`
Expected: 15 PASS.

- [ ] **Step 6: Run full suite**

Run: `python -m pytest -q`
Expected: 274 passed.

- [ ] **Step 7: Commit**

```bash
git add src/bsky_saves/_gui_serve.py tests/test_serve_gui.py
git commit -m "feat(_gui_serve): serve_static_or_spa core (path safety + file serving)"
```

---

### Task 7: `_gui_serve` — SPA fallback and API-prefix detection

**Files:**
- Modify: `src/bsky_saves/_gui_serve.py` — extend `serve_static_or_spa`.
- Modify: `tests/test_serve_gui.py` — add SPA fallback tests.

**Context:** When a request path doesn't resolve to a real file:
- If it starts with a documented API prefix, return `False` so the caller can send a JSON 404 via the existing `_send_json_error` pathway.
- Otherwise, serve `index.html` as the SPA fallback (200, not 404 — the browser fragment router takes over).

- [ ] **Step 1: Add tests**

Append to `tests/test_serve_gui.py`:

```python
def test_serve_static_or_spa_returns_false_for_api_prefix(tmp_path):
    """A path that looks like an undocumented API call returns False so the
    caller (serve.py dispatcher) can send a JSON 404 rather than SPA index."""
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()

    result = serve_static_or_spa(h, "/ping-not-a-real-route", gui)
    # /ping is the documented prefix; anything starting with /ping or other
    # known API paths should yield False (deferral).
    # (Whether /ping-not-a-real-route triggers this depends on the matching
    # rule — see the implementation. The test verifies the SPA fallback is
    # NOT used for these.)
    # Looking at the spec: any path matching one of the documented API
    # routes EXACTLY or starting with one should defer. Use exact-match for
    # specificity.
    # For this test, use an unambiguous API path.
    h2 = _StubHandler()
    result2 = serve_static_or_spa(h2, "/fetch", gui)
    assert result2 is False
    assert h2.status is None


def test_serve_static_or_spa_spa_fallback_for_non_existent_route(tmp_path):
    """A path that doesn't exist and isn't an API prefix gets index.html (SPA)."""
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()

    result = serve_static_or_spa(h, "/some/spa/route", gui)

    assert result is True
    assert h.status == 200
    assert h.body == b"<html>root</html>"


def test_serve_static_or_spa_does_not_spa_fallback_for_known_api_paths(tmp_path):
    """Each documented API path should defer (return False) when no static
    file with that exact name exists."""
    gui = _populate_gui_root(tmp_path)
    for api_path in ["/ping", "/fetch-image", "/extract-article", "/fetch", "/enrich", "/hydrate-threads"]:
        h = _StubHandler()
        result = serve_static_or_spa(h, api_path, gui)
        assert result is False, f"expected deferral for {api_path}"
```

Remove the older `test_serve_static_or_spa_404_for_missing_file` test (added in Task 6) — its behaviour changes with the SPA fallback. Replace it with:

```python
def test_serve_static_or_spa_404_replaced_by_spa_fallback(tmp_path):
    """Missing non-API path now triggers SPA fallback (Task 7); was 404 in Task 6."""
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    result = serve_static_or_spa(h, "/missing.txt", gui)
    assert result is True
    assert h.status == 200  # was 404 in Task 6
    assert h.body == b"<html>root</html>"
```

Actually, `/missing.txt` looks more like a static asset than an SPA route. The cleanest test is to remove the old 404-test and have only the new SPA fallback test for `/some/spa/route`. Drop `test_serve_static_or_spa_404_replaced_by_spa_fallback` and `test_serve_static_or_spa_404_for_missing_file`.

So final tests for Task 7:
- `test_serve_static_or_spa_returns_false_for_api_prefix`
- `test_serve_static_or_spa_spa_fallback_for_non_existent_route`
- `test_serve_static_or_spa_does_not_spa_fallback_for_known_api_paths`

- [ ] **Step 2: Run tests to verify failure modes**

Run: `python -m pytest tests/test_serve_gui.py -v`
Expected: 3 new tests fail; the old 404-for-missing test still passes (since the implementation hasn't changed yet).

- [ ] **Step 3: Extend `serve_static_or_spa` in `_gui_serve.py`**

Update the existing `serve_static_or_spa` in `src/bsky_saves/_gui_serve.py`. Add a module-level constant for the documented API paths:

```python
# Documented API paths from serve.py's ROUTES table. Listed here for SPA
# fallback decisions: if a GET request matches one of these and isn't a real
# file in _gui/, defer to the caller's 404 path rather than serving index.html.
# This list MUST be kept in sync with serve.py's ROUTES.
_API_PATHS = frozenset({
    "/ping",
    "/fetch-image",
    "/extract-article",
    "/fetch",
    "/enrich",
    "/hydrate-threads",
})
```

Modify the function:

```python
def serve_static_or_spa(handler, request_path: str, gui_root: Path) -> bool:
    parsed_path = urlsplit(request_path).path
    decoded = unquote(parsed_path)

    rel = decoded.lstrip("/")
    if rel == "":
        rel = "index.html"

    candidate = (gui_root / rel).resolve()
    gui_root_resolved = gui_root.resolve()
    if not str(candidate).startswith(str(gui_root_resolved) + "/") and candidate != gui_root_resolved:
        _send_404(handler)
        return True

    if candidate.is_file():
        _send_file(handler, candidate)
        return True

    # File doesn't exist. Two cases:
    # 1. Documented API path: defer (False) — caller sends JSON 404.
    # 2. SPA route: serve index.html (200) so the GUI's router takes over.
    if decoded in _API_PATHS:
        return False

    index = gui_root / "index.html"
    _send_file(handler, index)
    return True
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_serve_gui.py -v`
Expected: 17 PASS (15 from Task 6 minus 1 dropped + 3 new = 17 total).

- [ ] **Step 5: Run full suite**

Run: `python -m pytest -q`
Expected: 276 passed.

- [ ] **Step 6: Commit**

```bash
git add src/bsky_saves/_gui_serve.py tests/test_serve_gui.py
git commit -m "feat(_gui_serve): SPA fallback to index.html; defer API paths"
```

---

### Task 8: `_gui_serve` — cache-control, security headers, HEAD support

**Files:**
- Modify: `src/bsky_saves/_gui_serve.py` — extend `_send_file` with cache + security headers; add HEAD-aware writes.
- Modify: `tests/test_serve_gui.py` — add tests for headers and HEAD.

**Context:** Final layer on `_gui_serve`. Adds the three cache-control classes (immutable for `/assets/*`, no-store for index, no-cache for everything else) and the four security headers (CSP, X-Frame-Options, Referrer-Policy, COOP). HEAD support: send same headers, write zero body bytes.

- [ ] **Step 1: Add failing tests**

Append to `tests/test_serve_gui.py`:

```python
# Expected CSP value (from MVP spec §4.6). Pin exactly.
_EXPECTED_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'wasm-unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self'; "
    "connect-src 'self' https: http://127.0.0.1:* http://localhost:*; "
    "worker-src 'self' blob:; "
    "manifest-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def test_serve_static_or_spa_assets_get_immutable_cache(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/assets/main-abc123.js", gui)
    assert h.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_serve_static_or_spa_index_gets_no_store(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/", gui)
    assert h.headers["Cache-Control"] == "no-store"


def test_serve_static_or_spa_spa_fallback_gets_no_store(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/some/spa/route", gui)
    assert h.headers["Cache-Control"] == "no-store"


def test_serve_static_or_spa_other_static_gets_no_cache(tmp_path):
    gui = _populate_gui_root(tmp_path)
    (gui / "manifest.webmanifest").write_bytes(b'{"name":"x"}')
    h = _StubHandler()
    serve_static_or_spa(h, "/manifest.webmanifest", gui)
    assert h.headers["Cache-Control"] == "no-cache"


def test_serve_static_or_spa_sends_csp(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/", gui)
    assert h.headers["Content-Security-Policy"] == _EXPECTED_CSP


def test_serve_static_or_spa_sends_x_frame_options(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/", gui)
    assert h.headers["X-Frame-Options"] == "DENY"


def test_serve_static_or_spa_sends_referrer_policy(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/", gui)
    assert h.headers["Referrer-Policy"] == "no-referrer"


def test_serve_static_or_spa_sends_coop(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    serve_static_or_spa(h, "/", gui)
    assert h.headers["Cross-Origin-Opener-Policy"] == "same-origin"


def test_serve_static_or_spa_head_sends_headers_no_body(tmp_path):
    gui = _populate_gui_root(tmp_path)
    h = _StubHandler()
    h.command = "HEAD"
    serve_static_or_spa(h, "/assets/main-abc123.js", gui)
    assert h.status == 200
    assert h.headers["Content-Type"] == "application/javascript"
    assert h.headers["Content-Length"] == str(len(b"console.log(1);"))
    assert h.body == b""  # HEAD writes no body
```

- [ ] **Step 2: Run tests to verify failure modes**

Run: `python -m pytest tests/test_serve_gui.py -v -k "cache or csp or x_frame or referrer or coop or head"`
Expected: FAIL — none of these headers are emitted yet.

- [ ] **Step 3: Extend `_send_file` in `_gui_serve.py`**

Add a module-level constant near `_API_PATHS`:

```python
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'wasm-unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self'; "
    "connect-src 'self' https: http://127.0.0.1:* http://localhost:*; "
    "worker-src 'self' blob:; "
    "manifest-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
```

Add a helper to compute the cache-control class for a path:

```python
def _cache_control_for(rel_path: str, *, is_spa_fallback: bool) -> str:
    """Return the Cache-Control value for a served file.

    - SPA fallback or index.html: no-store (must always revalidate).
    - /assets/*: immutable (Vite-hashed filenames).
    - Everything else: no-cache (revalidate per request).
    """
    if is_spa_fallback or rel_path == "index.html":
        return "no-store"
    if rel_path.startswith("assets/"):
        return "public, max-age=31536000, immutable"
    return "no-cache"
```

Refactor `_send_file` to take the rel_path and SPA flag:

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
    handler.send_header("Content-Type", content_type_for(path))
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", _cache_control_for(rel_path, is_spa_fallback=is_spa_fallback))
    handler.send_header("Content-Security-Policy", _CSP)
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Cross-Origin-Opener-Policy", "same-origin")
    handler.end_headers()
    if getattr(handler, "command", "GET") != "HEAD":
        handler.wfile.write(body)
```

Update the two call sites in `serve_static_or_spa`:

```python
def serve_static_or_spa(handler, request_path: str, gui_root: Path) -> bool:
    parsed_path = urlsplit(request_path).path
    decoded = unquote(parsed_path)

    rel = decoded.lstrip("/")
    if rel == "":
        rel = "index.html"

    candidate = (gui_root / rel).resolve()
    gui_root_resolved = gui_root.resolve()
    if not str(candidate).startswith(str(gui_root_resolved) + "/") and candidate != gui_root_resolved:
        _send_404(handler)
        return True

    if candidate.is_file():
        _send_file(handler, candidate, rel_path=rel, is_spa_fallback=False)
        return True

    if decoded in _API_PATHS:
        return False

    index = gui_root / "index.html"
    _send_file(handler, index, rel_path="index.html", is_spa_fallback=True)
    return True
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_serve_gui.py -v`
Expected: 26 PASS (17 + 9 new).

- [ ] **Step 5: Run full suite**

Run: `python -m pytest -q`
Expected: 285 passed.

- [ ] **Step 6: Commit**

```bash
git add src/bsky_saves/_gui_serve.py tests/test_serve_gui.py
git commit -m "feat(_gui_serve): cache-control classes + security headers + HEAD support"
```

---

## Phase 4 — serve.py integration

### Task 9: `--gui` CLI flag + thread `gui_root` through `make_handler`/`run_serve` with startup guard

**Files:**
- Modify: `src/bsky_saves/cli.py` — add `--gui` flag.
- Modify: `src/bsky_saves/serve.py` — add `gui_root` parameter; startup guard in `run_serve`.
- Modify: `tests/test_serve.py` — add tests for the startup guard.

**Context:** This task wires `--gui` from the CLI into `run_serve`, including the refuse-to-start guard when `_gui/` is missing. The dispatcher integration (actually routing requests through `_gui_serve`) comes in Task 10.

- [ ] **Step 1: Add failing tests**

Add to `tests/test_serve.py`:

```python
def test_run_serve_with_gui_missing_returns_2(tmp_path, monkeypatch, capsys):
    """run_serve(gui=True) with empty _gui/ exits 2 with actionable message."""
    from bsky_saves.serve import run_serve
    from bsky_saves import _gui_serve

    # Point _gui_root_path at an empty directory.
    empty_gui = tmp_path / "_gui"
    empty_gui.mkdir()
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: empty_gui)

    exit_code = run_serve(port=0, gui=True)
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--gui requires" in captured.err
    assert "fetch_gui.py" in captured.err


def test_run_serve_without_gui_does_not_check_gui_root(tmp_path, monkeypatch):
    """run_serve(gui=False) does not call resolve_gui_root; empty _gui/ is fine."""
    from bsky_saves.serve import run_serve

    # Without --gui, the server should bind successfully (we exit it
    # immediately via port=0 → ephemeral, then SIGINT). Easiest: just
    # call resolve_gui_root NOT being invoked is the test's real point.
    # We'll spy on it.
    from bsky_saves import _gui_serve
    calls = []
    monkeypatch.setattr(
        _gui_serve, "resolve_gui_root",
        lambda: calls.append("called") or empty_gui,
    )
    # Note: gui=False means resolve_gui_root never called.
    # To run without starting a daemon, we'd need to patch
    # ThreadingHTTPServer. Simpler: assert that run_serve doesn't error on
    # an undefined empty_gui by skipping the actual server start.

    # Actually the cleanest test is just: make_handler accepts gui_root=None
    # without raising. Let's test that instead.
    from bsky_saves.serve import make_handler
    handler_cls = make_handler(port=1, allow_origins=["https://x"], gui_root=None)
    assert handler_cls is not None  # cls was constructed without GUI mount
```

The second test is awkward — let me simplify. Use a separate test for `make_handler`:

```python
def test_make_handler_accepts_gui_root_none():
    """make_handler should accept gui_root=None (the default behavior)."""
    from bsky_saves.serve import make_handler
    handler_cls = make_handler(port=1, allow_origins=["https://x"], gui_root=None)
    assert handler_cls is not None


def test_make_handler_accepts_gui_root_path(tmp_path):
    """make_handler accepts a populated _gui/ path."""
    from bsky_saves.serve import make_handler
    handler_cls = make_handler(
        port=1, allow_origins=["https://x"], gui_root=tmp_path
    )
    assert handler_cls is not None
```

Use only these and the startup-guard test.

- [ ] **Step 2: Run tests to verify failure modes**

Run: `python -m pytest tests/test_serve.py -v -k "run_serve_with_gui or make_handler_accepts_gui"`
Expected: FAIL — `make_handler` doesn't accept `gui_root`; `run_serve` doesn't accept `gui`.

- [ ] **Step 3: Update `make_handler` signature in `serve.py`**

Find `def make_handler(...)` (currently has `port`, `allow_origins`, `verbose`). Add `gui_root` as a keyword-only param:

```python
def make_handler(
    *,
    port: int,
    allow_origins: list[str],
    verbose: bool = False,
    gui_root: Path | None = None,
) -> type[BaseHTTPRequestHandler]:
```

Add `from pathlib import Path` to imports if not already present.

Inside the function, capture `gui_root` in the closure (no other behavior change yet — Task 10 wires it into the dispatcher):

```python
    origins = list(allow_origins)
    gui = gui_root  # closure capture; used by Handler._dispatch in Task 10
```

- [ ] **Step 4: Update `run_serve` signature and add startup guard**

Find `def run_serve(...)` (currently has `port`, `allow_origins`, `verbose`). Add `gui` param:

```python
def run_serve(
    *,
    port: int = 47826,
    allow_origins: list[str] | None = None,
    verbose: bool = False,
    gui: bool = False,
) -> int:
```

Near the top of the function body (before the existing handler/server construction), add the guard:

```python
    gui_root: Path | None = None
    if gui:
        from ._gui_serve import GuiNotInstalledError, resolve_gui_root
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
```

Then update the `make_handler(...)` call:

```python
    handler_cls = make_handler(
        port=port, allow_origins=origins, verbose=verbose, gui_root=gui_root
    )
```

- [ ] **Step 5: Add `--gui` flag to `cli.py`**

In `src/bsky_saves/cli.py`, find the `p_serve` argument-parser block. Add:

```python
    p_serve.add_argument(
        "--gui",
        action="store_true",
        default=False,
        help="Also serve the bundled GUI from / on the same port. "
             "Requires the wheel-bundled _gui/ tree (or a local fetch via "
             "scripts/fetch_gui.py).",
    )
```

Find the `run_serve(...)` call in the `serve` branch of the dispatcher (likely around `args.cmd == "serve"`) and pass `gui=args.gui`:

```python
        return run_serve(
            port=args.port,
            allow_origins=args.allow_origin,
            verbose=args.verbose,
            gui=args.gui,
        )
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_serve.py -v -k "run_serve_with_gui or make_handler_accepts_gui"`
Expected: 3 PASS.

- [ ] **Step 7: Run full suite**

Run: `python -m pytest -q`
Expected: 288 passed.

- [ ] **Step 8: Commit**

```bash
git add src/bsky_saves/cli.py src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): --gui CLI flag and startup guard for missing _gui/"
```

---

### Task 10: Dispatcher integration — route GET/HEAD through `_gui_serve` when `--gui`

**Files:**
- Modify: `src/bsky_saves/serve.py` — add static-file branch in `_dispatch`.
- Modify: `tests/test_serve.py` — add end-to-end tests for the GUI mount.

**Context:** The final wiring. When `--gui` is on AND the method is GET or HEAD AND the path doesn't match an exact API route, the dispatcher delegates to `_gui_serve.serve_static_or_spa`. API routes still take precedence.

- [ ] **Step 1: Add failing tests**

Add to `tests/test_serve.py`:

```python
def _populate_gui_for_serve_test(tmp_path):
    """Set up a minimal _gui/ that monkeypatched resolve_gui_root can return."""
    gui = tmp_path / "_gui"
    gui.mkdir()
    (gui / "index.html").write_bytes(b"<html>integration</html>")
    (gui / "assets").mkdir()
    (gui / "assets" / "main-deadbeef.js").write_bytes(b"console.log('integration');")
    return gui


def test_serve_with_gui_mounts_index_at_root(tmp_path, monkeypatch):
    gui = _populate_gui_for_serve_test(tmp_path)
    from bsky_saves import _gui_serve
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui)

    with serve_in_background(gui=True) as (port, _):
        status, headers, body = _request(port, "/")

    assert status == 200
    assert body == b"<html>integration</html>"
    assert headers["Content-Type"].startswith("text/html")
    assert headers["Cache-Control"] == "no-store"


def test_serve_with_gui_serves_assets(tmp_path, monkeypatch):
    gui = _populate_gui_for_serve_test(tmp_path)
    from bsky_saves import _gui_serve
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui)

    with serve_in_background(gui=True) as (port, _):
        status, headers, body = _request(port, "/assets/main-deadbeef.js")

    assert status == 200
    assert body == b"console.log('integration');"
    assert headers["Content-Type"] == "application/javascript"
    assert headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_serve_with_gui_spa_fallback(tmp_path, monkeypatch):
    gui = _populate_gui_for_serve_test(tmp_path)
    from bsky_saves import _gui_serve
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui)

    with serve_in_background(gui=True) as (port, _):
        status, _, body = _request(port, "/some/spa/route")

    assert status == 200
    assert body == b"<html>integration</html>"


def test_serve_with_gui_api_precedence(tmp_path, monkeypatch):
    """Even with --gui, /ping returns JSON, not HTML."""
    gui = _populate_gui_for_serve_test(tmp_path)
    from bsky_saves import _gui_serve
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui)

    with serve_in_background(gui=True) as (port, _):
        status, headers, body = _request(port, "/ping")

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert b"bsky-saves" in body


def test_serve_with_gui_post_to_root_is_404(tmp_path, monkeypatch):
    """POST /  is not a real API route and shouldn't serve static files."""
    gui = _populate_gui_for_serve_test(tmp_path)
    from bsky_saves import _gui_serve
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui)

    with serve_in_background(gui=True) as (port, _):
        status, _, _ = _request(
            port, "/",
            method="POST",
            headers={"Content-Type": "application/json", "Origin": DEFAULT_ORIGIN},
            body=b"{}",
        )

    assert status == 404


def test_serve_without_gui_root_is_404(tmp_path):
    """Without --gui, GET / returns the existing 404 (no static branch)."""
    with serve_in_background() as (port, _):
        status, _, _ = _request(port, "/")
    assert status == 404


def test_serve_with_gui_unknown_api_path_404(tmp_path, monkeypatch):
    """An undocumented API-looking path returns the JSON 404, not SPA index."""
    gui = _populate_gui_for_serve_test(tmp_path)
    from bsky_saves import _gui_serve
    monkeypatch.setattr(_gui_serve, "_gui_root_path", lambda: gui)

    with serve_in_background(gui=True) as (port, _):
        status, headers, body = _request(port, "/fetch-image")

    # /fetch-image is a documented POST route. GET to it falls through to the
    # _gui_serve deferral (api prefix) and lands at the JSON 404 path in the
    # dispatcher.
    assert status == 404
    assert headers["Content-Type"] == "application/json"
```

You'll also need to update `serve_in_background` in `tests/test_serve.py` to accept and pass through a `gui` kwarg. Find the existing helper and add the parameter:

```python
@contextmanager
def serve_in_background(*, allow_origins=None, verbose=False, gui=False):
    # ... existing setup ...
    handler_cls = make_handler(port=port, allow_origins=origins, verbose=verbose, gui_root=gui_root)
    # where gui_root is computed from resolve_gui_root() if gui else None
```

The exact shape depends on the current helper. Locate it and adapt. Pseudocode:

```python
@contextmanager
def serve_in_background(*, allow_origins=(DEFAULT_ORIGIN,), verbose=False, gui=False):
    # Bind an ephemeral port FIRST to know the port for the handler closure.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    if gui:
        from bsky_saves._gui_serve import resolve_gui_root
        gui_root = resolve_gui_root()  # monkeypatch in tests
    else:
        gui_root = None

    handler_cls = make_handler(
        port=port, allow_origins=list(allow_origins), verbose=verbose, gui_root=gui_root
    )
    # ... existing server start logic ...
```

Adjust to match the current helper's exact structure.

- [ ] **Step 2: Run tests to verify failure modes**

Run: `python -m pytest tests/test_serve.py -v -k "serve_with_gui or without_gui_root"`
Expected: most FAIL — dispatcher doesn't route to `_gui_serve` yet.

- [ ] **Step 3: Update `Handler._dispatch` (and/or related entry points) in `serve.py`**

Inside the `Handler` class returned by `make_handler`, find `_dispatch` (the route-dispatch method). Modify it to consult the static-file branch after ROUTES lookup:

```python
        def _dispatch(self, method: str) -> None:
            # Existing route table lookup (API takes precedence).
            handler = ROUTES.get((method, self.path))
            if handler is not None:
                handler(self)
                return

            # Static-file branch (only for GET and HEAD when --gui is on).
            if gui is not None and method in ("GET", "HEAD"):
                from ._gui_serve import serve_static_or_spa
                if serve_static_or_spa(self, self.path, gui):
                    return

            # Fall through to the existing 404 JSON error.
            self._send_json_error(404, "not found")
```

(The exact identifier for the captured gui_root in the closure depends on Task 9's implementation. If Task 9 named the closure variable `gui` and it can be `None` or a `Path`, the check is `if gui is not None and ...`. If it named it differently, adapt.)

Also: for the HEAD-when-no-GET-handler case, the existing `__getattr__` fallback handles unknown verbs. Verify that HEAD requests reach `_dispatch` with `method="HEAD"`. If the existing `do_HEAD` is not defined, the `__getattr__` fallback should route it through `_dispatch` (which is also called from `do_GET`). Confirm and adapt.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_serve.py -v -k "serve_with_gui or without_gui_root"`
Expected: ALL PASS.

- [ ] **Step 5: Run full serve suite**

Run: `python -m pytest tests/test_serve.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Run full suite**

Run: `python -m pytest -q`
Expected: 295 passed.

- [ ] **Step 7: Commit**

```bash
git add src/bsky_saves/serve.py tests/test_serve.py
git commit -m "feat(serve): integrate _gui_serve into dispatcher (GET/HEAD; API precedence)"
```

---

## Phase 5 — drive-bys

### Task 11: Remove unused `import httpx` from `images.py`

**Files:**
- Modify: `src/bsky_saves/images.py`

**Context:** v0.4.4 Task 8 switched `download_to` from `httpx.get` to `safe_http_get`. The `import httpx` line at the top of `images.py` is now dead code. The v0.4.4 spec deferred removal; v0.5.0 picks it up.

- [ ] **Step 1: Verify httpx is unused**

Run: `grep -n "httpx" src/bsky_saves/images.py`
Expected: one line — the `import httpx` itself. If there are any other `httpx.X` references, stop and report — the import is still in use.

- [ ] **Step 2: Delete the import**

Edit `src/bsky_saves/images.py`. Remove the line `import httpx`.

- [ ] **Step 3: Run image tests**

Run: `python -m pytest tests/test_images.py -v`
Expected: ALL PASS (no behavior change).

- [ ] **Step 4: Run full suite**

Run: `python -m pytest -q`
Expected: 295 passed (unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/bsky_saves/images.py
git commit -m "chore(images): remove unused httpx import"
```

---

### Task 12: Bump `threads.py` User-Agent to derive from `__version__`

**Files:**
- Modify: `src/bsky_saves/threads.py`

**Context:** `threads.py` may contain a stale `User-Agent` literal similar to the ones v0.4.4 fixed in `articles.py` and `images.py`. Same pattern: derive from `__version__`. If `threads.py` doesn't currently set a custom UA, this task is a no-op — verify first.

- [ ] **Step 1: Check for any User-Agent literal**

Run: `grep -n "User-Agent\|user_agent\|bsky-saves/" src/bsky_saves/threads.py`
Expected: identifies any UA-related code. If none exists, this task is a no-op — skip to Step 4 and commit an empty change (or skip the task).

- [ ] **Step 2: Replace literal with `__version__`-derived f-string**

If a literal like `"bsky-saves/0.1 ..."` is found, replace it. Pattern (same as v0.4.4 Tasks 9):

```python
from . import __version__

DEFAULT_USER_AGENT = f"bsky-saves/{__version__} (+https://github.com/tenorune/bsky-saves)"
```

Place the `from . import __version__` with other relative imports near the top of the file.

- [ ] **Step 3: Run threads tests**

Run: `python -m pytest tests/test_threads.py -v`
Expected: ALL PASS.

- [ ] **Step 4: Add sanity-check test (only if Step 2 made changes)**

If a `DEFAULT_USER_AGENT` constant now exists, append a test to `tests/test_version.py`:

```python
def test_threads_user_agent_contains_version():
    from bsky_saves import __version__
    from bsky_saves.threads import DEFAULT_USER_AGENT
    assert __version__ in DEFAULT_USER_AGENT
```

If no `DEFAULT_USER_AGENT` exists in `threads.py`, skip this step.

- [ ] **Step 5: Run full suite**

Run: `python -m pytest -q`
Expected: 295 or 296 passed (depending on whether Step 4 added a test).

- [ ] **Step 6: Commit (only if there were changes)**

```bash
git add src/bsky_saves/threads.py tests/test_version.py
git commit -m "refactor(threads): derive User-Agent from __version__"
```

If `threads.py` had no UA literal, skip the commit and report DONE_WITH_CONCERNS noting "no UA literal found; task is a no-op."

---

## Phase 6 — Real GUI release coordination

### Task 13: Populate real `gui-dist.sha256` against v0.5.3 GUI release

**Files:**
- Modify: `gui-dist.sha256` (replace placeholder with real v0.5.3 value).
- Side-effect: `src/bsky_saves/_gui/` will be populated locally for the first time.

**Context:** This is the manual coordination point with the GUI team. By this task, the bsky-saves implementation is complete and all tests pass against mocked GUI bundles. To unblock the remaining build-system and CI tasks, the real `bsky-saves-gui v0.5.3` release must be live with `dist.tar.gz` and `dist.tar.gz.sha256` attached.

**Coordination protocol:**
1. Stop and signal the project owner that v0.5.3 GUI release is needed now.
2. Wait for confirmation that the release exists at `https://github.com/tenorune/bsky-saves-gui/releases/tag/v0.5.3`.
3. Fetch the real `dist.tar.gz.sha256` file from the release.
4. Replace the placeholder in `gui-dist.sha256`.
5. Run `python scripts/fetch_gui.py` against the live release; verify `_gui/` populates with a working bundle.

- [ ] **Step 1: Signal coordination point**

Report status `BLOCKED` with message: "Task 13 needs the bsky-saves-gui v0.5.3 release to be live with dist.tar.gz and dist.tar.gz.sha256 attached. Project owner needs to trigger that release before this task can proceed."

When the project owner confirms v0.5.3 is released, continue with Step 2.

- [ ] **Step 2: Fetch the real sha256 file**

Run:

```bash
curl -sSL https://github.com/tenorune/bsky-saves-gui/releases/download/v0.5.3/dist.tar.gz.sha256 -o /tmp/gui-dist.sha256
cat /tmp/gui-dist.sha256
```

Expected output: a single line of the form `<64-hex>  dist.tar.gz`.

- [ ] **Step 3: Replace the placeholder**

```bash
cp /tmp/gui-dist.sha256 gui-dist.sha256
cat gui-dist.sha256
```

Verify the file matches the GitHub release's `.sha256` byte-for-byte.

- [ ] **Step 4: Run fetch script against the live release**

Run: `python scripts/fetch_gui.py`
Expected: prints `bsky-saves: vendored GUI bundle v0.5.3 (<sha-prefix>...) → src/bsky_saves/_gui` to stderr. Exit code 0.

- [ ] **Step 5: Verify `_gui/` populated correctly**

Run: `ls src/bsky_saves/_gui/`
Expected: at minimum `index.html`, `assets/`, `manifest.webmanifest`, plus a `.gui-version` marker.

Run: `cat src/bsky_saves/_gui/.gui-version`
Expected: two lines: `0.5.3` and the sha256 hex.

Run: `cat src/bsky_saves/_gui/index.html | head -5`
Expected: HTML starting with `<!doctype html>` or `<!DOCTYPE html>`, followed by `<html ...>` and a `<title>` containing something GUI-related.

- [ ] **Step 6: Run full test suite to confirm no regression**

Run: `python -m pytest -q`
Expected: 295 (or 296 if Task 12 added a test) passed. The real `_gui/` content doesn't affect any tests because they all use monkeypatched roots.

- [ ] **Step 7: Commit**

Note: `src/bsky_saves/_gui/` is gitignored, so only `gui-dist.sha256` is committed.

```bash
git add gui-dist.sha256
git commit -m "build: pin GUI bundle to v0.5.3 (real sha256)"
```

Report status DONE with the actual sha256 value pasted into the report for the controller's records.

---

## Phase 7 — Build + CI wiring

### Task 14: Hatch build hook + pyproject.toml wiring

**Files:**
- Create: `hatch_build.py` at repo root.
- Modify: `pyproject.toml` — add `[tool.hatch.build.hooks.custom]` and `[tool.hatch.build] artifacts`.

**Context:** Now that the real release exists and `fetch_gui.py` works end-to-end, wire it into the build system. After this task, `python -m build` automatically vendors `_gui/` and packages it into the wheel.

- [ ] **Step 1: Create `hatch_build.py` at repo root**

```python
"""Hatch custom build hook that vendors the GUI tarball before packaging.

Wires scripts/fetch_gui.py into `python -m build` and `pip install .` from
sdist. The wheel includes the populated src/bsky_saves/_gui/ tree via
[tool.hatch.build] artifacts (see pyproject.toml).
"""
from __future__ import annotations

import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class GuiBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        # Make scripts/ importable when this hook runs.
        sys.path.insert(0, str(Path(self.root)))
        from scripts.fetch_gui import fetch_gui

        fetch_gui(Path(self.root))
```

- [ ] **Step 2: Wire in `pyproject.toml`**

Add to `pyproject.toml`:

```toml
[tool.hatch.build.hooks.custom]
path = "hatch_build.py"

[tool.hatch.build]
# _gui/ is gitignored (vendored at build time); explicitly include it in the wheel.
artifacts = ["src/bsky_saves/_gui/**"]
```

Place these tables wherever consistent with existing `[tool.hatch.*]` config. If `[tool.hatch.build]` already exists, merge the `artifacts` key into it.

- [ ] **Step 3: Build the wheel to verify**

```bash
rm -rf dist/ build/ src/bsky_saves.egg-info/
python -m build 2>&1 | tail -20
```

Expected: build succeeds; `dist/bsky_saves-0.4.4-*.whl` and `.tar.gz` produced. (Version is still 0.4.4 — Task 17 bumps to 0.5.0.)

The build log should include the line from `fetch_gui` indicating the GUI bundle was vendored (or skipped due to idempotency).

- [ ] **Step 4: Inspect the wheel contents**

```bash
python -m zipfile -l dist/bsky_saves-*.whl | grep _gui | head -10
```

Expected: lines showing `bsky_saves/_gui/index.html`, `bsky_saves/_gui/assets/*.js`, etc. If `_gui/` is NOT in the wheel, the `artifacts` config didn't apply — investigate.

- [ ] **Step 5: Smoke-install the wheel**

```bash
python -m venv /tmp/v-task14
/tmp/v-task14/bin/pip install dist/bsky_saves-*-py3-none-any.whl
ls /tmp/v-task14/lib/python*/site-packages/bsky_saves/_gui/ | head -5
```

Expected: `_gui/` directory is present in the installed package with `index.html` etc.

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest -q`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add hatch_build.py pyproject.toml
git commit -m "build(hatch): vendor GUI tarball + include _gui/ in wheel"
```

---

### Task 15: Add `python scripts/fetch_gui.py` to `verify.yml` CI

**Files:**
- Modify: `.github/workflows/verify.yml`

**Context:** PR CI runs pytest on every push. Before pytest, fetch the GUI bundle so the build is fully reproducible and any pin mismatch surfaces at PR time.

- [ ] **Step 1: Inspect current verify.yml**

Run: `cat .github/workflows/verify.yml`

Note the existing step order: typically checkout → setup-python → pip install -e . → pytest. The new step (`python scripts/fetch_gui.py`) goes after dependencies are installed and before pytest. Actually since `fetch_gui.py` uses only stdlib, it can run right after checkout + Python setup, before `pip install`. The earlier the better for clearer failure attribution.

- [ ] **Step 2: Add the fetch step**

Edit `.github/workflows/verify.yml`. Add this step after `actions/setup-python`:

```yaml
      - name: Fetch GUI bundle
        run: python scripts/fetch_gui.py
```

The step uses no special inputs and has no `env` requirements; it reads from the repo's pinned `gui_version` and `gui-dist.sha256`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/verify.yml
git commit -m "ci(verify): fetch GUI bundle before pytest"
```

Note: this commit's effect is only visible on GitHub once the PR is pushed. Local testing isn't possible without simulating Actions. Trust the YAML syntax (it's a straight copy of the existing step pattern) and move on. If CI fails on push, we'll fix it in a follow-up.

---

## Phase 8 — Pre-release smoke test

### Task 16: New `.github/workflows/smoke.yml`

**Files:**
- Create: `.github/workflows/smoke.yml`

**Context:** Pre-release smoke test that exercises the full end-to-end path: build wheel, install in venv, start daemon with `--gui`, hit four endpoints. Plus a `workflow_dispatch` input to optionally corrupt the pin (for acceptance criterion 8).

- [ ] **Step 1: Create the workflow**

Create `.github/workflows/smoke.yml`:

```yaml
name: smoke

on:
  push:
    branches: [main]
    tags: ['v*']
  workflow_dispatch:
    inputs:
      gui_pin_corrupt:
        description: 'Corrupt gui-dist.sha256 (negative-pin test; build should fail)'
        type: boolean
        default: false

jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Optionally corrupt the GUI pin
        if: ${{ github.event.inputs.gui_pin_corrupt == 'true' }}
        run: |
          # Flip the first hex character so SHA-256 verification fails.
          sed -i 's/^./0/' gui-dist.sha256
          cat gui-dist.sha256

      - name: Build the wheel
        run: |
          python -m pip install --upgrade pip build
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

      - name: Smoke /  endpoint
        run: |
          curl -fsS http://127.0.0.1:47826/ | grep -q '<title>'

      - name: Smoke /ping endpoint
        run: |
          curl -fsS http://127.0.0.1:47826/ping | \
            python -c 'import sys, json; d = json.load(sys.stdin); assert d["name"] == "bsky-saves", d'

      - name: Smoke /assets/* endpoint
        run: |
          ASSET_DIR="/tmp/smoke/lib/python3.11/site-packages/bsky_saves/_gui/assets"
          ASSET=$(ls "$ASSET_DIR" | head -1)
          echo "Testing /assets/$ASSET"
          curl -fsS "http://127.0.0.1:47826/assets/$ASSET" > /dev/null

      - name: Smoke /fetch-image rejection
        run: |
          STATUS=$(curl -sS -o /dev/null -w "%{http_code}" -X POST \
              http://127.0.0.1:47826/fetch-image \
              -H 'Content-Type: application/json' \
              -H "Origin: http://127.0.0.1:47826" \
              -d '{"url":"https://evil.com/x.png"}')
          test "$STATUS" = "400" || { echo "expected 400, got $STATUS"; exit 1; }

      - name: Kill daemon
        if: always()
        run: kill "$(cat /tmp/smoke-pid)" || true
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/smoke.yml
git commit -m "ci(smoke): pre-release smoke test for --gui + negative-pin variant"
```

---

## Phase 9 — Release

### Task 17: Bump version to 0.5.0 + final test + manual smoke

**Files:**
- Modify: `pyproject.toml` — bump `version = "0.5.0"`.

**Context:** Final task. Bump version, run full test suite, build wheel, do a local smoke test, push.

- [ ] **Step 1: Bump version in pyproject.toml**

Find:

```toml
[project]
name = "bsky-saves"
version = "0.4.4"
```

Change to:

```toml
[project]
name = "bsky-saves"
version = "0.5.0"
```

- [ ] **Step 2: Reinstall editable package so `__version__` updates**

```bash
pip install -e . --quiet
python -c "import bsky_saves; print(bsky_saves.__version__)"
```

Expected output: `0.5.0`.

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest -q`
Expected: all tests pass (final count ~295 or so depending on exact test additions).

- [ ] **Step 4: Build wheel**

```bash
rm -rf dist/ build/ src/bsky_saves.egg-info/
python -m build 2>&1 | tail -10
```

Expected: `dist/bsky_saves-0.5.0-py3-none-any.whl` and `dist/bsky_saves-0.5.0.tar.gz` exist.

- [ ] **Step 5: Manual smoke test against the wheel**

```bash
python -m venv /tmp/v-050-smoke
/tmp/v-050-smoke/bin/pip install dist/bsky_saves-0.5.0-py3-none-any.whl
/tmp/v-050-smoke/bin/python -c "import bsky_saves; print(bsky_saves.__version__)"
```

Expected output: `0.5.0`.

Start the daemon:

```bash
/tmp/v-050-smoke/bin/bsky-saves serve --gui --port 47900 &
SERVE_PID=$!
sleep 1
```

Hit the endpoints:

```bash
echo "=== / (index.html) ==="
curl -fsS http://127.0.0.1:47900/ | head -5

echo "=== /ping ==="
curl -fsS http://127.0.0.1:47900/ping

echo "=== /assets/<hashed> ==="
ASSET=$(ls /tmp/v-050-smoke/lib/python*/site-packages/bsky_saves/_gui/assets/ | head -1)
curl -fsS "http://127.0.0.1:47900/assets/$ASSET" | wc -c

echo "=== SPA fallback ==="
curl -fsS http://127.0.0.1:47900/some/spa/route | head -3

echo "=== API precedence (POST /fetch-image) ==="
curl -sS -X POST http://127.0.0.1:47900/fetch-image \
    -H 'Content-Type: application/json' \
    -H "Origin: http://127.0.0.1:47900" \
    -d '{"url":"https://evil.com/x.png"}'

echo "=== Security headers on / ==="
curl -sI http://127.0.0.1:47900/ | grep -iE "csp|content-security|x-frame|referrer|coop|cache-control" 

kill $SERVE_PID
```

Verify visually:
- `/` returns the HTML index with a `<title>`.
- `/ping` returns JSON with `version: "0.5.0"`.
- `/assets/...` returns the asset bytes (size > 0).
- SPA route returns the index (200, not 404).
- POST `/fetch-image` with evil.com returns `400 {"error":"url not allowed"}`.
- Security headers are present: `Content-Security-Policy`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin`, `Cache-Control: no-store` (on `/`).

- [ ] **Step 6: Commit and push**

```bash
git add pyproject.toml
git commit -m "chore: bump version to 0.5.0"
git push origin claude/bsky-saves-next-phase-o7mOi 2>&1 | tail -5
```

Branch is now ready for PR → main → tag `v0.5.0` → PyPI publish.

---

## Post-implementation checklist

After Task 17:

- [ ] PR opened and merged to `main` via GitHub UI.
- [ ] `v0.5.0` tag created on the merge commit via GitHub UI.
- [ ] `release.yml` workflow run succeeds and `bsky-saves==0.5.0` appears on PyPI.
- [ ] Project owner notifies the bsky-saves-gui team that v0.5.0 is live; they can wire up their deferred runtime-smoke and version-coordination gates.
