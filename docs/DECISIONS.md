# Decisions

The decisions that shape repo-ripper, and the reason for each. Read this before
you change a rule in `CLAUDE.md`.

How to use this file:

- One entry per decision. Newest at the end. Do not renumber.
- An entry is short: the decision, the reason, and what it costs.
- To change a decision, add a new entry that says which one it replaces. Do not
  rewrite the old entry; mark it "Replaced by D-NN".
- A feature idea that was looked at and not built goes in "Not doing (for now)".

## Scope

**D-01. GitHub only.** (2026-11-25)
Other hosts (GitLab, Gitea, plain git URLs) are not supported. The GitHub API
code is in its own modules (`github.py`, `metadata/client.py`), so a second host
can be added later. Cost: none now.

**D-02. Docker only.** (2026-11-25)
The supported way to run the app is the Docker image on a Linux host. A native
Windows run is for development only. Reason: the app needs `git`, `git-lfs`
and `rsync`, and writes as uid 1000.

**D-03. One admin user.** (baseline)
No accounts, no roles. The UI can show private code, so it must stay on the LAN
or behind a VPN or an authenticating proxy (see `docs/HOSTING.md`).

## Storage

**D-04. The app does not mount shares.** (2026-11-25)
The folder picker shows only folders that are mounted into the container. The
user mounts the NAS on the Docker host and gives one parent folder to the
container. The alternative (the app mounts SMB/NFS itself) needs a privileged
container and stored NAS credentials. Cost: one manual mount step on the host.

**D-05. Metadata is plain JSON files next to each mirror.** (2026-11-25)
`<owner>/<name>.meta/` holds the objects as the GitHub API returns them. Reason:
the data stays readable with no app, and the protection copy includes it with no
extra work. Cost: lists need an `index.json`, and there is no SQL search over
issue text.

**D-06. The database is local, not on the NAS.** (baseline)
SQLite on SMB or NFS can become corrupt. `./data` stays on the Docker host. The
database holds settings, state and logs; the backup itself is the files on the NAS.

**D-07. Nothing is deleted without a confirmation.** (2026-11-25)
A repository that is gone on GitHub keeps its mirror ("gone upstream"). "Stop
watching" removes the list entry and keeps the files. A release or release file
that is removed on GitHub keeps its local copy ("removed upstream"). Reason:
this is a backup; the moment something disappears upstream is the moment the
copy matters. Cost: disk use only grows; the user deletes by hand.

**D-08. Changing the mirror folder does not move data.** (2026-11-25)
The page says so. A move is a long, risky operation on a NAS and is left to the user.

## Secrets

**D-09. The GitHub token is entered in the UI and stored encrypted.** (2026-11-25)
Fernet, with a key derived from `SECRET_KEY` (HKDF). `GITHUB_TOKEN` in `.env`
is a fallback. A copy of the database alone does not reveal the token. Cost:
someone with the database AND `SECRET_KEY` can read it; a new `SECRET_KEY` means
"enter the token again".

**D-10. A token is stored only after GitHub accepts it.** (2026-11-25)
Cost: the setup needs access to api.github.com at that step.

**D-11. The app refuses to start with placeholder secrets.** (2026-11-25)
`ADMIN_PASSWORD` and `SECRET_KEY` from `.env.example` are refused, and a
`SECRET_KEY` shorter than 32 characters. Cost: a container that stops at start
until `.env` is edited; the log says which value.

## Sync

**D-12. Live progress uses polling, not server-sent events.** (2026-11-25)
Pages ask the server every 2 seconds, only while a job runs. Reason: it works
behind every reverse proxy and survives a page reload. Cost: up to 2 seconds of
delay.

**D-13. Watched repositories start with metadata off.** (2026-11-25)
A public project can have a very large number of issues. The user turns it on
for each repository.

**D-14. A scan never changes the user's choices.** (2026-11-25)
The switches of an existing repository stay as they are. One exception: a
watched repository that appears in the token's own list becomes "mine".

**D-15. An empty GitHub listing marks nothing as gone.** (2026-11-25)
An empty answer is more probably a token problem than the loss of every
repository.

## Browser

**D-16. The browser is built in and read-only.** (2026-11-25)
Forgejo was looked at. It can show mirrored code, but it cannot keep GitHub pull
requests and issues up to date: its import is one-time. So the app shows its own
captured metadata. Forgejo and cgit are documented as options for code only.

**D-17. Remote images are not loaded.** (2026-11-25)
An image in a README or comment shows as a link. Reason: the app is used
offline, and loading the image tells another server that the page was opened.

**D-18. The revision is in the query string.** (2026-11-25)
`/repos/3/blob/src/a.py?rev=feature/x`. A branch name can contain `/`, so a
revision in the path needs a guess about where it ends.

## Release files

**D-19. GitHub's source archives are downloaded and stored.** (2026-11-26)
Both `.zip` and `.tar.gz`. The alternative was to make them on request from the
mirror with `git archive`. Reason: keep the same files that GitHub serves. Cost:
the same content is on disk a second time, in two formats.

**D-20. Release files are off by default, with a standing reminder.** (2026-11-26)
One switch per repository. While no repository has it on, the dashboard shows a
notice that cannot be dismissed. (When the notice goes away: replaced by D-23.) Reason: the files can be very large, but the
owner wants to be reminded that they are not in the backup.

**D-21. Limits for release files.** (2026-11-26)
Only the newest N releases get downloads (setting, default 10, 0 = all). A file
larger than the limit is not stored (setting, default 2048 MB).

**D-23. The release files reminder stays until all active repositories are on.** (2026-11-26)
Replaces the rule in D-20 that the notice goes away at the first repository.
Active = backup on and not gone upstream. The notice shows the count ("for 3 of
40 repositories"). The Repositories table has bulk actions "Release files on"
and "Release files off" so that many repositories can be changed at one time.
Cost: a repository with no releases, or with the metadata switch off on
purpose, keeps the notice on screen until its switch is on.

## Safety of the copies (code review of 2026-11-26)

**D-24. A storage folder is known by a marker file.** (2026-11-27)
The app writes `.repo-ripper-root` into the mirror folder and the protection
folder when the user selects them. A job that writes stops when the file is
missing, and no job creates the root folder. Reason: when the NAS share is not
mounted, the path is an empty folder on the Docker host, and the jobs wrote
there with status "ok". Cost: a folder that is moved or restored by hand must
be selected again in Settings. Known limit: an install from before the marker
gets the file at its first start if the folder holds mirrors; if that start is
done with the share not mounted and clones are on the host disk, the wrong
folder gets the file.

**D-25. The protection copy removes nothing by default.** (2026-11-27)
`rsync --delete` is off for a new install. With it on, the job refuses to run
when fewer than half of the repositories with a good sync are on disk. Reason:
an empty or replaced primary share made the copy remove the mirrors that
cannot be made again, which is against D-07. `--max-delete` was looked at and
not used: a normal git repack removes thousands of files. Cost: the protection
folder only grows.

**D-26. A mirror keeps commits that lose their name on GitHub.** (2026-11-27)
The update still uses `--prune` (without it a deleted branch `a` blocks a new
branch `a/b`). Each mirror has `gc.pruneExpire=never`, so git never removes
unreachable objects, and each update writes deleted and force-pushed refs to
`repo-ripper-lost-refs.log` in the mirror. Reflogs are on, but git deletes the
reflog of a deleted branch and a clone writes none, so the log file is the full
record. Reason: D-07 was true for a repository, not inside one. Cost: a mirror
never gets smaller. `refs/pull/<n>/merge` changes are kept but not logged.

## Look

**D-27. The pages look like GitHub.** (2026-11-27)
The colours are GitHub's Primer palette ("dark default" and "light default"),
and buttons, tabs, counters and state labels have GitHub's shapes. The user
reads the backup of a GitHub repository, so the same look makes the pages
familiar. About 15 Octicons (GitHub's MIT-licensed icon set) are copied into
`app/icons.py` as inline SVG; the licence is `app/static/octicons-LICENSE.txt`.
No GitHub logo or Octocat: those are trademarks. No web fonts: GitHub uses the
system font stack too. Cost: the app looks like GitHub, so the page header
says "repo-ripper" to make clear that this is the local copy.

## Process

**D-22. One PR per feature, based on `main`. No stacked PRs.** (2026-11-26)
The first six PRs were stacked. Three of them were merged before GitHub changed
their base to `main`, and landed in each other's branches. PR #7 repaired it.

## Not doing (for now)

- **Check the token's permissions at save time** (probe the issue and pull
  request lists). Started and stopped on 2026-11-26; the 403 message now names
  the permission.
- **Download release files for all repositories by default.** See D-20.
- **Move mirrors when the mirror folder changes.** See D-08.
- **Delete a mirror from the UI.** See D-07.
- **Clone over HTTP from the app.** Use the NAS path or SSH (`docs/HOSTING.md`).
- **Cancel a running job.**
- **Blame view and side-by-side diff in the browser.**
- **Capture discussions, Actions runs, and review thread "resolved" state**
  (the last one needs the GraphQL API).
