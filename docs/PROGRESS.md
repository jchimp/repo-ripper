# Project Progress

## Current Focus
All planned features are in `main` (PRs #1 to #10). Now: use it on the real NAS, do the browser checks that were never done, and fix what that finds.

## Open Todos
- [x] Foundation: migrations, CSRF, tests, CI, light theme (PR #1)
- [x] Setup wizard, folder picker, token in the UI (PR #2)
- [x] Watched public repositories and owners (PR #3)
- [x] Metadata capture: PRs, issues, comments, releases (PR #4)
- [x] Parallel sync engine, live progress, verify job, new pages (PR #5)
- [x] Read-only browser and hosting document (PR #6)
- [x] Clear message for a missing token permission (PR #8)
- [x] One style for form controls; versioned static files (PR #9)
- [x] Optional download of release files (PR #10)
- [ ] Browser check of the parts that need JavaScript: theme toggle, live progress, copy buttons, folder picker, "select all"
- [ ] Check the layout at phone width (below 500 px) and in Firefox
- [ ] First real run of "Download release files" on a private repository
- [ ] Decide: dashboard reminder for release files goes away at the first repository (now), or stays until all are on / shows a count
- [ ] Run the cgit service and the `jq` commands from `docs/HOSTING.md`; remove or confirm the [Likely] / [Uncertain] tags
- [ ] CI: move `actions/checkout` and `actions/setup-python` off Node.js 20 (deprecation warning)
- [ ] Decide on a first version number and tag; then start a dated section in `CHANGELOG.md`
- [ ] Workstation cleanup by hand: `..\repo-ripper-wt` and the session scratchpad folder

## Progress Log

### 2026-11-26
- Built and merged PRs #1 to #10: wizard, watched repositories, metadata, sync engine, browser, release files, UI fixes. PR #7 repaired three stacked PRs that had merged into the wrong branches.
- Verified against real GitHub (read-only, public data): metadata capture of this repository, release file download of one public release with checksums, and that a push to a PR is picked up by the next capture.
- Not verified: anything that needs JavaScript in a real browser, phone width, and a private repository with release files.
- Decided: GitHub and Docker only, JSON metadata next to mirrors, built-in read-only browser, nothing deleted without confirmation, release files off by default with a reminder. See `docs/DECISIONS.md`.
- Added `CLAUDE.md`, `CHANGELOG.md`, `docs/DECISIONS.md` and this file.
- Next: deploy to the NAS host, do the open browser checks, pick a first version number.
