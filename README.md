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

## The three jobs

- **Scan** — lists repos via the GitHub API and enrols new ones. Honours the
  fork/archived toggles.
- **Pull** — `git clone --mirror` on first sight, then `git remote update
  --prune` thereafter, for every *enabled* repo. Best-effort LFS and wiki.
- **Protection copy** — `rsync -a` from `MIRROR_ROOT` to `PROTECTION_ROOT`,
  optionally `--delete` for an exact replica.

Each has its own cron schedule (Settings) and a **Run now** button (Dashboard).

## Token

A fine-grained PAT with read-only **Contents** + **Metadata** is enough; add the
private repos you want it to reach. Classic PATs need the `repo` scope for
private repos. The token is passed to git per-command via `http.extraHeader`, so
it is never written into the mirrors' config on the NAS.

Enter the token in the setup wizard or in Settings. The app stores it encrypted
in its database, with a key made from `SECRET_KEY`, and never shows it again.
If `SECRET_KEY` changes, the stored token cannot be read: enter the token
again. A `GITHUB_TOKEN` value in `.env` is used only when no token is stored.
Each scan checks the token. The dashboard shows a notice when GitHub rejects
the token or when the token expires in less than 14 days.

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
