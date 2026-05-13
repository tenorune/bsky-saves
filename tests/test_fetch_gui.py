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
