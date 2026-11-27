# Project Progress

## Current Focus
UI work for 0.3.0, five PRs in this order: GitHub-style theme, Jobs page, repository tabs, settings tabs, Storage page. Then one release PR. The review follow-up checks below are still open.

## Open Todos
- [x] UI 1/5: GitHub-style theme, light and dark, with Octicons (PR #17)
- [x] UI 2/5: Jobs page: live panel plus all job runs, with pages and filters; "Recent jobs" leaves the Dashboard (branch `jobs-page`)
- [ ] UI 3/5: Repository tabs: Active (mine, backup on), Other (mine, backup off), Watched (with the watch panel)
- [ ] UI 4/5: Settings tabs: vertical, one URL and one Save button for each section
- [ ] UI 5/5: Storage page: folders, marker, disk space, protection copy, sizes of code / metadata / release files, charts (migration 0032)
- [ ] Release 0.3.0 after the five UI PRs
- [x] Code review of 2026-11-26: fix C-1, H-1 to H-4, M-1 to M-7, L-1 to L-5, L-7, L-8 (PR #15)
- [x] Tag `v0.2.0` on `main` after the release PR is merged
- [ ] Review follow-up, by hand in a browser: Settings with a bad schedule, in both themes (the message box can be too wide)
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
- [ ] Workstation cleanup by hand: the session scratchpad folder (the folder `repo-ripper-wt` is gone)
- [x] Tag `v0.1.0` on `main` after the release PR is merged

## Progress Log

### 2026-11-27
- Planned five UI PRs (theme, Jobs, repository tabs, settings tabs, Storage). Jeremy accepted all recommendations: theme first; Dashboard keeps the live panel and "Run now"; third tab is "Watched"; protection settings stay on Settings; one Save button for each settings tab; Octicons but no GitHub logo; release as 0.3.0.
- UI 1/5 built on `theme-github`: Primer colours, GitHub-style components, Octicons in `app/icons.py` (D-27). Merged as PR #17.
- UI 2/5 built on `jobs-page`: `/jobs` with the live panel, filters and pages; job run routes moved from `routes/dashboard.py` to `routes/job_runs.py`.
- Senior code review of `v0.1.0` (report in `.review/`, not in git): 1 critical, 4 high, 7 medium, 9 low findings. Main theme: the jobs did not check that the mirror folder is the NAS.
- Fixed on the branch `review-fixes`: marker file and storage check, protection copy guards and "delete" off by default, kept commits and `repo-ripper-lost-refs.log`, token header for the GitHub host only, cron check, crash guard for scan and protection, process log, dependency update with a lock and a CI audit. See `CHANGELOG.md`, section 0.2.0, and D-24 to D-26.
- Merged as PR #15. Version set to 0.2.0: the `v0.1.0` tag was made before PR #15, so the fixes go in a new version, not in 0.1.0.
- Next: the three follow-up todos above, then two weeks of daily use before the project is announced.

### 2026-11-26
- Built and merged PRs #1 to #10: wizard, watched repositories, metadata, sync engine, browser, release files, UI fixes. PR #7 repaired three stacked PRs that had merged into the wrong branches.
- Verified against real GitHub (read-only, public data): metadata capture of this repository, release file download of one public release with checksums, and that a push to a PR is picked up by the next capture.
- Jeremy checked by hand, all working: the JavaScript parts in a real browser, phone width and Firefox, and release files on a private repository.
- Decided: GitHub and Docker only, JSON metadata next to mirrors, built-in read-only browser, nothing deleted without confirmation, release files off by default with a reminder. See `docs/DECISIONS.md`.
- Added `CLAUDE.md`, `CHANGELOG.md`, `docs/DECISIONS.md` and this file.
- Version is 0.1.0 (in `app/__init__.py`, shown in the page footer). Reminder rule decided: stays until all active repositories are on (D-23). CI actions updated.
- Tagged `v0.1.0` on `main` (the merge of PR #14).
- Next: daily use, run the unverified parts of `docs/HOSTING.md`.
