"""Vendor the bsky-saves-gui dist.tar.gz into src/bsky_saves/_gui/.

Run as a build-time hook (via hatch_build.py) or directly:
    python scripts/fetch_gui.py
"""
from __future__ import annotations

import hashlib
import io
import shutil
import sys
import tarfile
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


if __name__ == "__main__":
    try:
        fetch_gui()
    except GuiFetchError as e:
        print(f"fetch_gui: {e}", file=sys.stderr)
        sys.exit(1)
