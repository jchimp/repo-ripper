# Changelog

All notable changes to repo-ripper. Newest first.

New changes go under "Unreleased". A release moves them to a section with the
version and the date. The version is in `app/__init__.py`; the git tag is `v` +
the version. Each line names the pull request (PR) that made the change.

## Unreleased

## 0.1.0 (2026-11-26)

The first numbered version: everything from PR #1 to PR #14.

### Added

- **Setup wizard** (PR #2). First run opens a four-step flow in the browser:
  storage folders, GitHub token, repository selection, first sync.
- **Folder picker** (PR #2). Browse the folders that are mounted into the
  container (below `BROWSE_ROOTS`), make a new folder, and see free space and a
  write test.
- **Token in the UI** (PR #2). The GitHub token is entered in the browser and
  stored encrypted. The app shows the account, token type, scopes and expiry,
  and warns before the token expires. `GITHUB_TOKEN` in `.env` stays a fallback.
- **Watched public repositories** (PR #3). Add a repository of another owner by
  name or URL, or watch all public repositories of a user or organisation.
  Works with no token.
- **Metadata capture** (PR #4). A job stores pull requests, issues, comments,
  reviews, review comments, labels, milestones and releases as JSON files next
  to each mirror. Incremental; stops cleanly on a rate limit and continues at
  the next run. One switch per repository.
- **Release files** (PR #10). An optional job downloads the files attached to
  releases and GitHub's source archives. Off by default for each repository,
  with a standing reminder. Settings for the number of newest releases
  (default 10) and the largest file (default 2 GB). Local copies are kept when
  a release or file is removed on GitHub.
- **Parallel pulls and live progress** (PR #5). The pull job mirrors 1 to 8
  repositories at the same time. Running jobs show progress on the dashboard,
  the job page and the repository rows.
- **Verify job** (PR #5). `git fsck` on each mirror, weekly by default.
- **Renamed and deleted repositories** (PR #5). A renamed repository keeps its
  history and its mirror folder is renamed. A repository that is gone on GitHub
  is marked "gone upstream"; its mirror stays on disk.
- **Repository pages** (PR #5). Search, filters, sort and bulk actions on the
  Repositories table; a page per repository with sync history, mirror path and
  restore command; "Sync now" for one repository.
- **Dashboard** (PR #5). Health line, "Needs attention" list, mirror size chart.
- **Read-only browser** (PR #6). Code tree, files with syntax colours, rendered
  README, commit history and diffs, branches and tags, code search, pull
  requests and issues with their conversation, "Files changed" computed from
  the local mirror, releases, wiki.
- **Hosting document** (PR #6). `docs/HOSTING.md`: use the mirrors with git,
  restore to GitHub, a second viewer (cgit), what Forgejo can and cannot do,
  read the metadata with no app.
- **Light theme** (PR #1) with a toggle; the dark theme stays the default.
- **Health endpoint** `GET /health` and a Docker healthcheck (PR #1).
- **Tests and CI** (PR #1). pytest, ruff and a Docker build on each pull request.

### Changed

- **`docker-compose.yml` has one NAS mount** (`/mnt/nas:/mnt/nas`) in place of
  two (PR #2). An existing install must select its folders again in Settings,
  Storage, or keep its old volume lines. See "Upgrade" in the README.
- **The app refuses to start with a placeholder `ADMIN_PASSWORD` or
  `SECRET_KEY`**, or a `SECRET_KEY` shorter than 32 characters (PR #1).
- **An empty schedule means "run by hand only"** (PR #1).
- **Scan job has independent parts** (PR #3, #5): your repositories, then each
  watched owner. One failed part does not stop the others. With no token, the
  watched owners are still scanned.
- **New setting "Turn on backup for newly found repositories"** (PR #2),
  default on.
- **A failed Git LFS fetch is status `warn`** with the error text (PR #5). It
  was hidden as "lfs skipped".
- **A clone runs in a `.partial` folder** and is renamed when complete (PR #5).
- **The release files reminder stays until all active repositories are on**,
  and shows the count. New bulk actions "Release files on" and "Release files
  off" in the Repositories table (PR #13).
- **Form controls have one style** (PR #9): same height, border and focus ring
  for text boxes, drop-downs and buttons, in both themes. The Repositories
  filter bar is a grid.
- **A GitHub answer "403 not accessible by personal access token" names the
  permission to set** (PR #8).
- **Token permissions for a fine-grained token** are now Contents, Metadata,
  Issues and Pull requests, all read-only (PR #4).

### Fixed

- **HTMX was not in the repository** (PR #1). The per-repository switch and
  "Send test message" did nothing.
- **A job that was running when the container stopped stayed "running"**
  (PR #5). It is now marked `interrupted` at the next start.
- **A browser could show new pages with an old cached stylesheet** (PR #9).
  Static file URLs now carry a hash of the file content.
- **"Run now" for the protection copy ran when the copy was skipped in the
  setup** (PR #5). It now records `skipped`.
- **With "Include forks" or "Include archived" off, a repository could be
  marked gone upstream by mistake** (PR #5).

### Security

- CSRF token on every POST; sign-out is a POST (PR #1).
- Sign-in throttle: 5 wrong attempts per address in 5 minutes (PR #1).
- Stored secrets (Telegram bot token, GitHub token) are never sent back to the
  browser (PR #1, #2).
- The folder picker refuses any path outside the allowed roots, after `..` and
  symlinks are resolved (PR #2).
- The browser treats repository content as hostile: no shell for git, checked
  revisions, raw HTML off in Markdown, files served as plain text or download
  (PR #6).
- The GitHub token is sent only to the GitHub API host; a download address on
  another host is refused (PR #4, #10).

### Maintenance

- The version number is in `app/__init__.py` and shows at the bottom of each
  page (PR #14).
- CI uses `actions/checkout@v7` and `actions/setup-python@v7`. The older
  versions ran on Node.js 20, which GitHub has deprecated (PR #14).

### Notes

- PR #7 only brought PRs #4, #5 and #6 into `main`. They had been merged into
  each other's branches by mistake. It has no code of its own.

## Baseline (2026-07-24)

The first version, before PR #1: scan the token's repositories, `git clone
--mirror` one at a time, rsync protection copy, cron schedules, Telegram
notifications, one admin sign-in, dark theme.
