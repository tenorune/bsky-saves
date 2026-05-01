"""Tests for the package's __version__ attribute."""
from __future__ import annotations

from importlib.metadata import version

import bsky_saves


def test_version_matches_package_metadata():
    """__version__ must reflect the installed package metadata, not a
    hardcoded constant. Guards against the second-source-of-truth bug
    where pyproject.toml gets bumped but __init__.py is forgotten.
    """
    assert bsky_saves.__version__ == version("bsky-saves")
