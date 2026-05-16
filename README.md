# bsky-saves

A toolkit for ingesting your own BlueSky bookmarks ("saves") into a portable
JSON inventory, with optional hydration of linked article text, self-thread
context, and CDN image downloads.

Since v0.5.0 the package also ships the [bsky-saves-gui] static web app
inside the wheel; `bsky-saves serve --gui` mounts it at `http://127.0.0.1:47826/`
so a `pipx install bsky-saves` user can open a browser-based UI without
provisioning anything else.

[bsky-saves-gui]: https://github.com/tenorune/bsky-saves-gui

## Why

The BlueSky web client lets you bookmark posts, but the saves are siloed
inside the app. This tool pulls them out into a single JSON file you can
read, archive, mirror, or build on top of.

It works for accounts hosted on `bsky.social` *and* on third-party AT
Protocol PDSes (e.g. `eurosky.social`), because the bookmark fetch goes
PDS-direct rather than through the AppView.

## Install

```
pip install bsky-saves
```

## Upgrade

If you installed with `pipx` (recommended for CLI tools):

```
pipx upgrade bsky-saves
```

If you installed with `pip`:

```
pip install --upgrade bsky-saves
```

If `bsky-saves serve` is currently running, restart it after upgrading so
the new helper version takes effect — the GUI's outdated-helper banner
keeps showing until the running daemon reports the upgraded version.

## Authenticate

Set two env vars from a [BlueSky app password]:

```
export BSKY_HANDLE=alice.bsky.social
export BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
# Required only for accounts hosted on a third-party PDS:
export BSKY_PDS=https://eurosky.social
```

The default `BSKY_PDS` is `https://bsky.social`.

[BlueSky app password]: https://bsky.app/settings/app-passwords

## Use

```
# Pull all bookmarks → ./saves_inventory.json
bsky-saves fetch --inventory ./saves_inventory.json

# Retention mode controls what happens to bookmarks no longer on the server.
#   keep-lost (default) — keep posts removed outside your control (deleted /
#                         blocked), drop bookmarks you deliberately un-saved.
#   --sync     (= --mode sync)     — keep only live posts; also drops posts
#                                    deleted/blocked (unknown-status kept).
#   --keep-all (= --mode keep-all) — keep everything, including your un-saves.
bsky-saves fetch --inventory ./saves_inventory.json --keep-all

# Hydrate every external-link bookmark with the linked article's text.
bsky-saves hydrate articles --inventory ./saves_inventory.json

# Hydrate every bookmark with same-author self-thread descendants.
bsky-saves hydrate threads --inventory ./saves_inventory.json

# Decode each save's post-creation timestamp from its rkey (offline).
bsky-saves enrich --inventory ./saves_inventory.json

# Download cdn.bsky.app images referenced by the inventory into ./images/
# (flat layout). Records url→path mappings as `local_images` on each entry.
# Use --uris FILE (newline-delimited at:// URIs) to limit to a subset.
bsky-saves hydrate images --inventory ./saves_inventory.json --out ./images

# Run a local HTTP helper daemon for bsky-saves-gui (CORS bridge).
# Binds 127.0.0.1:47826; pass --allow-origin for self-hosted GUI deployments.
bsky-saves serve

# Same daemon, plus serve the bundled GUI itself at http://127.0.0.1:47826/.
bsky-saves serve --gui
```

All commands are **safe to re-run**: `hydrate`/`enrich` skip already-hydrated
entries and add only what's new (`fetch` re-syncs the full bookmark list each
run). Failures are recorded inline (e.g. `article_fetch_error`) so subsequent
runs don't pointlessly re-hit them.

**Behaviour change in v0.6.0:** the default retention mode is `keep-lost`.
Before v0.6.0 the CLI was purely additive — it never removed an inventory
entry. From v0.6.0, the first `fetch` after upgrading will drop entries you had
un-saved (no longer in your bookmark list on the server). Run with
`--keep-all` to preserve the old additive-everything behaviour.

## `bsky-saves serve`

`bsky-saves serve` runs a small HTTP helper daemon on `127.0.0.1` that
[bsky-saves-gui] — a static web app running `bsky-saves` in Pyodide —
calls to offload operations the browser can't do directly: fetching image
bytes and arbitrary article URLs (both blocked by CORS), and routing
bookmark enumeration, enrichment, and thread hydration through the helper
instead of running them in Pyodide.

```
bsky-saves serve [--gui] [--port 47826] [--allow-origin ORIGIN]... [--verbose]
```

The daemon binds only to `127.0.0.1`, writes nothing to disk, reads no
config files, validates the `Host` header to reject DNS-rebinding attempts
(`421`), enforces an `Origin` allowlist (`403` for anything outside the
defaults), caps request bodies at 10 MB, and exposes six endpoints:

| Endpoint | Credentials | Purpose |
|---|---|---|
| `GET /ping` | — | Health check; advertises supported endpoints in a `features` array |
| `POST /fetch-image` | — | Download a `cdn.bsky.app` image; returns the bytes |
| `POST /extract-article` | — | Fetch + trafilatura-extract text from an article URL |
| `POST /fetch` | required | Paginated bookmark enumeration with opaque cursor |
| `POST /enrich` | — | Decode `post_created_at` offline from at-URI rkeys |
| `POST /hydrate-threads` | required | Concurrent same-author thread reply hydration |

Endpoints that require credentials accept `{handle, app_password, pds?}`
in the request body; the daemon does its own `createSession` per request
and never persists anything. `pds` defaults to `https://bsky.social` when
absent. `/hydrate-threads` validates credentials (to fail-fast on a bad
app password) but reads threads from the public AppView unauthenticated.

The default `Origin` allowlist is `http://127.0.0.1:<port>`,
`http://localhost:<port>`, and `https://saves.lightseed.net`. Pass
`--allow-origin <url>` (repeatable) to **add** to this list — for example
if you self-host the GUI at a custom URL. The flag is additive, not
replacing.

### `--gui` mode

Pass `--gui` to also mount the bundled `bsky-saves-gui` static bundle at
`/`. The GUI shares the same loopback port that serves the JSON API; API
routes always take precedence over static files. Missing non-API paths
fall back to the GUI's `index.html` so its SPA router takes over.

`--gui` is opt-in. Without it, the daemon behaves as a JSON-only CORS
bridge for the hosted GUI at `https://saves.lightseed.net` (the v0.4.x
behaviour). With it, you don't need a hosted GUI deployment at all — open
`http://127.0.0.1:47826/` directly. The wheel bundles a known-version GUI
pinned at build time via SHA-256; bumping the pin requires a coordinated
release with the `bsky-saves-gui` repo.

If `--gui` is passed but the bundled GUI is missing (e.g. a broken
install or an sdist build that didn't run the vendor hook), the daemon
exits with code 2 and a clear error.

The full HTTP API contracts live in the consumer repo:

- v1 endpoints (`/ping`, `/fetch-image`, `/extract-article`):
  [`bsky-saves-gui/docs/bsky-saves-serve-requirements.md`](https://github.com/tenorune/bsky-saves-gui/blob/main/docs/bsky-saves-serve-requirements.md).
- v2 endpoints (`/fetch`, `/enrich`, `/hydrate-threads`):
  [`bsky-saves-gui/docs/bsky-saves-serve-fetch-enrich-threads-requirements.md`](https://github.com/tenorune/bsky-saves-gui/blob/main/docs/bsky-saves-serve-fetch-enrich-threads-requirements.md).

## Inventory schema

```jsonc
{
  "fetched_at": "2026-04-30T14:00:00Z",
  "saves": [
    {
      "uri": "at://did:plc:.../app.bsky.feed.post/abc123",
      "saved_at": "2026-04-29T22:11:00Z",
      "post_created_at": "2026-04-29T17:43:51Z",  // decoded from rkey
      "post_text": "...",
      "embed": {
        "type": "external",
        "url": "https://example.org/article",
        "title": "...",
        "description": "..."
      },
      "author": { "handle": "...", "display_name": "...", "did": "..." },
      "images": [
        { "kind": "image", "url": "https://cdn.bsky.app/...", "alt": "..." }
      ],
      // Lifecycle flags (added by `fetch`; see retention modes above):
      "last_seen_at": "2026-04-30T14:00:00Z",          // last fetch that saw this URI
      "removed_detected_at": "2026-05-02T09:00:00Z",   // optional; you un-saved it (retained only under --keep-all)
      "subject_status": "not_found",                   // optional; "not_found" | "blocked" | "unknown"
      "subject_status_detected_at": "2026-05-02T09:00:00Z", // optional; when subject_status went non-live
      "quoted_post": { /* optional, when the save quote-posts another post */ },

      // Added by `hydrate articles`:
      "article_text": "...",
      "article_published_at": "2025-09-13",
      "article_fetched_at": "...",

      // Added by `hydrate threads`:
      "thread_replies": [
        { "uri": "...", "indexedAt": "...", "text": "...", "images": [...] }
      ],
      "thread_schema_version": 4,
      "thread_fetched_at": "...",

      // Added by `hydrate images`:
      "local_images": [
        { "url": "https://cdn.bsky.app/...", "path": "img-9f2c8e1b....jpg" }
      ]
    }
  ]
}
```

## What about OAuth?

`bsky-saves` only supports the app-password authentication path. The
OAuth + DPoP machinery for third-party PDSes lives in a separate package,
[`atproto-oauth-py`], and exists primarily for AppView-targeted resource calls
that aren't reachable via PDS-direct auth. For BlueSky bookmarks the
PDS-direct path (which `bsky-saves` uses) works regardless of where your
account is hosted.

[`atproto-oauth-py`]: https://pypi.org/project/atproto-oauth-py/

## License

MIT. See `LICENSE`.

## Provenance

Extracted from <https://github.com/tenorune/tenorune.github.io>'s `scripts/`
directory, where it powered the [Stories of 47] archive's BlueSky save
ingestion. The Jekyll site itself stays in that repo; this is the reusable
ingestion layer.

[Stories of 47]: https://lightseed.net/stories/
