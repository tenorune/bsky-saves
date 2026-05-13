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
