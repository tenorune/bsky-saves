# `bsky-saves-installers` bootstrap brief (helper-side contribution)

## 0. TL;DR

`bsky-saves-installers` is a new sibling repo whose goal is to ship native installers for the `bsky-saves` + `bsky-saves-gui` stack — extending the reachable user base beyond "people willing to `pipx install bsky-saves` from a terminal." The installer's scope, platforms, bundling strategy, code-signing posture, and distribution channels are **not yet decided** — the first session in the new repo should brainstorm those decisions before any code lands.

The two existing repos (`tenorune/bsky-saves` and `tenorune/bsky-saves-gui`) are released, coordinated, and stable as of mid-May 2026: latest is `bsky-saves` v0.6.4 (bundling `bsky-saves-gui` v0.6.2). The current installation flow is `pipx install bsky-saves; bsky-saves serve --gui` (CLI users) or `pipx install bsky-saves; bsky-saves serve; visit https://saves.lightseed.net` (hosted-PWA users). The installer effort is **additive** — it must not break either of those paths.

## 1. What `bsky-saves` and `bsky-saves-gui` are

### `bsky-saves` (helper / CLI)

- Python ≥3.11 package on PyPI.
- Two surfaces:
  - **CLI**: `bsky-saves fetch / hydrate / enrich / serve / token`. Ingests a BlueSky account's bookmarks into a JSON inventory (`saves_inventory.json`), hydrates them with article text / thread context / images, and exposes a local HTTP helper daemon.
  - **HTTP helper daemon** (`bsky-saves serve`): binds `127.0.0.1`, default port `47826`. Serves a small JSON API plus (optionally, via `serve --gui`) the bundled static GUI at `/`.
- Pure-Python implementation with a small dependency footprint (`httpx`, `trafilatura`, `hatchling` for build).
- **Vendors `bsky-saves-gui`'s static `dist.tar.gz` at wheel-build time** via a Hatch custom build hook (`hatch_build.py` → `scripts/fetch_gui.py`). The vendor pipeline is SHA-pinned: `pyproject.toml` declares `[tool.bsky-saves] gui_version = "x.y.z"`, `gui-dist.sha256` records the expected tarball hash, and the hook fails the build if the downloaded tarball doesn't match. The vendored tree lands at `src/bsky_saves/_gui/` inside the wheel.

### `bsky-saves-gui` (companion PWA)

- Svelte/Vite static web app, distributed two ways:
  1. **Bundled** into the `bsky-saves` wheel (above) and served from `serve --gui` at `http://127.0.0.1:47826/`.
  2. **Hosted** at `https://saves.lightseed.net` (independent deployment cadence).
- Talks to the helper daemon via the JSON API. CORS bridge — the GUI can't talk to BlueSky's CDN or arbitrary article URLs directly (browser CORS), so the helper proxies those calls.
- Includes a Pyodide runtime as a fallback for users who don't have the helper running (offline-ish degraded mode).
- *GUI-team to fill in:* exact runtime requirements (browser baseline), build process, hosted deployment infrastructure, asset size.

### How they install today

```
pipx install bsky-saves                    # 1: install the Python package
bsky-saves serve --gui                     # 2: start helper + bundled GUI on localhost
                                           #    → open http://127.0.0.1:47826/

  OR

bsky-saves serve                           # 2': start helper only
                                           # 3': visit https://saves.lightseed.net
                                           # 4': paste pairing token when prompted
```

Both paths require Python 3.11+ and the ability to run `pipx`. **That is the gap the installers close.**

## 2. The installer goal

### What's not in dispute

- v0.7.0 of `bsky-saves` is **reserved as the version label for the installer milestone** (during the v0.6.x release cycle we explicitly renamed in-progress work from v0.7.0 to v0.6.2/3/4 to keep v0.7.0 free). When the installer ships, `bsky-saves` v0.7.0 is the helper release that gets bundled into it.
- The installer is **additive**, not a replacement. The `pipx install bsky-saves` flow continues to work post-installer.

### What's not yet decided (the brainstorming agenda)

These are the design-space questions the new session must resolve before any code lands.

1. **Target platforms.** macOS only? macOS + Windows? All three (incl. Linux)? Each adds packaging tooling, signing infrastructure, and per-platform CI cost.
2. **Bundling strategy.** PyInstaller (mature, single-file bundles), Briefcase (BeeWare cross-platform installers, more opinionated about app structure), Nuitka (compiles Python to C, fastest but rougher edges), Docker (different audience entirely — assumes Docker is installed). The right pick depends on platforms + the user mental-model.
3. **Install experience.** Just-put-binary-on-PATH? Or also wire up a system service (launchd / systemd / Windows Service) so `bsky-saves serve` runs on login? A system tray icon? A URL shortcut that opens the GUI?
4. **Code signing.** Without it, macOS Gatekeeper and Windows SmartScreen both warn users on first launch. Apple Developer ID is $99/yr; Windows Authenticode certs vary by CA. Linux is comparatively easy. This decision drives distribution UX more than any other.
5. **Distribution channels.** GitHub Releases (download-and-run) is free and easy. Brew tap / winget / AUR / Snap / Flatpak each have their own submission flow and audience. Pick a target audience and the channel often falls out.
6. **What the installer is *for*.** Best framing: **who is the new user that the installer reaches?** Today's user is "someone willing to run `pipx install`." An installer expands that to ... who, exactly? Some plausible audiences:
   - A friend who got a JSON archive from you and wants to browse it locally.
   - A non-technical Bluesky user who wants the local-archive privacy story without learning CLI.
   - A power user who wants the daemon running as a system service rather than as a foreground terminal process.
   - A future user of features that don't exist yet (e.g., a desktop app shell with a tray icon, push notifications, etc.).

   The answer changes every other decision; resist locking the others before this one is clear.

### Likely first session in the new repo

Brainstorm → spec → plan → first implementation slice. Use `superpowers:brainstorming` to walk through the 6 questions above. Output goes to `docs/superpowers/specs/YYYY-MM-DD-bsky-saves-installer-v0.1.md` (or similar — the new repo can pick its own version axis distinct from bsky-saves'). Each platform / each bundling strategy can be its own task in the plan; expect the first plan to be larger than the v0.6.0/0.6.2 plans we did in the helper repo, because there's more inherent platform diversity.

## 3. Helper-side constraints the installer inherits

These are non-negotiable shapes of the system that the installer must respect or actively work around.

### 3.1 Python runtime

- `bsky-saves` requires **Python ≥3.11**. The wheel declares `requires-python = ">=3.11"` in `pyproject.toml`. There is a `tomli` shim for Python <3.11 in the build path, but the runtime is firm.
- Dependencies (`httpx`, `trafilatura`) pull in compiled C extensions (libxml2 family for trafilatura). Whatever bundler the installer uses must produce platform-correct binary wheels — meaning per-OS, per-arch builds. macOS arm64 + x86_64 are both relevant; Windows x64; Linux x86_64 (and arm64 increasingly).

### 3.2 The wheel already bundles the GUI

`bsky-saves`'s wheel build hook (`hatch_build.py` + `scripts/fetch_gui.py`) downloads `bsky-saves-gui`'s pinned `dist.tar.gz` from GitHub Releases, verifies the SHA-256 against `gui-dist.sha256`, and extracts it into `src/bsky_saves/_gui/`. The artifact is included in the wheel via `[tool.hatch.build] artifacts = ["src/bsky_saves/_gui/**"]`.

**Implication for the installer:** the installer DOES NOT need to ship the GUI separately. Once it bundles `bsky-saves`'s wheel, the GUI rides along. The installer's job is to bundle Python + the wheel + dependencies — not to re-implement the vendor pipeline. (The hosted PWA at `saves.lightseed.net` is a separate deployment artifact, irrelevant to the installer.)

### 3.3 Helper daemon must keep working as a localhost-only process

`bsky-saves serve` binds `127.0.0.1` only. The helper validates `Host` header, enforces an `Origin` allowlist (`http://127.0.0.1:<port>`, `http://localhost:<port>`, `https://saves.lightseed.net`, plus user-added via `--allow-origin`), and (since v0.6.2) requires `Authorization: Bearer <token>` on every credentialed endpoint. **The installer must not change any of this** — running as a system service on a different bind or different port without surfacing the token + port to the user breaks pairing.

If the installer chooses to wire `bsky-saves serve` up as a system service (launchd plist / systemd unit / Windows Service), it must:

- Use the same port the user expects (47826 by default).
- Run as the user (so config-dir paths and 0o600 token file resolve correctly), not as root / SYSTEM.
- Capture stdout/stderr in a place that respects the pairing-token-leak concern (per v0.6.4: the helper now prints the token at first run; system-service log files should be 0o600 or equivalent to avoid leaking).

### 3.4 Token storage is at platform-conventional paths

The pairing token lives at:

| Platform | Path |
|---|---|
| Linux/*BSD | `$XDG_CONFIG_HOME/bsky-saves/token` or `~/.config/bsky-saves/token` |
| macOS | `~/Library/Application Support/bsky-saves/token` |
| Windows | `%APPDATA%\bsky-saves\token` |

`0o600` perms; lazy-generated by `bsky-saves serve` or `bsky-saves token`. The installer **must not** override these paths — both the helper and the GUI's auto-pairing flow assume them.

### 3.5 The cross-repo pin-bump dispatch

When `bsky-saves-gui` tags a new release, its `release.yml` fires a `repository_dispatch` event into `tenorune/bsky-saves` (event_type: `gui-version-bump`). A workflow on this side (`gui-version-bump.yml`) verifies the released tarball's SHA, updates `[tool.bsky-saves] gui_version` + `gui-dist.sha256`, and opens a PR. CI runs on the auto-PR via a fine-grained PAT (`BSKY_SAVES_GUI_BUMP_PR_TOKEN`) — `GITHUB_TOKEN`-authored PRs don't trigger downstream workflows.

**Implication for the installer repo:** if the installer is *also* triggered by GUI tags (e.g., "rebuild installer whenever bundled GUI updates"), reuse the same dispatch pattern with a new event type, a new receiver workflow on the installer repo, and a third PAT scoped to the installer repo. Don't reinvent this loop.

### 3.6 The version contract

- `bsky-saves` ships a `protocol` field in `/ping`'s response. Current value is `"2"`. The GUI's `MIN_HELPER_VERSION` (currently `0.6.3`) gates the GUI's release-gate against the latest PyPI helper. Bump rules are documented at `docs/protocol-versioning.md`.
- For the installer's purposes: the installer bundles a specific `bsky-saves` version. The bundled `bsky-saves` reports its own `protocol` via `/ping`; the GUI talks to it normally. The installer doesn't need its own protocol — it's a delivery vehicle, not a protocol participant.

## 4. Conventions the installer repo should inherit

These came out of the `bsky-saves` and `bsky-saves-gui` workflows and are worth preserving for consistency.

### 4.1 Branch & PR discipline

- All work on feature branches; `main` is protected.
- Branch naming: `claude/<descriptive-name>-<optional-suffix>` for Claude-driven work, or `<bare-descriptive-name>` for human-driven.
- **PRs are user-initiated only** — Claude should not auto-open PRs without explicit user say-so. This was a hard rule throughout the v0.6.x work. Branch creation is similarly gated: ask before creating.
- Force-pushes to feature branches are fine after merge (the branch is dead); never force-push to `main`.

### 4.2 Conventional commits

`feat(scope):`, `fix(scope):`, `docs(...):`, `test(...):`, `build(...):`, `chore(...):`, `refactor(...):`, `ci(...):`, `release(...)`. Short subject; body paragraph (or two) for non-trivial commits explaining the *why*, not the *what*.

### 4.3 Specs/plans pipeline

Non-trivial work goes through:

1. **Brainstorm** via `superpowers:brainstorming` skill.
2. **Spec** at `docs/superpowers/specs/YYYY-MM-DD-<topic>.md` — the design doc, locked once approved.
3. **Plan** at `docs/superpowers/plans/YYYY-MM-DD-<topic>.md` — bite-sized TDD tasks built from the spec.
4. **Subagent-driven implementation** (the `superpowers:subagent-driven-development` skill) executes the plan task-by-task with two-stage review (spec compliance, then code quality).

The `bsky-saves` repo has 4 spec/plan pairs (v0.4.4, v0.5.0, v0.6.0, v0.6.2) — useful templates to read.

### 4.4 Testing

- pytest, run via `python -m pytest -q`. `bsky-saves` runs to 392 tests on Python 3.11 + 3.12. The installer repo should aim for comparable discipline — every behavioral change accompanied by tests; CI gates on test pass.
- For installer-specific work: each platform's smoke test should boot the installed artifact, exercise the GUI auto-pairing flow end-to-end, and verify the token-storage path matches the platform convention.

### 4.5 Release flow

- Tag `vX.Y.Z` on `main` → release workflow fires → publishes the artifact.
- For `bsky-saves` the artifact is a wheel on PyPI; for `bsky-saves-gui` it's a `dist.tar.gz` on GitHub Releases. For the installer repo it'll be platform-specific installers attached to GitHub Releases (and possibly external channels per the brainstorming).
- **Release notes** in the GitHub release body — see `bsky-saves` v0.6.1 / v0.6.2 / v0.6.3 / v0.6.4 release pages for the established voice and structure.
- **Coordination messages** to peer repos are part of the release flow when shipping cross-repo changes.

### 4.6 User preferences

- **Prose options over UI pickers.** When asking for direction, present numbered prose options. Don't use `AskUserQuestion` UI pickers — they block the chat scroll-back and are hard to read.
- **Diagnose → recommend → ask → fix.** When a question or bug surfaces, investigate, propose options, present a recommendation, ask for go-ahead, then act. Don't auto-apply unrequested changes.
- **Hands-on, drives the work.** The user confirms steps and merges PRs themselves. Claude prepares the work, ships PRs on request, surfaces blockers; the user owns the release/merge/tag flow.
- **Evidence-based diagnosis.** When the user reports a bug, they typically paste real output / errors / data. Read it carefully; don't guess.
- **Force-pushing.** Fine on feature branches that have been merged. Never on `main`.

## 5. The cross-repo coupling map

| From | To | Mechanism | Trigger |
|---|---|---|---|
| `bsky-saves-gui` (tag push) | `bsky-saves` | `repository_dispatch` event `gui-version-bump` | every GUI tag → auto-PR bumps the pin |
| `bsky-saves` runtime | `bsky-saves-gui` runtime | `/ping`'s `protocol`, `version`, `gui_bundled`, `features` fields | GUI's `probeHelper()` reads on connect |
| `bsky-saves` runtime | GUI's pairing recovery | `WWW-Authenticate: Bearer realm="bsky-saves"` on 401s from `_check_token` | GUI's 401 interceptor distinguishes pairing-401 vs upstream-PDS-401 |
| `bsky-saves-gui` runtime | `bsky-saves` runtime | `Authorization: Bearer <token>` on every credentialed call | every API call from the GUI |
| `bsky-saves-gui` release gate | PyPI `bsky-saves` latest | `MIN_HELPER_VERSION` constant in GUI source | GUI release fails if PyPI's latest is below this |
| `bsky-saves` wheel build | `bsky-saves-gui` releases | `gui_version` pin + `gui-dist.sha256` | wheel-build downloads + verifies tarball |

For the installer:

- `bsky-saves-installers` → `bsky-saves` (PyPI): consume a specific wheel.
- `bsky-saves-installers` ← `bsky-saves` (tag push): could trigger an installer rebuild via `repository_dispatch`. Symmetrical to the `gui-version-bump` pattern.
- `bsky-saves-installers` ← `bsky-saves-gui` (tag push): probably *not* a direct coupling — the GUI is vendored *into* `bsky-saves`'s wheel, so the installer transitively gets new GUIs by rebuilding against new `bsky-saves` releases. The installer doesn't need a direct GUI dispatch.
- `bsky-saves-installers` → users: GitHub Releases with platform-specific artifacts. Update mechanism (in-app self-update? release feed? brew/winget?) TBD in brainstorming.

## 6. Concrete starting points

When the new Claude session boots in `tenorune/bsky-saves-installers`, it should:

### 6.1 First-session reading list

In this order, before any creative work:

1. **This document** — the bootstrap.
2. **The GUI team's parallel bootstrap doc** — when it lands.
3. `bsky-saves` README — installation surface today, the helper daemon, the pairing model.
4. `bsky-saves` `docs/superpowers/specs/2026-05-16-bsky-saves-v0.6.2-session-token.md` — full design of the token-pairing model. The installer must understand this because installer-bundled helpers will go through the same pairing flow on every machine.
5. `bsky-saves` `.github/workflows/gui-version-bump.yml` — the cross-repo dispatch receiver template, useful if the installer repo wants its own dispatch loop.
6. `bsky-saves` `.github/workflows/release.yml` — the PyPI release pipeline, useful as a structural template for the installer repo's per-platform release workflow.
7. `bsky-saves` `scripts/fetch_gui.py` + `hatch_build.py` — the build-hook pattern for downloading + SHA-verifying upstream artifacts. The installer will do something analogous (download + verify the `bsky-saves` wheel).

### 6.2 First-session action

Invoke `superpowers:brainstorming` and resolve the 6 design questions in §2 above. End the brainstorm with a one-paragraph "installer v0.1 = this and not that" statement, and commit it to `docs/brainstorming/YYYY-MM-DD-installer-scope.md` (or whatever the new repo's convention will be).

Do not write any installer code until the brainstorming output is approved.

### 6.3 First-PR shape (post-brainstorm)

When the brainstorm lands, the natural first PR is:

- Initial repo scaffolding: `pyproject.toml` (if Python-based bundling) or `Cargo.toml` / `package.json` / shell scripts as appropriate, `.github/workflows/verify.yml` and per-platform `build-installer.yml`, README skeleton, `docs/superpowers/specs/...md` placeholder, `LICENSE`, `.gitignore`.
- Single platform first (likely macOS, given that's typically the highest-friction install experience and the easiest to test natively from a Mac). Add Windows + Linux as subsequent PRs once the macOS flow proves the architecture.

## 7. Open questions / unknowns

These are things to surface during brainstorming; the bootstrap doesn't pre-answer them.

1. **Update mechanism.** How do installer users get a new `bsky-saves` version? Self-update? "Re-download installer from GitHub Releases" link? `brew upgrade` etc.? Each option has different UX and code-signing implications.
2. **Coexistence with `pipx install bsky-saves`.** If a user installs via the installer AND has `pipx install bsky-saves` from before, which `bsky-saves` does their shell `PATH` resolve to? Token file is shared (same `$XDG_CONFIG_HOME/bsky-saves/token`), so they're consistent, but they might end up with two daemons on the same port.
3. **macOS notarization.** Distinct from Apple Developer ID code-signing — required for full Gatekeeper trust since macOS 10.15. Adds time per build (Apple's notarization service is asynchronous).
4. **Windows: MSIX vs MSI vs portable EXE.** Each has different update / install / uninstall semantics. MSIX is newer and Windows-Store-friendly; MSI is the traditional enterprise format; portable EXE skips the registry entirely.
5. **Linux: cross-distro packaging.** AppImage works everywhere but is unloved by some distros. Snap is Ubuntu-favored. Flatpak is Fedora-favored. `.deb` + `.rpm` are traditional but per-distro. Or just ship a tarball and let users figure it out.
6. **Audience research.** Has anyone actually asked end-users what they want? The decision-tree above could collapse dramatically with one signal like "we have N users on Mac waiting for a `.dmg`."
7. **Hosted-PWA case.** Does the installer need to make `saves.lightseed.net` reachable, or do we assume the user lands there via a browser bookmark / link? The current pairing flow is "user visits saves.lightseed.net, gets prompted for token, pastes it from helper terminal output." Installer could ship a desktop shortcut that opens `https://saves.lightseed.net` automatically. Or could pre-open it post-install.
8. **The "is this worth it" question.** `pipx install` is *one command*. The audience-expansion the installer offers vs. the engineering cost (per-platform CI, code-signing infrastructure, update mechanism, ongoing maintenance) should be made explicit in the brainstorm. It's possible the right answer is "for now, improve the README's onboarding paragraph and a `pipx`-shaped one-liner; revisit installers when there's user demand."

## 8. State of the world at handoff (helper side)

For sanity-checking when the new session starts:

- `bsky-saves` `main` HEAD: post-#16 merge (gui-bump v0.6.1 → v0.6.2 auto-PR merged); v0.6.4 ready to tag (or just tagged depending on timing).
- PyPI: latest release is whatever's been tagged most recently — verify via `pip index versions bsky-saves`.
- Bundled GUI in latest wheel: `bsky-saves-gui` v0.6.2.
- Open PRs on `bsky-saves`: should be empty post-merge. PR #15 was v0.6.4 first-run print; PR #16 was the GUI bump; both merged.
- Cross-repo: `BSKY_SAVES_DISPATCH_TOKEN` on `bsky-saves-gui` and `BSKY_SAVES_GUI_BUMP_PR_TOKEN` on `bsky-saves` are both live; the dispatch loop has fired twice (PR #11, PR #16) and works end-to-end.

---

If anything in the above is wrong, missing, or contradicted by the GUI team's parallel doc, defer to the most recent source: the actual code in `tenorune/bsky-saves` and `tenorune/bsky-saves-gui` is authoritative; this document is a guide to find your way in.
