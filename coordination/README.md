# bsky-saves coordination branch

This long-lived branch holds **this repo's drafts** of cross-repo coordination contracts. Drafts here are PR'd into the neutral `tenorune/bsky-saves-coordination` repo via the manual-trigger `Coordination PR` workflow (`.github/workflows/coordination-pr.yml` on `main`).

**This branch is never merged to `main`.** Its purpose is to hold transient draft state between rounds of cross-repo revision.

## Layout

```
coordination/
└── <topic>/
    ├── <topic>.md                     # this repo's draft of the canonical contract
    ├── <topic>-resolved.md            # this repo's draft of the resolved-questions archive (if used)
    └── manifest.json                  # tells the workflow what to PR and what to call it
```

Filenames under each `<topic>/` directory match the canonical filenames in the coord repo's `docs/` directory, so the manifest's `source` → `target` mapping is mechanical.

## The manifest

Each `<topic>/manifest.json` is a small JSON file describing what one round of revision should PR upstream:

```json
{
  "title": "Round summary — author + key changes",
  "files": [
    { "source": "coordination/<topic>/<topic>.md",          "target": "docs/<topic>.md" },
    { "source": "coordination/<topic>/<topic>-resolved.md", "target": "docs/<topic>-resolved.md" }
  ]
}
```

- `title` — non-empty; becomes both the commit message and the PR title on the coord repo.
- `files` — non-empty array; every `source` path must exist (relative to repo root) and be non-empty. The workflow validates these and fails fast on missing / empty files.
- Single-file revisions list one entry; multi-file revisions (e.g. main doc + resolved-companion together) list both. The workflow applies all files in one commit.

## Round-trip (from this repo's perspective)

See the full convention at https://github.com/tenorune/bsky-saves-coordination/blob/main/README.md. Short version:

1. Read the latest canonical(s) via `WebFetch`:
   - `https://raw.githubusercontent.com/tenorune/bsky-saves-coordination/main/docs/<topic>.md`
   - `https://raw.githubusercontent.com/tenorune/bsky-saves-coordination/main/docs/<topic>-resolved.md` (if it exists)
2. Draft revisions on this branch (overwrite the matching files under `coordination/<topic>/`).
3. Update `coordination/<topic>/manifest.json` with the new round's title and the file list.
4. Commit + push.
5. Maintainer triggers **Coordination PR** from the Actions tab with the manifest path as the input; workflow opens a single PR on the coord repo with all files in one commit.
6. Once merged on the coord repo, next round starts at step 1.

## Active drafts

| Topic | Draft files | Manifest | Canonical |
|---|---|---|---|
| Installer status panel | [`installer-status-panel/installer-status-panel.md`](installer-status-panel/installer-status-panel.md) | [`installer-status-panel/manifest.json`](installer-status-panel/manifest.json) | [coord-repo:`docs/installer-status-panel.md`](https://github.com/tenorune/bsky-saves-coordination/blob/main/docs/installer-status-panel.md) |
