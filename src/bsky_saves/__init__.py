"""bsky-saves — BlueSky bookmarks ingestion toolkit."""
from __future__ import annotations

__version__ = "0.1.0"

from .normalize import (
    merge_into_inventory,
    normalise_record,
)

__all__ = [
    "__version__",
    "merge_into_inventory",
    "normalise_record",
]
