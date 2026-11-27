# Project Progress

## Current Focus
Version 0.3.0: the five UI PRs are in `main` (#17 theme, #18 Jobs, #19 repository tabs, #21 settings tabs, #22 Storage page). Now: tag `v0.3.0`, the manual checks of the UI PRs, then the review follow-ups on the Docker host and daily use. See `docs/HANDOFF.md`.

## Open Todos
- [x] UI 1/5: GitHub-style theme, light and dark, with Octicons (PR #17)
- [x] UI 2/5: Jobs page: live panel plus all job runs, with pages and filters; "Recent jobs" leaves the Dashboard (PR #18)
- [x] UI 3/5: Repository tabs: Active (mine, backup on), Other (mine, backup off), Watched (with the watch panel), All (PR #19)
- [x] UI 4/5: Settings tabs: vertical, one URL and one Save button for each section (PR #21)
- [x] UI 5/5: Storage page: folders, marker, disk space, protection copy, sizes of code / metadata / release files, chart (migrations 0032, 0041; PR #22)
- [x] Release 0.3.0 after the five UI PRs (branch `release-0.3.0`)
- [ ] Tag `v0.3.0` on `main` after the release PR is merged
- [ ] Manual checks of PRs #17, #18, #19, #21 and #22 (numbered lists in each PR): phone width, Firefox, the HTMX filters and bulk actions in a real browser
- [x] Code review of 2026-11-26: fix C-1, H-1 to H-4, M-1 to M-7, L-1 to L-5, L-7, L-8 (PR #15)
- [x] Tag `v0.2.0` on `main` after the release PR is merged
- [x] Review follow-up: Settings with a bad schedule, in both themes (the message box was too wide; fixed in PR #21, checked in screenshots)
- [ ] Review follow-up, on the Docker host: restart with the NAS not mounted (jobs must stop with the marker message); one protection copy to the real SMB share; one pull of a repository with Git LFS (token header now limited to github.com)
- [ ] Review follow-up, not done: one GitHub client in place of two (L-6); a container stop during a pull (L-9); the protection copy does not block a job that starts after it, or "Sync now" of selected repositories
- [x] Foundation: migrations, CSRF, tests, CI, light theme (PR #1)
- [x] Setup wizard, folder picker, token in the UI (PR #2)
- [x] Watched public repositories and owners (PR #3)
- [x] Metadata capture: PRs, issues, comments, releases (PR #4)
- [x] Parallel sync engine, live progress, verify job, new pages (PR #5)
- [x] Read-only browser and hosting document (PR #6)
- [x] Clear message for a missing token permission (PR #8)
- [x] One style for form controls; versioned static files (PR #9)
- [x] Optional download of release files (PR #10)
- [x] Browser check of the parts that need JavaScript: theme toggle, live progress, copy buttons, folder picker, "select all"
- [x] Check the layout at phone width (below 500 px) and in Firefox
- [x] First real run of "Download release files" on a private repository
- [x] Decide: dashboard reminder for release files goes away at the first repository (now), or stays until all are on / shows a count
- [ ] Run the cgit service and the `jq` commands from `docs/HOSTING.md`; remove or confirm the [Likely] / [Uncertain] tags
- [x] CI: move `actions/checkout` and `actions/setup-python` off Node.js 20 (deprecation warning)
- [x] Decide on a first version number and tag; then start a dated section in `CHANGELOG.md`
- [ ] Workstation cleanup by hand: the session scratchpad folder (the folder `repo-ripper-wt` is gone), and the screenshot folders `%TEMP%
r-shots-*`
- [x] Tag `v0.1.0` on `main` after the release PR is merged

## Progress Log

### 2026-11-27
- Merged PR #22 (Storage page). Release PR for 0.3.0: version, CHANGELOG section `0.3.0 (2026-11-27)`. The GitHub CI workflow is off (`ci.yml.disabled` since commit 40b0b87), so PRs #17 to #22 had no CI run; `ruff` and `pytest` ran on the workstation.
- Merged PR #21 (settings tabs). Storage page (UI 5/5, branch `storage-page`): folders, disk space, sizes, protection copy, three-line size chart; the chart left the Dashboard. Metadata size now recorded by the metadata job. Header links scroll sideways below 560 px.
- Settings tabs (UI 4/5, branch `settings-tabs`): six tabs, each with its own URL, form and Save button. A save writes only the keys of its tab. Old `POST /settings` removed. Error boxes on Settings no longer wider than the form.
- Deleted the merged branches `progress-handoff` and `review-fixes` (local). The remote branches of PRs #17 to #20 were already gone.
- Planned five UI PRs (theme, Jobs, repository tabs, settings tabs, Storage). Jeremy accepted all recommendations: theme first, third tab "Watched", one Save button for each settings tab, Octicons but no GitHub logo, release as 0.3.0.
- Merged PR #17 (GitHub-style theme, Octicons, D-27), PR #18 (Jobs page) and PR #19 (repository tabs). PR #19 added a fourth tab, "All", not in the plan: the Dashboard links count own and watched repositories together.
- Senior code review of `v0.1.0` (report in `.review/`, not in git): 1 critical, 4 high, 7 medium, 9 low findings. Main theme: the jobs did not check that the mirror folder is the NAS.
- Fixed in PR #15 (branch `review-fixes`): marker file and storage check, protection copy guards and "delete" off by default, kept commits, token header for the GitHub host only, cron check, crash guards, process log, dependency lock. See `CHANGELOG.md` 0.2.0 and D-24 to D-26.
- Version set to 0.2.0: the `v0.1.0` tag was made before PR #15, so the fixes go in a new version.
- Next: UI 4/5 and 5/5, release 0.3.0, the manual checks of #17 to #19, then the review follow-ups and two weeks of daily use.

### 2026-11-26
- Built and merged PRs #1 to #10: wizard, watched repositories, metadata, sync engine, browser, release files, UI fixes. PR #7 repaired three stacked PRs that had merged into the wrong branches.
- Verified against real GitHub (read-only, public data): metadata capture of this repository, release file download of one public release with checksums, and that a push to a PR is picked up by the next capture.
- Jeremy checked by hand, all working: the JavaScript parts in a real browser, phone width and Firefox, and release files on a private repository.
- Decided: GitHub and Docker only, JSON metadata next to mirrors, built-in read-only browser, nothing deleted without confirmation, release files off by default with a reminder. See `docs/DECISIONS.md`.
- Added `CLAUDE.md`, `CHANGELOG.md`, `docs/DECISIONS.md` and this file.
- Version is 0.1.0 (in `app/__init__.py`, shown in the page footer). Reminder rule decided: stays until all active repositories are on (D-23). CI actions updated.
- Tagged `v0.1.0` on `main` (the merge of PR #14).
- Next: daily use, run the unverified parts of `docs/HOSTING.md`.
