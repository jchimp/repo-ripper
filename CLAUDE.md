# CLAUDE.md — repo-ripper

Project instructions for Claude Code. Read `README.md` for what the app does from
the user's side, `docs/DECISIONS.md` for why it is built this way,
`docs/PROGRESS.md` for the current state, and `CHANGELOG.md` for what changed.

## What this is

repo-ripper backs up GitHub repositories to a NAS folder and lets you browse the
backup with no connection to GitHub.

- **Mirror:** bare `git clone --mirror` of each repository (all branches, tags,
  and GitHub's `refs/pull/*`), plus the wiki.
- **Metadata:** pull requests, issues, comments, reviews, labels, milestones and
  releases as JSON files next to each mirror.
- **Release files:** attached files and source archives of releases (optional,
  off by default for each repository).
- **Protection copy:** rsync of the mirror folder to a second folder.
- **Browser:** read-only pages for code, history, pull requests, issues, releases.

Personal project (jchimp), self-hosted with Docker Compose on a LAN. One admin
user. GitHub only. Docker only.

## Non-negotiables

Do not change these without a new entry in `docs/DECISIONS.md`.

- **Never delete a mirror or stored data silently.** "Gone upstream", "removed
  upstream" and "stop watching" keep the files on disk. Any delete needs a
  confirmation step in the UI.
- **Secrets never leave the server.** Tokens are never logged, never written to
  a job log, never sent back to the browser, never put in a URL. The GitHub
  token goes only to the GitHub API host.
- **Works offline in the browser.** No CDN, no web fonts, no remote images, no
  build step. HTMX is vendored.
- **Paths never come from a URL.** A repository is opened by its database row
  id. Names from GitHub are cleaned before they become folder or file names.
- **The app does not mount shares.** It uses folders that are mounted into the
  container, below `BROWSE_ROOTS`.
- **Every route is private** unless it is in `routes/health.py` or
  `routes/auth_routes.py`.
- **Tests never use the network.** GitHub is `tests/fake_github.py` through
  `httpx.MockTransport`; git tests use local repositories in `tmp_path`.

## Stack

- Python 3.12, **pip** with pinned `requirements.txt` (dev: `requirements-dev.txt`) and a
  full lock, `requirements.lock`, made with `uv pip compile` (see README, Development)
- **FastAPI** + **Jinja2** + **HTMX 2.0.4** (vendored), small vanilla JS
- **SQLite** through raw `sqlite3`, short-lived connections, WAL. No ORM.
- **APScheduler** (in-process, started in the FastAPI lifespan)
- **httpx** for the GitHub API; the `git`, `git-lfs` and `rsync` programs
- **cryptography** (Fernet) for the stored token; **bcrypt** for the password
- **markdown-it-py** and **Pygments** for the browser
- **Docker Compose**, one container, runs as uid 1000

## Layout

```
app/
  main.py            app, middleware, router registration (private tuple)
  config.py          env settings (pydantic-settings); placeholder check
  runtime.py         github_token(), mirror_root(), protection_root()
  storage_guard.py   marker file of the storage folders; checks before a job writes
  logsetup.py        process log to stderr (docker logs)
  web.py             render(), CSRF, static_url(), shared template filters
  auth.py            single admin, sign-in throttle
  db.py              all SQL: settings, repos, job runs, logs
  migrations/        NNNN_name.sql, applied once, in order
  jobs.py            JobSpec registry; scan, pull, protection, verify jobs
  scheduler.py       APScheduler wiring from the registry
  progress.py        in-memory live progress of running jobs
  github.py          discovery + token check (typed errors)
  mirror.py          clone / update / verify one mirror
  protection.py      rsync protection copy
  watch.py           watched public repositories and owners
  metadata/          client.py, capture.py, store.py, job.py  (PRs, issues, ...)
                     assets.py, assets_job.py                 (release files)
  gitread.py         read a bare mirror through the git program
  mdrender.py, highlight.py   safe Markdown, syntax colours
  fsbrowse.py, secrets_store.py, setup_state.py   setup wizard support
  viewhelpers.py     template helpers of the sync pages
  routes/            one APIRouter per page area
  templates/         Jinja; partials start with "_"
  static/            style.css, theme.js, app.js, setup.js, htmx.min.js
tests/               pytest; fake_github.py, browse_fixture.py
docs/                HOSTING.md, DECISIONS.md, PROGRESS.md; private/ is ignored
```

On the NAS, for a repository `owner/name`:

```
<mirror folder>/<owner>/<name>.git/        bare mirror
<mirror folder>/<owner>/<name>.wiki.git/   wiki mirror
<mirror folder>/<owner>/<name>.meta/       JSON metadata; assets/ = release files
```

## Conventions

- **Settings.** Infrastructure values come from env (`config.py`). Values the
  user changes live in the `settings` table (`db.get_setting`, `db.get_bool`).
  The token and the two storage folders are read ONLY through `runtime.py`.
- **Schema.** Add a new file in `app/migrations/`. Never edit a shipped file.
  Number ranges: 0001-0009 core, 0010-0019 setup, 0020-0029 watched, 0030-0039
  metadata and release files, 0040-0049 sync engine, 0050-0059 browser.
- **Storage check.** A job that writes to the mirror folder or the protection
  folder calls `storage_guard.mirror_problem()` (or `protection_problem()`) first
  and stops with `jobs.refuse(...)` when the marker file is missing. A job does
  not create the root folder.
- **Log.** `logging.getLogger("repo-ripper")`. Never log a token, a URL that
  holds one, or the text of an httpx exception (log the exception type).
- **Jobs.** A job is one `jobs.register(JobSpec(...))` call. The dashboard, the
  settings page and the scheduler are built from the registry. A job takes
  `jobs._LOCKS[key]`, records `db.start_job` / `db.finish_job`, and never
  leaves a run on "running".
- **Routes.** New page area = new module in `app/routes/` + its name in the
  private tuple in `main.py`. Route modules do not import each other; shared
  code goes in `web.py` or a helper module. Use `render()` for all HTML.
- **CSRF.** Every POST needs the token. HTMX sends it from `<body hx-headers>`.
  A plain form needs the hidden `csrf_token` field.
- **CSS.** Only the CSS variables (`--bg`, `--panel`, `--field`, `--line`,
  `--text`, `--muted`, `--accent`, `--ok`, `--warn`, `--fail`, `--run`, ...).
  No hard-coded colours. Both themes (dark default, light) must work. Text
  boxes and drop-downs have ONE definition near the top of `style.css`; layout
  rules set only widths. Link static files with `static_url('name')`.
- **Templates.** Jinja autoescape for everything. The only trusted HTML is the
  output of `mdrender` and `highlight` (returned as `Markup`).
- **git calls.** Argument lists, never a shell. `--end-of-options` before a
  revision, `--` before paths. Timeouts on every call.
- **Files on the NAS.** Write to a temporary name, then rename
  (`store.atomic_write_*`). SMB shares need this.
- **UI text and docs:** Simplified Technical English. Short sentences, active
  voice, no marketing words.
- **Python:** type hints on all signatures, Google-style docstrings on public
  functions, f-strings, `pathlib`, line length 100, comments say why.

## Env vars

See `.env.example`. The app refuses to start while `ADMIN_PASSWORD` or
`SECRET_KEY` is a placeholder, or `SECRET_KEY` is shorter than 32 characters.

```
ADMIN_USERNAME, ADMIN_PASSWORD, SECRET_KEY     auth (required)
GITHUB_TOKEN                                   optional fallback; the UI stores the token
BROWSE_ROOTS=/mnt,/data                        folders the path picker may show
MIRROR_ROOT, PROTECTION_ROOT, DB_PATH          fallbacks; the UI stores the folders
TZ                                             timezone of the cron schedules
```

The other values in `.env.example` only seed the settings table on first start.
`SECRET_KEY` also encrypts the stored token: a new key means "enter the token again".

## Testing

- `pytest` + `ruff`. Both must pass before a commit. CI runs them on Ubuntu and
  builds the Docker image.
- Tests run on Windows and Linux. Do not depend on Linux-only tools in tests.
- CSS and JavaScript have no tests. After a UI change, render the pages and
  look at them in both themes (see the PR of the form-control change for the method).
- Checks against the real GitHub API are done by hand, read-only, on a public
  repository, and are written in the PR description.

## Commands

```
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt -c requirements.lock   # Linux: .venv/bin/python
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m pytest
docker compose up -d --build                                   # on the Docker host
```

Local run without Docker (dev only; rsync and the uid note do not apply):

```
ADMIN_PASSWORD=<12+ chars> SECRET_KEY=<32+ chars> DB_PATH=./data/dev.db \
MIRROR_ROOT=./data/mirrors BROWSE_ROOTS=./data \
.venv/Scripts/python -m uvicorn app.main:app --port 8019
```

## Workflow in this repo

- Never commit to `main`. One branch and one PR per feature or fix, based on
  `main`. Do not stack PRs: a stacked PR that is merged before its base
  retargets lands in the wrong branch.
- The version is `__version__` in `app/__init__.py`. A release PR changes it and moves
  "Unreleased" in `CHANGELOG.md` to a dated section; the tag is `v<version>`.
- Update `CHANGELOG.md` and `docs/PROGRESS.md` in the same PR. A decision that
  changes a rule above goes in `docs/DECISIONS.md` in that PR too.
- No force-push. To repair a pushed branch, add a commit or a merge.
- PR descriptions explain the problem, the change, what was checked and what
  was not, with numbered manual checks.

## Where to put things

- New scheduled job → its own module, `jobs.register(...)`, import it in `main.py`.
- New GitHub data to capture → `app/metadata/capture.py` + a read function in `store.py`.
- New browser page → `app/routes/browse.py` + `templates/browse_*.html`.
- New setting → `SEED_DEFAULTS` in `db.py` (or `config.py` if it is an env
  value), the save code in `routes/settings.py`, the field in `settings.html`.
- New column → a migration file in the right number range.
