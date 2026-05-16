"""Low-level inventory I/O helpers shared by every write callsite."""
from __future__ import annotations

import base64
import json
import os
import secrets
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
