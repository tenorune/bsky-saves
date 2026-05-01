# bsky-saves

A toolkit for ingesting your own BlueSky bookmarks ("saves") into a portable
JSON inventory, with optional hydration of linked article text, self-thread
context, and CDN image downloads.

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
```

All commands are **idempotent**: running them again skips already-hydrated
entries and adds only what's new. Failures are recorded inline (e.g.
`article_fetch_error`) so subsequent runs don't pointlessly re-hit them.

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
      "quoted_post": { /* optional, when the save quote-posts another post */ },

      // Added by `hydrate articles`:
      "article_text": "...",
      "article_published_at": "2025-09-13",
      "article_fetched_at": "...",

      // Added by `hydrate threads`:
      "thread_replies": [
        { "uri": "...", "indexedAt": "...", "text": "...", "images": [...] }
      ],
      "thread_schema_version": 3,
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
