# `bsky-saves` helper: protocol versioning

`GET /ping` includes a `protocol` field — a stable integer-as-string
that consumers (notably `bsky-saves-gui`'s `probeHelper()`) read to
decide whether the helper they're talking to is API-compatible.

## When `protocol` bumps

`protocol` bumps **only on non-additive changes** to the helper's
HTTP surface. Specifically, bumps when any of the following is true
for an *existing* endpoint:

- request body schema changes in a way that breaks old clients
  (renamed required field, type change, removed accepted shape)
- response body schema changes in a way that breaks old clients
  (renamed field, type change, removed field)
- status-code semantics change (e.g. a failure mode that returned 400
  now returns 422, or 200 grows a new "soft-fail" wrapper)
- authentication requirement changes (anonymous → authenticated, or
  auth header format changes)

## When `protocol` does *not* bump

These changes are additive and consumers must feature-detect rather
than gate wholesale on `protocol`:

- adding a new endpoint
- adding a new optional field to an existing request body
- adding a new field to an existing response body
- adding a new entry to a feature list
- adding a new optional header
- bug fixes that preserve the documented contract

## Current value

`protocol = "2"` — current as of `bsky-saves` v0.7.0.

## Changelog

- `"1"` — `bsky-saves` v0.6.1. Initial value when `protocol` was added to `/ping`.
- `"2"` — `bsky-saves` v0.7.0. `Authorization: Bearer <token>` now required on
  all credentialed endpoints (`/fetch`, `/fetch-image`, `/extract-article`,
  `/enrich`, `/hydrate-threads`). `/ping` and OPTIONS preflight remain unauth.
  See `docs/superpowers/specs/2026-05-16-bsky-saves-v0.7.0-session-token.md`.

## Cross-repo coupling

`bsky-saves-gui`'s `lib/min-helper-version.ts` and `probeHelper()`
read the same `/ping` response. When `protocol` bumps here, the
GUI side decides independently whether the bump represents a
breakage it can absorb (per-feature fallbacks) or one that should
gate sign-in behind a helper upgrade prompt. The `helper` semver
(returned as `version` in `/ping`'s response) remains the source of
truth for "is the helper installation new enough"; `protocol` is the
coarse-grained compat-band signal layered on top.
