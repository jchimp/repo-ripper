# repo-ripper

Mirrors every GitHub repository your token can see to a NAS folder as bare
`--mirror` clones (full history, all refs), copies that share to a second share
for protection, and gives you a small authenticated UI with schedules, per-repo
status, and optional Telegram notifications.

The UI also has a read-only browser for the mirrored code, the history and the
captured pull requests and issues. See [Browse](#browse).

## Stack

FastAPI · APScheduler · SQLite · Jinja2 · HTMX (vendored) · Docker.
Blocking git/rsync work runs in a thread pool; the app stays responsive during syncs.

## Quick start

You need a Linux host with Docker and Docker Compose, and a NAS share (SMB or
NFS) that the host can reach. Do all steps on the Docker host, in Bash.

1. Mount the NAS share on the Docker host at `/mnt/nas`. The app does not mount
   shares. The container runs as uid **1000**, so uid 1000 must be able to
   write to the share. Replace each `<placeholder>` with your value.

   SMB share:

   ```bash
   sudo apt install cifs-utils
   sudo mkdir -p /mnt/nas
   sudo mount -t cifs //<nas-address>/<share-name> /mnt/nas \
     -o username=<nas-user>,uid=1000,gid=1000,file_mode=0660,dir_mode=0770
   ```

   NFS share:

   ```bash
   sudo apt install nfs-common
   sudo mkdir -p /mnt/nas
   sudo mount -t nfs <nas-address>:/<export-path> /mnt/nas
   sudo chown 1000:1000 /mnt/nas
   ```

   If `chown` fails on NFS, set the owner on the NAS, or map all users of the
   export to uid 1000 in the NAS settings. To make the mount permanent, add it
   to `/etc/fstab`. For a second share (for the protection copy), mount it
   below the same folder, for example at `/mnt/nas/backup2`.

2. Make the configuration file:

   ```bash
   cp .env.example .env
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```

   Open `.env`. Set `ADMIN_PASSWORD` to a password of your choice. Set
   `SECRET_KEY` to the value that the second command printed. Set `TZ` to your
   time zone. The app does not start while the password or the key is a
   placeholder. You do not need a GitHub token in this file.

3. If your NAS mount point is not `/mnt/nas`, open `docker-compose.yml` and
   change the left side of the line `- /mnt/nas:/mnt/nas`.

4. Start the app:

   ```bash
   docker compose up -d --build
   ```

5. Open `http://<docker-host-address>:8019` in a browser and sign in with
   `ADMIN_USERNAME` and `ADMIN_PASSWORD`. The setup wizard opens:

   1. **Storage**: select "Browse", go to the folder below `/mnt/nas` that will
      hold the mirrors, and select "Use this folder". You can also type the
      path. The app shows the free space and does a write test. Select a
      protection folder, or skip the protection copy.
   2. **Token**: paste a GitHub personal access token and select "Check
      token". The app shows the account, the token type and the expiry date.
      Select "Save and continue". The app stores the token encrypted.
   3. **Repositories**: select the repositories to back up.
   4. **Start**: select "Start first sync". The dashboard opens.

After the first sync, the schedules in Settings run the jobs. To open the
wizard again, select "Run setup again" in Settings.

If the write test fails: the container user (uid 1000) cannot write to the
share. Change the owner of the folder to `1000:1000`, or mount the share with
`uid=1000` (SMB), then select the folder again. If you mount the share after
the container started, run `docker compose restart`.

## The jobs

- **Scan GitHub** lists the repositories through the GitHub API and adds the
  new ones. It obeys the fork and archived settings. It also finds renamed
  repositories and repositories that are gone upstream (see below).
- **Pull mirrors** makes a `git clone --mirror` the first time, then runs
  `git remote update --prune`, for each repository that has the backup on. It
  mirrors 4 repositories at the same time. You can set 1 to 8 in Settings,
  section "Sync". One repository that fails does not stop the others.
- **Protection copy** runs `rsync -a` from `MIRROR_ROOT` to `PROTECTION_ROOT`.
  With `--delete` on, the copy is an exact replica.
- **Verify mirrors** runs `git fsck` on each mirror on disk, one time each
  week. A mirror that fails gets the tag `verify failed`.
- **Capture metadata** downloads pull requests, issues, comments and releases
  from the GitHub API to JSON files next to each mirror. See [Metadata](#metadata).
- **Download release files** stores the files of the newest releases, for the
  repositories that have the switch on. See [Release files](#release-files).

Each job has a cron schedule (Settings) and a **Run now** button (Dashboard).
While a job runs, the dashboard and the job page show the progress, the
repositories in work, and the log. The pages update every 2 seconds.

### Repository status

| Status | Meaning |
| --- | --- |
| `ok` | The last sync was good. |
| `warn` | The mirror is updated, but a part failed. Example: the Git LFS fetch. The repository page shows the reason. |
| `error` | The last sync failed. The mirror on disk is not changed. |
| `pending` | No sync yet. |
| `running` | A sync is in work now. |

A repository can also have these tags:

- `gone upstream`: the repository is not in the GitHub listing. It was
  deleted, or the token can no longer see it. The mirror stays on disk and
  the pull job skips the repository. If the repository comes back, the next
  scan removes the tag. repo-ripper never deletes a mirror.
- `verify failed`: `git fsck` found a problem in the mirror. If GitHub still
  has the repository, move the mirror folder away and select **Sync now**.

The Repositories page has search, filters, sort and actions for the checked
rows (backup on, backup off, sync now). The address of a filtered list can be
kept as a bookmark. Select a repository name to see its facts, its sync
history, the last error in full, and the restore command.

## Watch public repositories

You can mirror public repositories that are not yours. These repositories have
the tag `watched` on the **Repositories** page.

**Add one repository.** On the **Repositories** page, type the name or the URL
in the field "Watch a public repository" and select **Add**. These forms are
accepted:

- `owner/name`
- `https://github.com/owner/name` (also with `.git`, or with more path such as
  `/tree/main`)
- `git@github.com:owner/name.git`

The app asks GitHub if the repository exists. If it does, the repository goes
into the list and the next pull job makes the mirror.

**Watch an owner.** Open "Watched owners", type the name of a user or an
organisation, and select **Add owner**. Each scan job then adds the public
repositories of that owner. Forks and archived repositories are added only if
you set the two options. If you remove an owner, the scan job stops looking for
new repositories of that owner. The repositories that are in the list stay
there.

**Stop watching.** Select "Stop watching" in the row of a watched repository
and confirm. The app removes the repository from the list. The app does not
delete the mirror: the files stay in `MIRROR_ROOT` and you can delete them by
hand. Your own repositories cannot be removed; turn them off with the backup
switch.

**No token.** Public repositories do not need a token. Without a token the
scan job skips your own repositories and still scans the watched owners.
GitHub then permits only 60 API requests per hour for each IP address. When
the limit is reached, the app shows the time at which the limit resets. Add a
token to raise the limit to 5000 requests per hour.

**Metadata.** Metadata capture (pull requests, issues, comments) is off for
each watched repository when it is added, because a public project can have a
very large number of issues. Turn it on for a repository if you want it.

## Token

A fine-grained PAT needs these read-only permissions: **Contents** and
**Metadata** for the git mirrors, plus **Issues** and **Pull requests** for the
metadata capture. Add the private repos you want it to reach. Classic PATs need the `repo` scope for
private repos. The token is passed to git per-command via `http.extraHeader`, so
it is never written into the mirrors' config on the NAS.

Enter the token in the setup wizard or in Settings. The app stores it encrypted
in its database, with a key made from `SECRET_KEY`, and never shows it again.
If `SECRET_KEY` changes, the stored token cannot be read: enter the token
again. A `GITHUB_TOKEN` value in `.env` is used only when no token is stored.
Each scan checks the token. The dashboard shows a notice when GitHub rejects
the token or when the token expires in less than 14 days.

## Metadata

A git mirror holds the code and the history. It does not hold pull request
conversations, issues or releases, because GitHub keeps these outside git. The
job **Capture metadata** downloads them from the GitHub API.

What the job captures for each repository:

- Pull requests: the pull request, its conversation comments, its review
  comments (comments on diff lines), its reviews, and its commit list.
- Issues and their comments.
- Labels, milestones, and releases. For a release file this job stores the
  name, size and URL. The files themselves are a separate job: see
  [Release files](#release-files).
- The GitHub repository object (description, topics, default branch).

Where the data is stored: in plain JSON files in a folder next to the mirror.
You can read the files without repo-ripper. The protection copy includes them.

```
<MIRROR_ROOT>/<owner>/<name>.git/     the git mirror
<MIRROR_ROOT>/<owner>/<name>.meta/
    README.txt                        what the folder is, format version
    repo.json  labels.json  milestones.json  releases.json
    issues/<number>.json              {"issue", "comments", "fetched_at"}
    pulls/<number>.json               {"pull", "comments", "review_comments",
                                       "reviews", "commits", "fetched_at"}
    index.json                        compact list of all issues and pull requests
    state.json                        sync position, counts, result of the last run
```

The objects are stored as GitHub returns them. Text bodies are raw Markdown.

How the job works:

- The first run downloads everything. Each later run downloads only the issues
  and pull requests that changed after the last run.
- The job saves its position after each item. If a run stops, the next run
  continues from that position.
- An issue or pull request that is deleted on GitHub stays on disk.
- The job runs each day at 04:00 by default. Change the schedule in Settings.
- To stop the capture for all repositories, clear **Capture metadata from
  GitHub** in Settings. To stop it for one repository, use the **Metadata**
  switch on the Repositories page.

Token permissions:

- Fine-grained token: **Issues** read, **Pull requests** read, **Metadata**
  read, **Contents** read.
- Classic token: the `repo` scope.
- With no token the job can read public repositories only.

Rate limits:

- GitHub permits 5,000 API requests in one hour with a token, and 60 requests
  in one hour with no token.
- A pull request costs 2 to 5 requests or more. An issue costs 0 to 1 request
  or more. Thus the first run on a large repository can use the full limit.
- When GitHub reports the limit, the job stops and does not send more
  requests. The job result shows how many repositories are done and the time
  at which the limit resets. The next run continues. A large repository can
  need more than one run.

The commits of pull requests are in the git mirror, not in the metadata.
`git clone --mirror` uses the refspec `+refs/*:refs/*`, so the mirror includes
GitHub's `refs/pull/<number>/head` refs. Thus the commits of each pull request
are stored locally, also when the source fork is deleted. To get them:

```bash
git --git-dir /mnt/nas/git-mirrors/<owner>/<repo>.git log refs/pull/<number>/head
```

## Release files

A release on GitHub can have files: the files that the author attached (build
zips, installers) and the "Source code" archives that GitHub makes (.zip and
.tar.gz). The attached files exist only on GitHub. The job **Download release
files** stores both kinds.

- **Off by default.** Release files can be very large. Turn on the "release
  files" switch of a repository in the Repositories table, or on its Releases
  page. The repository also needs the metadata switch, because the list of
  releases comes from the metadata.
- **Reminder.** The dashboard shows a notice until each repository with the
  backup on has the switch on ("not downloaded for 3 of 40 repositories"). The
  Releases page of a repository shows a notice while its switch is off. To
  change many repositories, check the rows in the Repositories table and select
  "Release files on".
- **Newest releases only.** Settings, section Metadata: "Newest releases for
  each repository" (default 10, 0 = all).
- **Size limit.** Settings, section Metadata: "Largest file (MB)" (default 2048).
  A larger file is not stored and shows as "too large".
- **Nothing is deleted.** If a release or a file is removed on GitHub, the local
  copy stays and shows as "removed upstream". A release that is no longer one
  of the newest keeps its files.
- **Checks.** Each file is written under a temporary name and renamed when it
  is complete. The size must agree with GitHub's list, and the SHA-256 checksum
  too when GitHub gives one. The job stops when the disk has less than 500 MB
  of free space after the next file.
- **Where.** `<mirror folder>/<owner>/<repo>.meta/assets/<release id>/files/`
  and `.../source/`. The file `assets/index.json` lists the state of each file.
  The protection copy includes these folders.
- **Download.** The Releases page of a repository has a link for each stored file.

## Browse

repo-ripper has a read-only browser for each repository. Select **Browse** in a
row of the Repositories page. It reads the mirror and the metadata folder on
disk, so it works with no connection to GitHub.

- **Code**: file tree for each branch, tag or commit, the rendered README,
  files with syntax colours, raw download, and a text search.
- **Commits**: history of a branch or a file, and each commit with its diff.
- **Pull requests** and **Issues**: lists with filters, and the full
  conversation. The changed files of a pull request are computed from the
  local mirror.
- **Releases** and, when it is mirrored, the **Wiki**.

The browser cannot change a repository, and it is not a git server. To clone
from a mirror, to restore a repository to GitHub, to add a second viewer
(cgit), or to learn what Forgejo can and cannot do with these mirrors, read
[docs/HOSTING.md](docs/HOSTING.md).

Text from a repository or from GitHub is not trusted: Markdown is rendered
with raw HTML off, remote images are not loaded, and the raw file route sends
HTML and SVG files as plain text.

## NAS mounts and permissions

The container runs as uid **1000**. The mounted shares must be writable by that
uid, or clones fail with permission errors. On the NAS, either export with that
uid/gid or `chown -R 1000:1000` the target directories. Keep the app database
(`./data`) on local storage, not the NAS — SQLite over SMB/NFS is asking for
corruption.

The folder browser in the UI shows only folders below the `BROWSE_ROOTS` value
(default `/mnt,/data`), and only folders that are mounted into the container.
If you change the mirror folder later (Settings, Storage), the app does not
move data: the mirrors that exist stay in the old folder, and the next sync
makes new clones in the new folder.

When you select a storage folder, the app writes the file `.repo-ripper-root`
into it. A job that writes stops with an error when this file is not there,
because the NAS share is then probably not mounted. Do not delete the file; if
you move a folder, select it again in Settings, Storage.

## Protection `--delete`

The setting "Delete extras on the copy" (Settings) adds `--delete` to the rsync
command. **The default is off** for a new install. An install that already has
the setting keeps its value.

- **Off:** the protection copy never removes a file. A file that is deleted
  from the mirror folder stays in the protection folder. The copy uses more
  disk space with time. If the mirror folder is lost, damaged or replaced by an
  empty disk, the protection copy keeps all that it has. This is the reason for
  the default: the protection copy must not lose data at the moment when the
  first copy fails.
- **On:** the protection copy is an exact replica. rsync removes from the
  protection folder each file that is not in the mirror folder. A damaged or
  incomplete mirror folder then also damages the copy at the next run.

The protection copy does not run, and the job shows an error, in these cases:

- The file `.repo-ripper-root` is not in the mirror folder or not in the
  protection folder (see "NAS mounts and permissions").
- "Delete" is on, and fewer than half of the repositories that had a good sync
  are in the mirror folder. Example: the database knows 40 repositories with a
  good sync and the mirror folder has 3. Examine the mirror folder before you
  run the copy again.

The job is skipped while the pull, metadata or release files job runs, because
rsync must not copy a repository while a job changes it.

Filesystem snapshots (ZFS, btrfs) on the protection share give you an exact
replica and protection against a damaged mirror folder at the same time.

## Restore

Mirrors are plain bare repos. To restore any project:

```bash
git clone /mnt/nas/git-mirrors/<owner>/<repo>.git <repo>
```

## Security

- The app does not start while `ADMIN_PASSWORD` or `SECRET_KEY` holds a
  placeholder, or while `SECRET_KEY` is shorter than 32 characters.
- Five wrong sign-in attempts from one address lock that address out for five
  minutes. Behind a reverse proxy, all clients share the proxy address.
- Every form and HTMX request carries a CSRF token.
- Stored secrets (the GitHub token, the Telegram bot token) are never sent back
  to the browser. The GitHub token is encrypted in the database.
- The folder browser resolves every path and refuses a path that is outside
  `BROWSE_ROOTS`, also through `..` or a symlink. It lists folders, not files.
- `GET /health` is public and returns `{"status": "ok"}`; Docker uses it as
  the container healthcheck.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # Windows: .venv\Scripts\pip
.venv/bin/ruff check .
.venv/bin/pytest
```

Layout: `app/routes/` has one module per page area; add a new router to the
private tuple in `app/main.py` and it needs a signed-in session. Schema
changes are new numbered files in `app/migrations/`, applied once at start.
The UI has a light/dark toggle in the top bar; the choice is kept per browser.

## Notes

- New GitHub repos are picked up on the next scan. Backup is on for them
  unless you clear "Turn on backup for newly found repositories" in Settings.
- A manual run and a scheduled run of the same job can't overlap — the second is
  recorded as `skipped`.
- **Sync now** on one repository, or on the checked rows, is refused while the
  full pull job runs. The pull job syncs these repositories anyway.
- A renamed or moved repository keeps its history: the scan knows it by its
  GitHub id, gives the entry the new name, and moves the mirror folder (and
  the `.wiki.git` and `.meta` folders). If a folder with the new name exists,
  nothing is moved, the job log says so, and the next pull writes to the new
  name.
- If GitHub returns an empty list while repositories are known, the scan
  marks nothing as gone upstream and says so in the job summary. The usual
  cause is a token problem.
- With "Include archived" or "Include forks" off, the scan cannot tell a
  repository that became archived (or a fork) from one that is gone, and can
  mark it `gone upstream`. Its mirror stays on disk.
- A failed Git LFS fetch gives the status `warn` with the reason. If the
  server has no `git-lfs`, turn "Fetch Git LFS objects" off.
- A network error during a clone or update is tried again one time.
- A clone runs in a `<name>.git.partial` folder and gets its final name only
  when it is complete. A clone that was stopped does not block the next run.
- A job that a restart stopped shows as `interrupted`. Run it again.
- The app keeps the newest 200 history rows for each repository.
- The Telegram message of a pull job with failures lists the failed
  repositories (10 names at most).
- Settings does not save a schedule that is not valid cron. The page shows the
  field with the error, and no value of that form is stored. If a stored
  schedule is not valid at start, the app uses the default schedule of that
  job, stores it, and writes a warning to the process log.
- Ports, image, and mounts are all in `docker-compose.yml`.

## Project documents

- [CHANGELOG.md](CHANGELOG.md): what changed.
- [docs/DECISIONS.md](docs/DECISIONS.md): the decisions behind the design, with reasons.
- [docs/PROGRESS.md](docs/PROGRESS.md): current state and open items.
- [docs/HOSTING.md](docs/HOSTING.md): browse and host the mirrors locally.
- [CLAUDE.md](CLAUDE.md): instructions for an AI assistant or a new developer.

## License

[MIT License](LICENSE)
