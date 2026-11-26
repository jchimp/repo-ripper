# repo-ripper

Mirrors every GitHub repository your token can see to a NAS folder as bare
`--mirror` clones (full history, all refs), copies that share to a second share
for protection, and gives you a small authenticated UI with schedules, per-repo
status, and optional Telegram notifications.

Point a local git UI (Forgejo, klaus, cgit, lazygit) at `MIRROR_ROOT` when you
want visual diffs and history — repo-ripper handles the capture, not the viewer.

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

## Upgrade from a version before the setup wizard

Older versions mounted the NAS at `/data/mirrors` and `/data/protection` in
`docker-compose.yml`. The file now has one NAS mount, `/mnt/nas`. If you take
the new file and change nothing else, the app still uses `/data/mirrors`, which
is then a folder on the local disk of the Docker host. The next sync makes new
clones there. No data is deleted, but the mirrors are not on the NAS.

Do one of these:

- Keep your two old volume lines in `docker-compose.yml`. Nothing changes.
- Or use the new mount, then open **Settings**, **Storage**, and select the
  folders below `/mnt/nas` that hold your mirrors and your protection copy.
  The app finds the mirrors that are there and updates them.

## The jobs

- **Scan** — lists repos via the GitHub API and enrols new ones. Honours the
  fork/archived toggles.
- **Pull** — `git clone --mirror` on first sight, then `git remote update
  --prune` thereafter, for every *enabled* repo. Best-effort LFS and wiki.
- **Protection copy** — `rsync -a` from `MIRROR_ROOT` to `PROTECTION_ROOT`,
  optionally `--delete` for an exact replica.
- **Capture metadata**: downloads pull requests, issues, comments and releases
  from the GitHub API to JSON files next to each mirror. See [Metadata](#metadata).

Each has its own cron schedule (Settings) and a **Run now** button (Dashboard).

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
- Labels, milestones, and releases. For a release asset the job stores the
  name, size and URL. It does not download the asset file.
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

## Protection `--delete`

On makes the copy an exact replica and drops repos deleted upstream; it also
propagates a corrupted primary on the next run. Off makes the copy append-only
(deleted repos linger) but shields it from primary corruption. Pick per your
threat model — filesystem snapshots (ZFS/btrfs) on the protection pool give you
both.

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
- Bad cron strings are ignored on save; the previous schedule stays in effect.
- Ports, image, and mounts are all in `docker-compose.yml`.

## License

[MIT License](LICENSE)
