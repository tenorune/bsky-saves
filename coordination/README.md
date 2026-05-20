# bsky-saves coordination branch

This long-lived branch holds **this repo's drafts** of cross-repo coordination contracts. Drafts here are PR'd into the neutral `tenorune/bsky-saves-coordination` repo via the manual-trigger `Coordination PR` workflow (`.github/workflows/coordination-pr.yml` on `main`).

**This branch is never merged to `main`.** Its purpose is to hold transient draft state between rounds of cross-repo revision.

## Layout

```
coordination/
├── README.md                          # this file
└── <topic>/
    ├── <topic>.md                     # this repo's draft of the canonical contract
    └── <topic>-resolved.md            # this repo's draft of the resolved-questions archive (if used)
```

Filenames under each `<topic>/` directory match the canonical filename in the coord repo's `docs/` directory, so the workflow's source-to-target mapping is mechanical.

## Round-trip

See the full convention at https://github.com/tenorune/bsky-saves-coordination/blob/main/README.md. Short version, from this repo's perspective:

1. Read the latest canonical via `WebFetch` against `https://raw.githubusercontent.com/tenorune/bsky-saves-coordination/main/docs/<topic>.md`.
2. Draft revisions on this branch (overwrite the matching file under `coordination/<topic>/`).
3. Commit + push.
4. Maintainer triggers **Coordination PR** from the Actions tab; workflow opens a PR on the coord repo.
5. Once merged on the coord repo, next round starts at step 1.

## Active drafts

| Topic | Draft file | Canonical |
|---|---|---|
| Installer status panel | [`installer-status-panel/installer-status-panel.md`](installer-status-panel/installer-status-panel.md) | [coord-repo:`docs/installer-status-panel.md`](https://github.com/tenorune/bsky-saves-coordination/blob/main/docs/installer-status-panel.md) |
