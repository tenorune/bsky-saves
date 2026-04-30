"""Normalize raw bookmark records into the inventory schema, and merge new
entries into an existing inventory.

Two raw response shapes are supported:

1. ``app.bsky.bookmark.getBookmarks`` (hydrated bookmark view): each entry
   has ``subject.uri`` (the post URI), ``createdAt`` (the bookmark's
   saved-at), and ``item.record.text`` / ``item.author`` / ``item.record.embed``
   for the hydrated post content.

2. ``com.atproto.repo.listRecords`` for the bookmark collection (raw
   records): each entry has ``uri`` (the bookmark record's URI),
   ``value.subject.uri`` (the post URI), ``value.createdAt`` (the bookmark's
   saved-at). No hydrated post content.
"""
from __future__ import annotations


def normalise_record(raw: dict) -> dict:
    """Map a raw bookmark record to the inventory schema."""
    embed_view: dict = {}
    if "item" in raw and isinstance(raw.get("item"), dict):
        # Hydrated `getBookmarks` shape.
        item = raw["item"]
        subject = raw.get("subject", {})
        post_uri = item.get("uri") or subject.get("uri", "")
        saved_at = raw.get("createdAt") or item.get("indexedAt", "")
        record = item.get("record", {})
        post_text = record.get("text", "")
        embed_raw = record.get("embed") or {}
        embed_view = item.get("embed") or {}
        author_raw = item.get("author", {})
    else:
        # Raw `listRecords` shape.
        value = raw.get("value", raw)
        subject = value.get("subject", value)
        post_uri = subject.get("uri") or raw.get("uri", "")
        saved_at = value.get("createdAt") or raw.get("indexedAt", "")
        post_value = subject.get("value", subject)
        post_text = post_value.get("text", "")
        embed_raw = post_value.get("embed") or {}
        author_raw = subject.get("author", {})

    embed = None
    if embed_raw.get("$type") == "app.bsky.embed.external":
        ext = embed_raw.get("external", {})
        embed = {
            "type": "external",
            "url": ext.get("uri", ""),
            "title": ext.get("title", ""),
            "description": ext.get("description", ""),
        }
    elif embed_raw.get("$type") == "app.bsky.embed.recordWithMedia":
        media_raw = embed_raw.get("media") or {}
        if media_raw.get("$type") == "app.bsky.embed.external":
            ext = media_raw.get("external", {})
            embed = {
                "type": "external",
                "url": ext.get("uri", ""),
                "title": ext.get("title", ""),
                "description": ext.get("description", ""),
            }

    author = {
        "handle": author_raw.get("handle", ""),
        "display_name": author_raw.get("displayName", ""),
        "did": author_raw.get("did", ""),
    }

    images = extract_media(embed_view)
    quoted_post = extract_quoted_post(embed_view)

    entry = {
        "uri": post_uri,
        "saved_at": saved_at,
        "post_text": post_text,
        "embed": embed,
        "author": author,
        "images": images,
    }
    if quoted_post is not None:
        entry["quoted_post"] = quoted_post
    return entry


def extract_media(view: dict) -> list[dict]:
    """Extract image / video / embed-thumb URLs from a hydrated embed view.

    Returns a list of {kind, url, alt} dicts where:
      - kind = 'image' for post-attached images
      - kind = 'video' for video thumbnails
      - kind = 'embed_thumb' for external link card thumbnails
    """
    if not isinstance(view, dict):
        return []
    typ = view.get("$type", "")
    out: list[dict] = []
    if typ == "app.bsky.embed.images#view":
        for img in view.get("images", []) or []:
            url = img.get("fullsize") or img.get("thumb")
            if url:
                out.append(
                    {
                        "kind": "image",
                        "url": url,
                        "thumb": img.get("thumb"),
                        "alt": img.get("alt", ""),
                    }
                )
    elif typ == "app.bsky.embed.video#view":
        thumb = view.get("thumbnail")
        if thumb:
            out.append(
                {
                    "kind": "video",
                    "url": thumb,
                    "alt": view.get("alt", ""),
                }
            )
    elif typ == "app.bsky.embed.external#view":
        ext = view.get("external", {}) or {}
        thumb = ext.get("thumb")
        if thumb:
            out.append(
                {
                    "kind": "embed_thumb",
                    "url": thumb,
                    "alt": ext.get("title", ""),
                }
            )
    elif typ == "app.bsky.embed.recordWithMedia#view":
        out.extend(extract_media(view.get("media")))
    return out


def extract_quoted_post(view: dict) -> dict | None:
    """Extract a quote-post's referenced record from a hydrated embed view.

    Returns None when the embed isn't a quote-post. For unavailable records
    (not_found / blocked / detached), returns a stub:
        {"uri": "...", "unavailable": "<kind>"}
    For an available quoted post, returns the full hydrated dict.
    """
    if not isinstance(view, dict):
        return None

    typ = view.get("$type", "")
    record = None
    if typ == "app.bsky.embed.record#view":
        record = view.get("record")
    elif typ == "app.bsky.embed.recordWithMedia#view":
        inner = view.get("record")
        if isinstance(inner, dict):
            record = inner.get("record")

    if not isinstance(record, dict):
        return None

    rec_typ = record.get("$type", "")

    if rec_typ == "app.bsky.embed.record#viewNotFound":
        return {"uri": record.get("uri", ""), "unavailable": "not_found"}
    if rec_typ == "app.bsky.embed.record#viewBlocked":
        return {"uri": record.get("uri", ""), "unavailable": "blocked"}
    if rec_typ == "app.bsky.embed.record#viewDetached":
        return {"uri": record.get("uri", ""), "unavailable": "detached"}
    if rec_typ != "app.bsky.embed.record#viewRecord":
        return None

    author_raw = record.get("author") or {}
    value = record.get("value") or {}

    quoted_images: list[dict] = []
    for embed in record.get("embeds") or []:
        quoted_images.extend(extract_media(embed))

    return {
        "uri": record.get("uri", ""),
        "cid": record.get("cid", ""),
        "author": {
            "handle": author_raw.get("handle", ""),
            "display_name": author_raw.get("displayName", ""),
            "did": author_raw.get("did", ""),
        },
        "text": value.get("text", ""),
        "created_at": value.get("createdAt", ""),
        "images": quoted_images,
    }


def merge_into_inventory(existing: dict, new_entries: list[dict]) -> dict:
    """Merge new_entries into existing inventory.

    Rules:
    - Keyed by ``uri``.
    - For URIs already in the inventory: ADD missing fields from the new
      entry, but never overwrite a non-empty existing value. Preserves
      hydration fields written by other commands (article_text,
      thread_replies, etc.).
    - For new URIs: append the entry as-is.
    - Result sorted by ``saved_at`` desc (newest first).
    - ``fetched_at`` updated by the caller.
    """
    by_uri: dict[str, dict] = {s["uri"]: dict(s) for s in existing.get("saves", [])}
    for entry in new_entries:
        uri = entry.get("uri", "")
        if not uri:
            continue
        if uri in by_uri:
            existing_entry = by_uri[uri]
            for k, v in entry.items():
                cur = existing_entry.get(k)
                if cur in (None, "", [], {}):
                    existing_entry[k] = v
        else:
            by_uri[uri] = dict(entry)
    saves = sorted(by_uri.values(), key=lambda s: s.get("saved_at", ""), reverse=True)
    return {
        "fetched_at": existing.get("fetched_at"),
        "saves": saves,
    }
