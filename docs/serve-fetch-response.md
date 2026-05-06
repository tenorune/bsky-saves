# `POST /fetch` — Response Shape and Post-Processing

> Daemon-side reference for the `/fetch` endpoint of `bsky-saves serve`.
> The high-level HTTP contract lives in the consumer-side requirements doc
> ([bsky-saves-gui v2 spec](https://github.com/tenorune/gui/blob/main/docs/bsky-saves-serve-fetch-enrich-threads-requirements.md));
> this doc focuses on the actual JSON shapes the daemon emits and the
> post-processing that produces them.

## TL;DR

`POST /fetch` returns `200 application/json` on success with this shape:

```jsonc
{
  "saves": [ /* normalised inventory entries */ ],
  "cursor": "<opaque-string-or-null>",
  "rotated_credentials": { /* optional, JWT path with refresh only */ }
}
```

Each entry in `saves` is the result of running the upstream record through
`bsky_saves.normalize.normalise_record`, which flattens the four
BOOKMARK_ENDPOINTS' wildly different upstream shapes into one consistent
inventory shape (the same shape `bsky-saves fetch` writes to its CLI
inventory file).

## Per-entry shape

Each `saves[i]` is a `normalise_record()` output:

```jsonc
{
  "uri": "at://did:plc:abc/app.bsky.feed.post/...",
  "saved_at": "2026-04-29T22:11:00Z",
  "post_text": "...",
  "embed": null,
  // or, for external-link bookmarks:
  // "embed": { "type": "external", "url": "...", "title": "...", "description": "..." }
  "author": {
    "handle": "alice.bsky.social",
    "display_name": "Alice",
    "did": "did:plc:abc"
  },
  "images": [
    { "kind": "image",       "url": "...", "thumb": "...", "alt": "..." },
    { "kind": "video",       "url": "...", "alt": "..." },
    { "kind": "embed_thumb", "url": "...", "alt": "..." }
  ],
  "quoted_post": null
  // or, for quote-posts:
  // "quoted_post": {
  //   "uri": "at://...", "cid": "...",
  //   "author": { "handle": "...", "display_name": "...", "did": "..." },
  //   "text": "...", "created_at": "...",
  //   "images": [ /* same kind/url/alt shape as above */ ]
  // }
  // or, for unavailable quote-posts:
  // "quoted_post": { "uri": "at://...", "unavailable": "not_found" | "blocked" | "detached" }
}
```

Snake_case throughout. The daemon does NOT include:

- `post_created_at` — added by `/enrich` from the rkey's TID.
- `article_text`, `article_published_at`, `article_fetched_at` — added by the GUI
  after calling `/extract-article` and merging.
- `thread_replies`, `thread_schema_version`, `thread_fetched_at` — added by `/hydrate-threads`.
- `local_images` — added by the GUI after calling `/fetch-image` for each
  CDN URL and merging.
- `cid` (per-save) — `bsky-saves`'s `normalise_record` doesn't currently
  capture it; if upstream adds it, the endpoint inherits it.

## Top-level `cursor`

Opaque pagination token. `urlsafe-base64(JSON({v: 1, endpoint, upstream}))`
internally; the GUI MUST treat it as fully opaque and round-trip it
byte-for-byte. `null` when there are no more pages.

`endpoint` records which of the four BOOKMARK_ENDPOINTS the daemon used
for this page so subsequent calls skip the probe and call that endpoint
directly. If the named endpoint hard-fails on a continuation call, the
daemon silently re-probes (cursor dropped, restart from page 1 of the new
winner) — see § `Failure modes` below.

## Top-level `rotated_credentials` (JWT path only, optional)

Present **only** when:

1. The request used the JWT-pair credential shape (`{access_jwt, refresh_jwt, did, ...}`), AND
2. The upstream call returned 401, AND
3. The daemon's `refreshSession` call succeeded, AND
4. The retry of the upstream call succeeded.

Shape:

```jsonc
"rotated_credentials": {
  "access_jwt": "<new>",
  "refresh_jwt": "<new>",
  "did": "did:plc:..."
}
```

The GUI **must** persist these synchronously over its stored JWT pair
before the next request — AT Protocol invalidates a `refresh_jwt` once
it's been used to mint a new pair. Failing to persist the rotation
leaves the GUI's stored `refresh_jwt` silently dead, and the next
refresh will fail with `auth refresh failed`.

Always absent on:

- App-password path responses.
- JWT-path responses where no refresh was needed.
- All `/hydrate-threads` responses (no upstream call could trigger refresh).

## Post-processing — what `normalise_record` does

The four BOOKMARK_ENDPOINTS return wildly different upstream shapes; the
daemon flattens them all into one consistent inventory shape so the GUI
doesn't have to know which endpoint served the request. `normalise_record`
performs all of the following:

### 1. Two upstream record shapes get unified

| Upstream endpoint | Raw shape |
|---|---|
| `app.bsky.bookmark.getBookmarks` (hydrated bookmark view) | `{subject: {uri, ...}, createdAt, item: {uri, indexedAt, record, author, embed}}` |
| `com.atproto.repo.listRecords` (raw record fallback) | `{uri, value: {subject: {...}, createdAt}}` (URI references only; no hydrated post content) |

`normalise_record` detects which shape it got (by presence of the `item` key)
and pulls the right fields. The output shape is the same either way.

### 2. Field renaming (camelCase → snake_case)

| Upstream | Normalised |
|---|---|
| `displayName` | `display_name` |
| `createdAt` (quoted post) | `created_at` |
| `indexedAt` (top-level entry) | `saved_at` |

`bsky-saves`'s inventory shape is snake_case throughout; upstream API is
camelCase.

### 3. Embed normalization

BlueSky's lexicon embed shapes vary by type. The normaliser maps them all
into a flat `embed` field (or `quoted_post`, for record-shaped embeds):

| Upstream `$type` | Normalised target |
|---|---|
| `app.bsky.embed.external#view` | `embed: {type: "external", url, title, description}` |
| `app.bsky.embed.images#view` | `images: [{kind: "image", url, thumb, alt}, ...]`; `embed: null` |
| `app.bsky.embed.video#view` | `images: [{kind: "video", url, alt}]`; `embed: null` |
| `app.bsky.embed.record#view` | `quoted_post: {uri, cid, author, text, created_at, images}`; `embed: null` |
| `app.bsky.embed.recordWithMedia#view` | Both: external/media → `embed`/`images`, quoted record → `quoted_post` |
| (no embed) | `embed: null`, `images: []`, `quoted_post: null` |

### 4. Image extraction across embed shapes

Whether the post had post-attached images (`app.bsky.embed.images#view`),
an external link with a thumbnail (`embed.external.thumb`), or a video
thumbnail, the result is a single uniform `images: [{kind, url, thumb, alt}]`
array. `kind` is one of:

| `kind` | Source |
|---|---|
| `image` | Native post-attached images |
| `video` | Video thumbnails |
| `embed_thumb` | External link card thumbnails |

This makes downstream code (the GUI's render path, `bsky-saves
hydrate images`) blind to the upstream embed structure.

### 5. Quoted-post unwrapping

If the post quotes another post (`app.bsky.embed.record#view` or
`recordWithMedia#view`), the quoted record is extracted into a nested
`quoted_post: {uri, cid, author, text, created_at, images}` — itself
snake_case-normalised. Unavailable quotes (`not_found` / `blocked` /
`detached`) become a stub:

```jsonc
"quoted_post": { "uri": "at://...", "unavailable": "not_found" }
```

(or `"blocked"` or `"detached"`).

## Post-processing — what the daemon does NOT do

- **No deduplication** of `saves` entries within a page (the upstream API
  doesn't return duplicates).
- **No filtering** — every record from upstream becomes an entry in
  `saves`, even if it has minimal data (e.g., `listRecords` URI-only
  records — see "subtle implication" below).
- **No enrichment** — `post_created_at`, `article_text`, `thread_replies`,
  `local_images` are explicitly NOT populated by `/fetch`. The GUI calls
  the other helper endpoints (or runs them in Pyodide) to fill those in.
- **No sorting** — the page comes back in whatever order the upstream
  endpoint chose.
- **No per-save error wrapping** — if a record fails to normalise, an
  exception bubbles up and the whole `/fetch` call returns 502. Per-save
  failure modes (e.g., a malformed embed) shouldn't happen with current
  upstream data; if they do, the call fails atomically rather than
  partially.

## Subtle implication: `listRecords` stubs

Because the same `normalise_record()` runs whether the page came from
`app.bsky.bookmark.getBookmarks` (hydrated, full post content) or
`com.atproto.repo.listRecords` (URI-only, no `record`/`author`/`embed`),
entries served via the latter have many fields stubbed:

```jsonc
{
  "uri": "at://did:plc:abc/app.bsky.feed.post/...",
  "saved_at": "2026-04-29T22:11:00Z",
  "post_text": "",
  "embed": null,
  "author": { "handle": "", "display_name": "", "did": "" },
  "images": [],
  "quoted_post": null   // (omitted, not stubbed)
}
```

The GUI's downstream code must tolerate these stubs. The `listRecords`
endpoint is a fallback — the probe order tries the hydrated bookmark
endpoints first, and they normally succeed. `listRecords` only serves
when all three other endpoints have failed. When it does serve, the GUI
loses author profiles, post text, embed metadata, and images; it has only
URIs to work with. Calling `/extract-article` (with the original linked
URL) won't work because the `embed` is `null`. `/hydrate-threads` still
works (it only needs the URI).

This is a property of which upstream endpoint successfully responded, not
a daemon-side processing decision. The daemon doesn't try to "recover"
the missing fields — that's not its job, and there's no source of truth
for them at this layer.

## Failure modes (response status codes)

| Status | Body | Trigger |
|---|---|---|
| `400` | `{"error": "missing credentials"}` | Neither `app_password` nor `access_jwt` present, or required fields missing for the variant |
| `400` | `{"error": "invalid cursor"}` | Cursor failed to decode (corrupted, mangled, or version-incompatible) |
| `401` | `{"error": "createSession failed: <message>"}` | App-password path: bad app password |
| `401` | `{"error": "auth refresh failed", "code": "refresh_failed"}` | JWT path: `refreshSession` itself failed (refresh_jwt invalid/expired/revoked) |
| `401` | `{"error": "auth refresh failed", "code": "upstream_rejected_after_refresh"}` | JWT path: refresh succeeded but the retry still got 401 |
| `502` | `{"error": "no working bookmark endpoint: ..."}` | All four BOOKMARK_ENDPOINTS failed (probe path or silent-fallback path) |
| `502` | `{"error": "<ExceptionType>: <message>"}` | Transport-level failure during `createSession` (e.g., DNS, network) |

The GUI handles both 401 `code` values the same way (re-prompt for app
password); the split exists for daemon-side diagnostics and `--verbose`
logs.

## Silent endpoint fallback

When a continuation call fails (a wrapped cursor's named endpoint returns
a 4xx/5xx), the daemon re-probes from a fresh state and continues on
whichever new endpoint becomes the winner. This is invisible to the GUI in
terms of error surface — the call returns 200 with saves and a cursor
encoding the new winner.

The fallback **drops the upstream cursor** and restarts pagination from
page 1 of the new endpoint. The four bookmark endpoints have incompatible
cursor formats (e.g., `pds:listRecords` uses a record-key TID,
`bookmark.getBookmarks` uses an opaque lexicon cursor), so cross-endpoint
cursor reuse risks silently wrong pages. The GUI may receive entries it
has already seen on this run — deduplicate by `uri` if downstream code
can't tolerate that.

JWT-path silent fallback is restricted to non-401 failures. A 401 on a
JWT-path continuation triggers refresh + retry on the SAME endpoint, not
fallback.

## Source

- Implementation: `src/bsky_saves/serve.py::_handle_fetch`,
  `src/bsky_saves/normalize.py::normalise_record`,
  `src/bsky_saves/fetch.py::fetch_one_page`.
- Tests: `tests/test_serve.py` (look for `test_fetch_*`).
- Spec: `docs/superpowers/specs/2026-05-04-bsky-saves-v0.3-serve-subcommand.md` (v0.3 — original `/serve`),
  `docs/superpowers/specs/2026-05-06-bsky-saves-v0.4-serve-fetch-enrich-threads.md` (v0.4 — `/fetch` introduction).
