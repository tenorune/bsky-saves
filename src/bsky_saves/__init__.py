"""bsky-saves — BlueSky bookmarks ingestion toolkit."""
from __future__ import annotations

from importlib.metadata import version as _pkg_version

__version__ = _pkg_version("bsky-saves")

from .normalize import (
    merge_into_inventory,
    normalise_record,
)

__all__ = [
    "__version__",
    "merge_into_inventory",
    "normalise_record",
]
