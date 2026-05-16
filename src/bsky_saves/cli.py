"""Command-line entry point for ``bsky-saves``.

Subcommands:

  bsky-saves fetch --inventory PATH
      Authenticate and pull all bookmarks into the inventory file.

  bsky-saves hydrate articles --inventory PATH [--refresh-dates]
  bsky-saves hydrate threads  --inventory PATH
  bsky-saves hydrate images   --inventory PATH --out DIR [--uris FILE]
      Idempotent hydration of articles, threads, and image localization.

  bsky-saves enrich --inventory PATH [--refresh]
      Decode post_created_at from rkeys and clean bogus article_published_at.

  bsky-saves serve [--port PORT] [--allow-origin ORIGIN]... [--verbose]
      Run a local HTTP helper daemon for bsky-saves-gui (CORS bridge).

  bsky-saves token [--rotate]
      Print (or rotate) the session token used by bsky-saves-gui to pair
      with the local helper daemon.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_uris(path: Path | None) -> set[str] | None:
    """Load a newline-delimited URI list. Returns None if path is None.

    Strips blank lines and `#`-prefixed comments; trims surrounding whitespace
    on each URI; deduplicates.
    """
    if path is None:
        return None
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.add(s)
    return out


def _add_inventory_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--inventory",
        type=Path,
        required=True,
        help="Path to saves_inventory.json (created if absent on fetch).",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bsky-saves")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="Pull bookmarks into the inventory.")
    _add_inventory_arg(p_fetch)
    p_fetch.add_argument(
        "--pds",
        default=os.environ.get("BSKY_PDS", "https://bsky.social"),
        help="PDS base URL (default: $BSKY_PDS or https://bsky.social).",
    )
    p_fetch.add_argument(
        "--appview",
        default=os.environ.get("BSKY_APPVIEW", "https://bsky.social"),
        help="AppView base URL for fallback endpoints (default: $BSKY_APPVIEW or https://bsky.social).",
    )
    fetch_mode_group = p_fetch.add_mutually_exclusive_group()
    fetch_mode_group.add_argument(
        "--mode",
        choices=["sync", "keep-lost", "keep-all"],
        dest="mode",
        help=(
            "Inventory retention policy (default: keep-lost). "
            "sync: mirror only what is live on the server. "
            "keep-lost: also keep posts removed outside your control. "
            "keep-all: also keep bookmarks you deliberately un-saved."
        ),
    )
    fetch_mode_group.add_argument(
        "--sync",
        action="store_const",
        const="sync",
        dest="mode",
        help="Alias for --mode sync.",
    )
    fetch_mode_group.add_argument(
        "--keep-all",
        action="store_const",
        const="keep-all",
        dest="mode",
        help="Alias for --mode keep-all.",
    )
    # NOTE: argparse MEG enforcement is bypassed when one arg specifies the
    # default value explicitly (e.g. --sync --mode keep-lost). This is a
    # known argparse limitation with store_const + set_defaults on a shared
    # dest; the last-given value wins silently in that narrow case.
    p_fetch.set_defaults(mode="keep-lost")

    p_hydrate = sub.add_parser("hydrate", help="Hydrate inventory entries.")
    hsub = p_hydrate.add_subparsers(dest="hydrate_what", required=True)

    p_articles = hsub.add_parser("articles", help="Fetch linked articles.")
    _add_inventory_arg(p_articles)
    p_articles.add_argument(
        "--refresh-dates",
        action="store_true",
        help="Re-fetch already-hydrated articles to update article_published_at.",
    )

    p_threads = hsub.add_parser("threads", help="Walk same-author thread descendants.")
    _add_inventory_arg(p_threads)
    p_threads.add_argument(
        "--appview",
        default="https://public.api.bsky.app",
        help="Public AppView base URL (default: https://public.api.bsky.app).",
    )

    p_images = hsub.add_parser("images", help="Download CDN images referenced in the inventory.")
    _add_inventory_arg(p_images)
    p_images.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Directory to download images into (flat layout; created if absent).",
    )
    p_images.add_argument(
        "--uris",
        type=Path,
        default=None,
        help="Optional newline-delimited list of at:// post URIs to limit download to. "
             "If omitted, all inventory entries with images are processed.",
    )

    p_enrich = sub.add_parser("enrich", help="Decode post_created_at and clean stale dates.")
    _add_inventory_arg(p_enrich)
    p_enrich.add_argument(
        "--refresh",
        action="store_true",
        help="Recompute post_created_at even if it's already set.",
    )

    p_serve = sub.add_parser(
        "serve",
        help="Run a local HTTP helper daemon for bsky-saves-gui.",
    )
    p_serve.add_argument(
        "--port",
        type=int,
        default=47826,
        help="TCP port to bind on 127.0.0.1 (default: 47826).",
    )
    p_serve.add_argument(
        "--allow-origin",
        action="append",
        default=None,
        metavar="ORIGIN",
        help=(
            "Additional Origin to permit, in addition to the defaults "
            "(http://127.0.0.1:<port>, http://localhost:<port>, "
            "https://saves.lightseed.net). May be specified multiple times."
        ),
    )
    p_serve.add_argument(
        "--verbose",
        action="store_true",
        help="Log each request to stderr.",
    )
    p_serve.add_argument(
        "--gui",
        action="store_true",
        default=False,
        help="Also serve the bundled GUI from / on the same port. "
             "Requires the wheel-bundled _gui/ tree (or a local fetch via "
             "scripts/fetch_gui.py).",
    )

    p_token = sub.add_parser(
        "token",
        help="Print the session token used by bsky-saves-gui to pair with this helper.",
    )
    p_token.add_argument(
        "--rotate",
        action="store_true",
        help="Generate a fresh token, invalidating any paired GUI sessions.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "fetch":
        from .fetch import fetch_to_inventory

        handle = os.environ.get("BSKY_HANDLE")
        app_password = os.environ.get("BSKY_APP_PASSWORD")
        if not handle or not app_password:
            print(
                "bsky-saves: BSKY_HANDLE and BSKY_APP_PASSWORD must be set",
                file=sys.stderr,
            )
            return 2
        fetch_to_inventory(
            args.inventory,
            handle=handle,
            app_password=app_password,
            pds_base=args.pds,
            appview_base=args.appview,
            mode=args.mode,
        )
        return 0

    if args.cmd == "hydrate":
        if args.hydrate_what == "articles":
            from .articles import hydrate_articles

            hydrate_articles(args.inventory, refresh_dates=args.refresh_dates)
            return 0
        if args.hydrate_what == "threads":
            from .threads import hydrate_threads

            hydrate_threads(args.inventory, appview=args.appview)
            return 0
        if args.hydrate_what == "images":
            from .images import hydrate_images

            hydrate_images(
                args.inventory,
                args.out,
                uris=_load_uris(args.uris),
            )
            return 0

    if args.cmd == "enrich":
        from .enrich import enrich_inventory

        enrich_inventory(args.inventory, refresh=args.refresh)
        return 0

    if args.cmd == "serve":
        from .serve import run_serve

        return run_serve(
            port=args.port,
            allow_origins=args.allow_origin or [],
            verbose=args.verbose,
            gui=args.gui,
        )

    if args.cmd == "token":
        from ._io import config_dir, read_or_create_token, _TOKEN_BYTES
        import base64
        import secrets

        if args.rotate:
            cdir = config_dir()
            cdir.mkdir(mode=0o700, parents=True, exist_ok=True)
            fresh = base64.urlsafe_b64encode(secrets.token_bytes(_TOKEN_BYTES)).rstrip(b"=").decode("ascii")
            path = cdir / "token"
            tmp = path.with_suffix(".tmp")
            fd = os.open(str(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            try:
                os.write(fd, (fresh + "\n").encode("ascii"))
            finally:
                os.close(fd)
            os.replace(tmp, path)
            print(fresh)
            return 0

        print(read_or_create_token())
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
