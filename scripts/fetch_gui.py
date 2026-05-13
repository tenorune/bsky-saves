"""Vendor the bsky-saves-gui dist.tar.gz into src/bsky_saves/_gui/.

Run as a build-time hook (via hatch_build.py) or directly:
    python scripts/fetch_gui.py
"""
from __future__ import annotations

import hashlib
import sys
import tomllib
import urllib.error
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
