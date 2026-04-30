"""Command-line entry point for ``bsky-saves``.

Subcommands:

  bsky-saves fetch --inventory PATH
      Authenticate and pull all bookmarks into the inventory file.

  bsky-saves hydrate articles --inventory PATH [--refresh-dates]
  bsky-saves hydrate threads  --inventory PATH
  bsky-saves hydrate images   --stories DIR --assets DIR [--assets-url-prefix /assets/stories]
      Idempotent hydration of articles, threads, and image localization.

  bsky-saves enrich --inventory PATH [--refresh]
      Decode post_created_at from rkeys and clean bogus article_published_at.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


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

    p_images = hsub.add_parser("images", help="Localize cdn.bsky.app image refs in Markdown files.")
    p_images.add_argument(
        "--stories",
        type=Path,
        required=True,
        help="Directory of Markdown files to scan.",
    )
    p_images.add_argument(
        "--assets",
        type=Path,
        required=True,
        help="Directory to download images into (under <slug>/ subdirs).",
    )
    p_images.add_argument(
        "--assets-url-prefix",
        default="/assets/stories",
        help="Root-relative URL prefix that will replace cdn.bsky.app URLs (default: /assets/stories).",
    )

    p_enrich = sub.add_parser("enrich", help="Decode post_created_at and clean stale dates.")
    _add_inventory_arg(p_enrich)
    p_enrich.add_argument(
        "--refresh",
        action="store_true",
        help="Recompute post_created_at even if it's already set.",
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
            from .images import localize_images

            localize_images(
                args.stories,
                args.assets,
                assets_url_prefix=args.assets_url_prefix,
            )
            return 0

    if args.cmd == "enrich":
        from .enrich import enrich_inventory

        enrich_inventory(args.inventory, refresh=args.refresh)
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
