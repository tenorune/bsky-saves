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
    subject_status: str | None = None
    if "item" in raw and isinstance(raw.get("item"), dict):
        # Hydrated `getBookmarks` shape.
        item = raw["item"]
        subject = raw.get("subject", {})
        post_uri = item.get("uri") or subject.get("uri", "")
        saved_at = raw.get("createdAt") or item.get("indexedAt", "")
        item_type = item.get("$type", "")
        # Any other $type — a postView, an absent $type, or a future union
        # member we don't recognise — is treated as live: no subject_status.
        if item_type == "app.bsky.feed.defs#notFoundPost":
            subject_status = "not_found"
        elif item_type == "app.bsky.feed.defs#blockedPost":
            subject_status = "blocked"
        record = item.get("record", {})
        post_text = record.get("text", "")
        embed_raw = record.get("embed") or {}
        embed_view = item.get("embed") or {}
        author_raw = item.get("author", {})
    else:
        # Raw `listRecords` shape — no hydrated post content, no subject state.
        subject_status = "unknown"
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
    if subject_status is not None:
        entry["subject_status"] = subject_status
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


_LIFECYCLE_KEYS = frozenset(
    {"subject_status", "subject_status_detected_at", "last_seen_at", "removed_detected_at"}
)


def _reconcile_subject_status(
    working: dict, prior: dict | None, fresh: dict, now: str
) -> None:
    """Reconcile subject_status / subject_status_detected_at on `working`.

    `prior` is the entry as it stood before this merge (or None for a
    brand-new URI); `fresh` is the newly-fetched normalised record. `working`
    is mutated in place. See the v0.6.0 spec section 6.2.
    """
    fresh_status = fresh.get("subject_status")
    prior_status = prior.get("subject_status") if prior is not None else None
    prior_detected = (
        prior.get("subject_status_detected_at") if prior is not None else None
    )

    working.pop("subject_status", None)
    working.pop("subject_status_detected_at", None)

    if fresh_status is None:
        # Live observation — both fields stay cleared.
        return
    if fresh_status in ("not_found", "blocked"):
        working["subject_status"] = fresh_status
        if prior_status == fresh_status and prior_detected is not None:
            working["subject_status_detected_at"] = prior_detected
        else:
            working["subject_status_detected_at"] = now
        return
    # fresh_status == "unknown": never overwrites, weakens, or clears an
    # existing entry; stored only for a brand-new URI; never sets the
    # timestamp.
    if prior is None:
        working["subject_status"] = "unknown"
    else:
        if prior_status is not None:
            working["subject_status"] = prior_status
        if prior_detected is not None:
            working["subject_status_detected_at"] = prior_detected


def merge_into_inventory(
    existing: dict,
    new_entries: list[dict],
    *,
    mode: str = "keep-lost",
    now: str,
) -> dict:
    """Merge new_entries into existing inventory under a retention mode.

    ``mode`` is one of "sync", "keep-lost" (default), "keep-all". ``now`` is
    the fetch timestamp, written into the lifecycle flags. See the v0.6.0 spec
    section 6.2 for the full algorithm.

    Present entries: field-fill (never overwrite a non-empty existing value;
    lifecycle keys are owned by the flag pass, not the field-fill) plus a
    lifecycle-flag pass. Absent entries (Class 1) are dropped under
    keep-lost/sync and flagged under keep-all; sync additionally prunes Class 2
    entries (present bookmarks with a dead subject).
    """
    by_uri: dict[str, dict] = {s["uri"]: dict(s) for s in existing.get("saves", [])}
    fetched_uris = {e["uri"] for e in new_entries if e.get("uri")}

    for entry in new_entries:
        uri = entry.get("uri", "")
        if not uri:
            continue
        prior = by_uri.get(uri)
        if prior is not None:
            prior_snapshot = dict(prior)
            working = prior
            for k, v in entry.items():
                if k in _LIFECYCLE_KEYS:
                    continue
                cur = working.get(k)
                if cur in (None, "", [], {}):
                    working[k] = v
        else:
            prior_snapshot = None
            working = dict(entry)
            by_uri[uri] = working
        working["last_seen_at"] = now
        working.pop("removed_detected_at", None)
        _reconcile_subject_status(working, prior_snapshot, entry, now)

    # Absent entries (Class 1 — the user un-saved them).
    for uri, entry in list(by_uri.items()):
        if uri in fetched_uris:
            continue
        if mode == "keep-all":
            entry.setdefault("removed_detected_at", now)
        else:  # keep-lost or sync
            del by_uri[uri]

    # sync mode actively prunes Class 2 entries — present bookmarks whose
    # subject post is known to be gone. "unknown" is not *known* dead, so it
    # is kept.
    if mode == "sync":
        for uri, entry in list(by_uri.items()):
            if entry.get("subject_status") in ("not_found", "blocked"):
                del by_uri[uri]

    saves = sorted(by_uri.values(), key=lambda s: s.get("saved_at", ""), reverse=True)
    return {
        "fetched_at": existing.get("fetched_at"),
        "saves": saves,
    }
