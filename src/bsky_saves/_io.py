"""Low-level I/O helpers: inventory writes and session-token management."""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

_TOKEN_BYTES = 32


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

    If two bsky-saves processes race to lazy-create, both succeed at writing
    a token to the temp file (last-writer-wins on the temp file body); the
    second os.replace then overwrites the first. Both tokens are valid 0o600
    files; the second-replaced value sticks. A token.tmp file left over from
    a crashed prior write is recovered by truncate-on-next-open, not blocking
    future creation.
    """
    cdir = config_dir()
    path = cdir / "token"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        # Empty file → fall through and regenerate.

    cdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fresh = base64.urlsafe_b64encode(secrets.token_bytes(_TOKEN_BYTES)).rstrip(b"=").decode("ascii")
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
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


def atomic_write_status(path: Path, body: bytes) -> None:
    """Atomic-write the status snapshot to disk with per-write unique tmp names.

    Used by _status._flush_to_disk_synchronously and the background flush
    callback. Per-write unique tmp names (via tempfile.NamedTemporaryFile)
    are defense-in-depth against a future contributor running the same
    writer from multiple threads — today's caller is single-threaded by
    design, but the broader inventory writer's single-tmp-name scheme races
    under concurrent calls and we don't want to inherit that hazard here.

    Args:
        path: target file path. Parent dir is created with 0o700 if absent.
        body: bytes to write. Caller is responsible for encoding (utf-8).
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
    ) as tmp:
        tmp.write(body)
        tmp_path = Path(tmp.name)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
