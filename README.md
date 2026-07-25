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

```bash
cp .env.example .env
# edit .env: ADMIN_PASSWORD, SECRET_KEY, GITHUB_TOKEN, TZ
# edit docker-compose.yml: point the two NAS mounts at your shares
docker compose up -d --build
```

Open `http://<host>:8019`, sign in, then **Run now → Scan GitHub** to enrol your
repos, followed by **Pull mirrors**. After that the schedules take over.

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
it is never written into the mirrors' config on the NAS. Note the PAT expiry —
an expired token is the usual cause of a silently dead backup.

## NAS mounts and permissions

The container runs as uid **1000**. The mounted shares must be writable by that
uid, or clones fail with permission errors. On the NAS, either export with that
uid/gid or `chown -R 1000:1000` the target directories. Keep the app database
(`./data`) on local storage, not the NAS — SQLite over SMB/NFS is asking for
corruption.

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

## Notes

- New GitHub repos are picked up on the next scan; no config needed.
- A manual run and a scheduled run of the same job can't overlap — the second is
  recorded as `skipped`.
- Bad cron strings are ignored on save; the previous schedule stays in effect.
- Ports, image, and mounts are all in `docker-compose.yml`.

## License

[MIT License](LICENSE)
