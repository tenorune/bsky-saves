# bsky-saves v0.2 — Image Scope Cut

> **Status:** approved 2026-05-01. Implementation pending.
> **Branch:** `v0.2` in `tenorune/bsky-saves`.
> **Affects:** `tenorune/bsky-saves` (this repo) and `tenorune/tenorune.github.io` (the "stories of 47" project, which is the only current external consumer of `bsky-saves`).

---

## 1. Context

`bsky-saves` was extracted from the stories-of-47 project. Its current `hydrate images` subcommand carries assumptions inherited from that project:

- Discovers image references by walking a directory of Markdown files.
- Reads Jekyll-style YAML frontmatter to extract a per-post `slug:`.
- Stores images under `<assets>/<slug>/` (one subdirectory per post).
- Rewrites Markdown image references to `<url-prefix>/<slug>/<filename>`.

A second consumer ("SavesPlus", working name) is being designed. It will offer JSON, HTML/CSS, and Markdown exports — likely as flat single-page outputs with no per-post slug, no Jekyll frontmatter, and no Markdown-source-to-rewrite. The current `hydrate images` does not fit it.

## 2. Scope decision

**Principle:** `bsky-saves` is the *ingestion* tool. It exists to make a curator's BlueSky data independent of BlueSky (capturing posts, threads, linked article text, and image bytes). It is format-agnostic about *output* — it produces a portable bundle that any downstream tool can transform.

| Subcommand | v0.2 disposition | Reason |
|---|---|---|
| `fetch` | unchanged | Pulls posts from BlueSky into the inventory — core ingestion. |
| `enrich` | unchanged | Cleans timestamps in captured data. |
| `hydrate articles` | unchanged | Captures linked article text before link rot — independence. |
| `hydrate threads` | unchanged | Captures self-thread context from BlueSky — still ingestion. |
| `hydrate images` (download step) | **kept, generalized** | Captures images before `cdn.bsky.app` rotates them — independence. |
| `hydrate images` (Markdown rewrite step) | **removed** | Format-specific transformation. Belongs in consumers. |

Only `hydrate images` changes. All other subcommands are untouched in v0.2.

## 3. New `hydrate images` behavior

### 3.1 CLI

```
bsky-saves hydrate images --inventory PATH --out DIR [--uris FILE]
```

**Flags:**

| Flag | Required? | Meaning |
|---|---|---|
| `--inventory PATH` | yes | Path to the JSON inventory produced by `bsky-saves fetch` (and extended by `enrich`, `hydrate articles`, `hydrate threads`). Read for image URL discovery; written to record `local_images`. |
| `--out DIR` | yes | Directory to download images into. Created if absent. **Flat layout** — no per-post subdirectories. |
| `--uris FILE` | no | Newline-delimited list of `at://...` post URIs. Only entries whose URI is in this list are processed. If omitted, all inventory entries with images are processed. Lines beginning with `#` and blank lines are ignored. URIs in the file but absent from the inventory are silently skipped. |

**Removed flags** (breaking change from v0.1): `--stories`, `--assets`, `--assets-url-prefix`. These are gone entirely; no deprecation warning, no compatibility shim.

### 3.2 Behavior

For each selected inventory entry:

1. Enumerate all image URLs the entry references. (Sources: BlueSky-native post images, quoted-post images, and any other CDN URLs already present in the entry's embed structures. The exact set is whatever the `fetch` and `hydrate threads` steps already record in the inventory schema as of the time of implementation; no new image-URL discovery logic is added here.)
2. For each URL:
   - Compute the deterministic filename (Section 4.2).
   - If `<out-dir>/<filename>` already exists on disk, skip the download (idempotent).
   - Otherwise, download with `httpx`, 30s timeout, custom User-Agent (`bsky-saves/0.2 (+https://github.com/tenorune/bsky-saves)`), `follow_redirects=True`.
   - Per-image failures are non-fatal: counted, logged to stderr, processing continues.
3. Append a `local_images` field to the inventory entry (Section 4.1).

After all entries are processed, write the inventory back atomically (`<file>.tmp` + `os.rename`).

Print a one-line summary to stderr:
```
bsky-saves: processed N entries, downloaded M images, skipped K (already present), F failed
```

### 3.3 What it does NOT do

- Does not read any Markdown files.
- Does not parse YAML frontmatter.
- Does not produce per-slug subdirectories.
- Does not rewrite any source documents.
- Does not know about Jekyll, HTML, slugs, or URL prefixes.

## 4. JSON contract

### 4.1 `local_images` field

Each inventory entry that has at least one image URL (and at least one successful download or pre-existing local file) gains a new field:

```json
{
  "uri": "at://did:plc:.../app.bsky.feed.post/3kxyz...",
  "post_text": "...",
  "embed": {...},
  "local_images": [
    {
      "url": "https://cdn.bsky.app/img/feed_thumbnail/plain/did:plc:.../bafkrei....@jpeg",
      "path": "img-9f2c8e1b4a5d6f70.jpg"
    },
    {
      "url": "https://cdn.bsky.app/img/feed_thumbnail/plain/did:plc:.../bafkrei....@jpeg",
      "path": "img-a14d3b22c8e9f013.jpg"
    }
  ]
}
```

**Semantics:**

- `url`: the exact CDN URL as it appeared in the inventory before download. Stable join key for downstream rewriters.
- `path`: relative to `--out DIR`, never absolute. Downstream consumers join `<out-dir>/<path>` or rebase as needed.
- Order: matches the order in which the URLs are encountered while walking the entry's embeds, so downstream rewriters can rely on positional correspondence if they want.
- Entries with no images get **no** `local_images` field (not `[]`).
- Re-running adds new images to existing `local_images` arrays without duplicating already-recorded URL/path pairs.
- Failed downloads are not recorded in `local_images` — the array reflects only files actually present on disk.

### 4.2 Filename format

Unchanged from v0.1: `img-<first 16 hex chars of sha256(url)>.jpg`.

The `.jpg` extension is hardcoded — BlueSky CDN serves JPEG regardless of the source format. If downstream consumers need accurate content types, they can sniff the file or check the `Content-Type` of a fresh fetch; this is out of scope for v0.2.

### 4.3 Inventory mutation rules

- Existing fields are never modified.
- Only `local_images` is added or extended.
- Inventory writes are atomic (temp file + rename).
- Inventory pretty-printed with `indent=2`, sorted keys (matches existing convention).

## 5. Code changes in `bsky-saves`

### 5.1 `src/bsky_saves/images.py`

**Keep:**
- `DEFAULT_USER_AGENT` (bump version string to `0.2`)
- `TIMEOUT`
- `filename_for_url(url)`
- `download_to(url, dest, *, user_agent)`

**Remove:**
- `IMG_PATTERN` regex
- `slug_from_frontmatter(text)`
- `localize_images(stories_dir, assets_dir, ...)`

**Add:**
- `hydrate_images(inventory_path: Path, out_dir: Path, *, uris: set[str] | None = None) -> tuple[int, int, int, int]` — main entry point. Returns `(entries_processed, downloaded, skipped, failed)`.
- A small helper that walks an inventory entry's embed structure and yields image URLs. Implementation detail — keep it private.

### 5.2 `src/bsky_saves/cli.py`

The `hydrate images` subparser is replaced. New shape:

```python
p_images = hsub.add_parser("images", help="Download CDN images referenced in the inventory.")
_add_inventory_arg(p_images)
p_images.add_argument(
    "--out",
    type=Path,
    required=True,
    help="Directory to download images into (flat; created if absent).",
)
p_images.add_argument(
    "--uris",
    type=Path,
    default=None,
    help="Optional newline-delimited list of at:// post URIs to limit download to. "
         "If omitted, all inventory entries with images are processed.",
)
```

The dispatch in `main()` calls `hydrate_images(args.inventory, args.out, uris=_load_uris(args.uris))`.

`_load_uris` reads the file, strips comments and blanks, returns `set[str] | None` (None if `--uris` not provided).

### 5.3 Tests (`tests/`)

Replace any existing image tests with the following coverage. All tests use a fixture inventory and a temp output dir.

| Test | Asserts |
|---|---|
| `test_hydrate_images_default_processes_all` | No `--uris`: every inventory entry with images is processed. |
| `test_hydrate_images_uris_filter` | With `--uris`: only listed URIs are processed; unlisted entries are untouched. |
| `test_hydrate_images_uris_file_strips_comments_and_blanks` | `--uris` file with `#`-prefixed lines and blanks loads cleanly. |
| `test_hydrate_images_unknown_uri_silently_skipped` | URI present in the `--uris` file but not in inventory does not error. |
| `test_hydrate_images_idempotent_existing_file` | Pre-existing `<out>/<filename>.jpg` is not re-downloaded; mapping still recorded. |
| `test_hydrate_images_idempotent_inventory_field` | Re-running does not duplicate entries in `local_images`. |
| `test_hydrate_images_per_image_failure_nonfatal` | One failing URL does not abort the run; other images proceed; failure counted. |
| `test_hydrate_images_no_images_no_field` | Entries with no image URLs have no `local_images` key (not `[]`). |
| `test_hydrate_images_atomic_write` | Crash-mid-write simulation does not leave the inventory corrupt. (Use temp file + rename inspection.) |
| `test_filename_for_url_deterministic` | Same URL → same filename; different URLs → different filenames; format `img-[0-9a-f]{16}\.jpg`. |

HTTP calls in tests use `respx` (already a dev dependency) to mock `httpx`.

### 5.4 `pyproject.toml`

Bump `version = "0.2.0"`.

### 5.5 `README.md`

Update the `hydrate images` section to reflect the new CLI. No migration notice or deprecation banner.

## 6. Migration plan for stories-of-47

stories-of-47 is the only current consumer of `bsky-saves` and depends on the v0.1 `hydrate images` Markdown rewriting behavior. v0.2 removes that behavior; stories-of-47 must absorb it locally.

### 6.1 Defensive pin (do this first, before any v0.2 work)

In `tenorune.github.io/scripts/requirements.txt`, change:
```
bsky-saves
```
to:
```
bsky-saves>=0.1.0,<0.2
```
Commit. This guarantees the existing build keeps working even if v0.2.0 lands on PyPI before the migration PR is ready.

### 6.2 Move the Jekyll Markdown rewriter into stories-of-47

The logic currently in `bsky-saves`'s `localize_images` (regex-match `![alt](https://cdn.bsky.app/...)`, look up local path, rewrite to `<url-prefix>/<slug>/<filename>`) becomes a stories-of-47-owned script. Suggested location: `tenorune.github.io/scripts/localize_story_images.py`.

Sketch of what the new flow looks like in stories-of-47 (specifics TBD by stories-of-47's implementation Claude):

1. **Build the URI list.** Walk `_stories/*.md`. Read each file's `bluesky_uri:` frontmatter field. Write the collected URIs to a temp file `uris.txt`.

2. **Call the new bsky-saves CLI** to download images for the curated subset:
   ```
   bsky-saves hydrate images \
     --inventory _data/saves_inventory.json \
     --out _data/_image_cache \
     --uris uris.txt
   ```
   This writes images under `_data/_image_cache/` (flat) and appends `local_images` to each curated entry in `_data/saves_inventory.json`.

3. **Run the in-repo rewriter.** `scripts/localize_story_images.py`:
   - Reads `_data/saves_inventory.json` (now containing `local_images` for curated entries).
   - For each `_stories/*.md`:
     - Parses the file's frontmatter to get `slug` and `bluesky_uri`.
     - Looks up the entry in the inventory by URI.
     - For each `local_images` entry on that record, copies `_data/_image_cache/<path>` to `assets/stories/<slug>/<path>` (or moves; or symlinks — implementation choice). The slug-based layout is now stories-of-47's responsibility.
     - Walks the Markdown body for `![alt](https://cdn.bsky.app/...)` matches; for each match, looks up the URL in the entry's `local_images`, rewrites to `/assets/stories/<slug>/<filename>`.
   - Writes the rewritten Markdown back.

4. **Update `scripts/fetch_images.py`** (mentioned in stories-of-47's `2026-04-27-stories-design.md` Section 7 errata) to be a thin orchestrator that runs steps 1–3 above. Or rename it to make the new responsibility obvious.

5. **Verify with `scripts/verify.py` and `scripts/build-check.sh`.** Both should pass unchanged — the rewritten Markdown and the on-disk asset layout end up identical to today's output.

### 6.3 Integration test branch (before final release)

While bsky-saves's `v0.2` branch is in development:

1. Create stories-of-47 branch `migrate-bsky-saves-v0.2`.
2. In its `requirements.txt`, replace the v0.1 pin with a git install:
   ```
   bsky-saves @ git+https://github.com/tenorune/bsky-saves@v0.2
   ```
3. Implement steps 6.2 (rewriter, build wiring) on this branch.
4. Run the full stories-of-47 build (`scripts/verify.py` + `bundle exec jekyll build` + `scripts/build-check.sh`).
5. Diff the resulting `_site/assets/stories/` and `_stories/*.md` against the current main-branch baseline. Expected diff: zero (the goal is byte-for-byte equivalent output, modulo any incidental whitespace).
6. Iterate on both branches until step 5 is clean.

### 6.4 Final release cutover

Once the integration branch is green:

1. **bsky-saves:** merge `v0.2 → main`, tag `v0.2.0`, push tag. The `release.yml` workflow publishes to PyPI.
2. **stories-of-47:** in the `migrate-bsky-saves-v0.2` PR, change `requirements.txt` from the git install to:
   ```
   bsky-saves>=0.2.0,<0.3
   ```
   Merge the PR.
3. Smoke-test the next scheduled stories-of-47 build/run.

### 6.5 Rollback

If v0.2.0 has a critical bug discovered after release:

- stories-of-47: emergency PR re-pinning to `bsky-saves==0.1.0` and reverting the rewriter migration commit. The v0.1.0 PyPI wheel never goes away.
- bsky-saves: investigate, fix on `v0.2` branch, cut `v0.2.1`. Stories-of-47 unpins again.

## 7. Test plan (the gate before pushing the v0.2.0 tag)

All of the following must pass before merging `v0.2 → main`:

### 7.1 Unit tests in bsky-saves

```
pytest tests/
```
The full suite from Section 5.3, plus existing `test_fetch.py`, `test_normalize.py`, `test_tid.py`. All green.

### 7.2 Build artifact verification

```
pip install build twine
python -m build
twine check dist/*
```
Both the sdist and wheel must build cleanly and pass `twine check`.

### 7.3 Clean-venv install smoke test

```
python -m venv /tmp/v02-smoke
/tmp/v02-smoke/bin/pip install dist/bsky_saves-0.2.0-py3-none-any.whl
/tmp/v02-smoke/bin/bsky-saves --help
/tmp/v02-smoke/bin/bsky-saves hydrate images --help
```
The help output must show only the new flags (`--inventory`, `--out`, `--uris`); no `--stories`, `--assets`, or `--assets-url-prefix` may appear anywhere.

### 7.4 Local end-to-end smoke against a fixture inventory

Use a small fixture (3–5 entries with mocked image URLs, served by a local HTTP fixture or `respx`-style stub if running inside pytest). Run:
```
bsky-saves hydrate images --inventory fixture.json --out /tmp/imgs
```
Verify:
- All expected files appear in `/tmp/imgs/`.
- `fixture.json` gains `local_images` on the right entries.
- Re-running is a no-op (no re-downloads; no inventory diff).

### 7.5 Stories-of-47 integration test (the critical gate)

The `migrate-bsky-saves-v0.2` branch in stories-of-47 (Section 6.3) must:
- Install `bsky-saves` from the v0.2 branch via git.
- Run the full build pipeline (`scripts/verify.py`, `bundle exec jekyll build`, `scripts/build-check.sh`) successfully.
- Produce `_site/assets/stories/` and `_stories/*.md` content that diffs cleanly against the current main-branch baseline (no functional differences).

This is the most important gate — it is the actual evidence that the scope cut hasn't broken the only existing consumer.

### 7.6 Post-publish verification

After tagging `v0.2.0` and the release workflow runs:
- `pip install bsky-saves==0.2.0` in a clean venv succeeds.
- The smoke from 7.3 passes against the PyPI wheel.
- stories-of-47's PR (Section 6.4 step 2) flips its dependency from git install to PyPI; one full build run after merge confirms the published wheel works.

## 8. Out of scope for v0.2 (YAGNI)

Documented to prevent scope creep:

| Deferred | Trigger to revisit |
|---|---|
| Sharded image directory layout (`<out>/9f/2c/img-...`) | If any consumer's image count exceeds ~10k in one directory. |
| Per-image granularity in `--uris` (cherry-picking which images of a post to download) | If a consumer's UX requires it. Today both consumers want all-images-per-selected-post. |
| Sniffing/preserving accurate image content type and extension | If a consumer renders to a context that cares (e.g., needs `.png` for transparency). |
| Refactoring `hydrate articles` or `hydrate threads` along similar lines | They already operate on the inventory — no scope cut needed. |
| Versioned inventory schema (e.g., a top-level `schema_version`) | If a future change requires migration logic. The append-new-fields-only convention is sufficient for now. |
| Compatibility shim that emulates v0.1 `--stories/--assets` flags | Explicitly rejected: clean break. |

## 9. Decisions log

| Date | Decision |
|---|---|
| 2026-05-01 | Scope: `bsky-saves` becomes ingestion-only; transformation moves to consumers. |
| 2026-05-01 | Subset granularity: per-post (URIs), not per-image. |
| 2026-05-01 | Layout: flat directory, no per-slug subdirs, no hash sharding. |
| 2026-05-01 | URL→path mapping recorded in the inventory as `local_images` field, not in a sidecar manifest. |
| 2026-05-01 | Migration: clean break (no compat shim). |
| 2026-05-01 | Release strategy: defensive pin → branch dev → git-install integration test → final release. RC step skipped. |
| 2026-05-01 | Jekyll Markdown rewriter relocates to stories-of-47, not to a separate package. |
