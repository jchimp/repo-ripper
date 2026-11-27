# Handoff — repo-ripper
_Last updated: 2026-11-27 · branch: `release-0.3.0` (PR open) · phase: release 0.3.0_

## Status

The UI work for 0.3.0 has five PRs. All are merged:

| # | Part | PR | State |
|---|---|---|---|
| 1 | GitHub-style theme, light and dark, Octicons (decision D-27) | #17 | merged |
| 2 | Jobs page `/jobs`: live panel, all job runs, filters, pages | #18 | merged |
| 3 | Repository tabs: Active, Other, Watched, All | #19 | merged |
| 4 | Settings tabs: six tabs, one form and Save button each | #21 | merged |
| 5 | Storage page `/storage`: folders, sizes, protection copy, size chart | #22 | merged |
| — | Release 0.3.0 (version, CHANGELOG section, tag `v0.3.0`) | release-0.3.0 | in review; tag after the merge |

- **Verified:** `ruff` clean and `pytest` green at each merge (523 passed, 1 skipped
  at PR #22, workstation only: CI is off, see below). Headless-browser screenshots in both themes of every page that changed.
- **Not verified:** phone width, Firefox, and the HTMX parts (filters, bulk buttons,
  "Stop watching" on the All tab, theme toggle) in a real browser. Each PR has a
  numbered manual check list. This is an open todo in `docs/PROGRESS.md`.
- **Known limit, not new:** the Backup switch in one repository row changes only that
  row. With tabs, the row stays on the old tab until the page loads again.
- **CI is off:** `.github/workflows/ci.yml` was renamed to `ci.yml.disabled` (commit 40b0b87,
  2026-11-27). PRs #17 to #22 had no CI run. `CLAUDE.md` still says CI runs.
- **Pending for Jeremy:** the manual checks of #17, #18, #19, #21 and #22; the older review
  follow-ups in `docs/PROGRESS.md`; remove the folders `%TEMP%\rr-shots-*` (left by
  the screenshot script, the delete was not allowed in the session).

## This session (storage page)

- Merged #21. Branch `storage-page` (Jeremy's answers: chart with three lines; fix the
  header at phone width; "not measured" until the next metadata run).
- `app/routes/storage.py` + `templates/storage.html`. The page reads sizes from the
  database only; on a page load it checks the marker file and calls
  `shutil.disk_usage` for the two folders (an `OSError` shows "not available").
- Sizes: `repos.size_bytes` (code, pull job), `repos.meta_bytes` (new, migration 0032,
  `store.meta_bytes()` in the metadata job, without `assets/`), `repos.assets_bytes`
  (release files job, existed). `sync_history` got `meta_bytes` and `assets_bytes`
  (migration 0041); the pull job stores the totals from `db.size_totals()`.
- `viewhelpers.size_chart(times, series)` now draws several lines with gaps for None.
  The filled area is drawn only when there is one line. The Dashboard lost the chart.
- "Run now" on the Storage page posts to `/run/protection?back=storage`. The return
  page comes from a fixed map in `routes/dashboard.py` (`_RUN_BACK`), never from the
  request as-is.
- Header below 560 px: the links scroll sideways, the user name is hidden.
- Screenshot note: headless Edge has a minimum layout width of about 492 px. A
  `--window-size=420` screenshot is the 492 px layout cut at 420. Check phone width in
  a real browser.

## Session before (settings tabs)

- Branch `settings-tabs`: `app/routes/settings.py` has `TABS` (slug, title, and the
  keys each tab saves). `GET /settings/{section}`, `POST /settings/{section}`;
  `/settings` redirects to `/settings/storage`. The fixed POST routes (`storage`,
  `token`, `token/remove`, `test-telegram`) are registered before the general one.
  The old `POST /settings` is removed (Jeremy's choice).
- New tests in `tests/test_settings_tabs.py`; the other settings tests post to the tab URLs.
- CSS: `.settings-layout`, `.settings-tabs`, `.settings-panel`. The global `.notice`
  rule has side margins (12px 16px); the Settings panel overrides it. This was the
  cause of the "message box too wide" todo.
- Deleted the merged local branches `progress-handoff` and `review-fixes`.

## Earlier session

- Planned the five UI PRs. Jeremy accepted all recommendations: theme first; the
  Dashboard keeps the live panel and "Run now"; third repository tab is "Watched";
  protection settings stay on Settings; one Save button for each settings tab;
  Octicons but no GitHub logo; release as 0.3.0.
- PR #17: `app/static/style.css` (Primer colours and components), `app/icons.py`
  (14 Octicons, inline SVG), `app/static/octicons-LICENSE.txt`, icons in the
  repository tabs, state labels and file list.
- PR #18: `app/routes/job_runs.py` (new; `/jobs`, `/jobs/{id}`, `/jobs/{id}/live`),
  `db.query_jobs()`, `templates/jobs.html`, `_job_list.html`. The Dashboard lost
  "Recent jobs" and got an "All job runs" button.
- PR #19: tabs in `app/routes/repos.py` (`TABS`), `db.repo_tab_counts()`,
  `templates/repos.html`. A fourth tab, **All**, was added outside the plan: the
  Dashboard links count own and watched repositories together. Jeremy merged it.

## Open questions

Q1: Settings tabs: which sections go on which tab? Recommendation: 6 tabs —
  **Storage and token** (the two folder fields and the token, already their own forms),
  **Discovery** (with "Parallel pulls" from the Sync section, which has only that one
  field), **Metadata** (with release files), **Schedules**, **Protection copy**,
  **Notifications** (Telegram).  
A1: 6 tab, recommendations

Q2: Storage page: show the size of the protection copy too? It needs a measurement  
  of the protection folder (slow over SMB) or the byte count from the rsync output.  
  Recommendation: not in PR 5; show used and free disk space of that folder only.  
A2: dont show for now.

Q3: Header order after PR 5? Recommendation: Dashboard, Repositories, Jobs, Storage,  
  Settings.  
A3: Dashboard, Repos, Jobs, Storage, Settings

## Next steps

1. **Done, merged as #21: branch `settings-tabs` (UI 4/5).** The notes below were the plan.
   - Routes: `GET /settings/{section}` and `POST /settings/{section}`, sections from a
     fixed list (Q1); `/settings` redirects to the first tab. Keep the existing POST
     routes `/settings/storage`, `/settings/token`, `/settings/token/remove` and
     `/settings/test-telegram`; change their redirects to the right tab.
   - The trap: `_save()` in `app/routes/settings.py` writes `"0"` for every key in
     `_BOOL_KEYS` that is not in the form. With one form for each tab, a save must
     write only the keys of that tab. Make a map section → keys and save only those.
   - A bad cron string: show the Schedules tab again with the errors (status 400),
     as now. This also covers the open todo "the message box can be too wide".
   - Template: vertical tab list on the left (CSS grid), a horizontal bar below 760px.
     Use `.state-tabs` styles where they fit; CSS variables only.
   - Tests: each tab saves only its keys (a checkbox on another tab stays "1"),
     unknown section is 404, cron error stays on Schedules, CSRF on each form.
2. **Done, merged as #22: branch `storage-page` (UI 5/5).** The notes below were the plan; see
   "This session" for what changed (release file sizes already existed; one chart, three lines).
   - New `app/routes/storage.py` (add it to the private tuple in `app/main.py`) and
     `templates/storage.html`; header item "Storage" (Q3).
   - Folders: mirror and protection paths from `runtime.py`, the marker state from
     `storage_guard.mirror_problem()` / `protection_problem()`, used and free space
     from `shutil.disk_usage`.
   - Protection copy: last run and recent runs (`db.query_jobs(job_type="protection")`),
     next run (`scheduler.next_runs()`), "Run now" (`/run/protection`), link to the
     Protection copy settings tab.
   - Sizes: code = sum of `repos.size_bytes`. Metadata and release files are not
     measured today. Add columns with a migration: per repository in `0032_*.sql`
     (metadata range 0030-0039); history columns on `sync_history` in `0041_*.sql`
     (sync engine range 0040-0049). The metadata job and the release files job
     record the size of `<name>.meta/` and its `assets/` when they run. Never walk
     the NAS on a page load.
   - Charts: server-side SVG like `viewhelpers.size_chart()`; no JavaScript library.
     Move the "Mirror size over time" chart from the Dashboard to this page.
3. **In review: release PR `release-0.3.0`.** After the merge: tag `v0.3.0` on `main`. `__version__` in `app/__init__.py` to 0.3.0; move
   "Unreleased" in `CHANGELOG.md` to "0.3.0 (date)"; after the merge, tag `v0.3.0`.
4. **Each PR:** update `CHANGELOG.md` and `docs/PROGRESS.md` (and README where it
   describes the page); `ruff check .` and `pytest` green; screenshots in both themes;
   a PR description with problem, change, checked, not checked and numbered manual
   checks. No session links or AI attribution.

**Screenshot method** (the script was in the session scratchpad and is gone): with
`TestClient`, sign in (`tests/conftest.py: login`), add repositories that point at
`tests/browse_fixture.build_site()`, get each page, replace the `/static/style.css`
link with a `file:///` path to `app/static/style.css`, remove all `<script>` tags,
set `<html data-theme="dark|light">`, save the HTML, then run
`msedge --headless --disable-gpu --hide-scrollbars --screenshot=<png> --window-size=1280,1100 <file URL>`.
Make temp folders inside the session scratchpad, not in `%TEMP%`.

## Resume prompt

> In the repo-ripper repository, read `docs/HANDOFF.md`, `CLAUDE.md` and
> `docs/PROGRESS.md`. Confirm that `main` has the `release-0.3.0` PR merged, tag
> `v0.3.0` on `main` if it is not there, then list the open todos in
> `docs/PROGRESS.md` with a recommendation for the next one, and wait for my go.
