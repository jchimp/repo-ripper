# Browse and host your mirrors locally

This document tells you how to read the data that repo-ripper stores: the
code, the history, the pull requests and the issues. All of it works with no
connection to GitHub.

Statements about other tools that could not be confirmed from their official
documentation have a tag: [Likely] or [Uncertain]. Test those before you rely on them.

Short version: use the browser that is built into repo-ripper (Option A). It
shows the code and the captured pull requests together. A second viewer
(Option B) is possible, but it can show only the git data.

Placeholders used in this document:

| Placeholder | Meaning | Example |
|---|---|---|
| `<mirror-root>` | The mirror folder that you selected in the setup (Settings, Storage shows it), as a path on the Docker host | `/mnt/nas/git-mirrors` |
| `<host>` | The name or address of the Docker host on your LAN | `nas.lan` |
| `<owner>`, `<repo>` | The GitHub owner and repository name | `octocat`, `hello-world` |
| `<n>` | The number of a pull request or an issue | `42` |

## 1. What is on the NAS

```
<mirror-root>/
  <owner>/
    <repo>.git/         bare git mirror: all branches, all tags, and the
                        commits of each pull request (refs/pull/<n>/head)
    <repo>.wiki.git/    bare git mirror of the wiki (only if the wiki exists)
    <repo>.meta/        metadata from the GitHub API, as JSON files
      index.json        one compact entry for each issue and pull request
      pulls/<n>.json    the pull request, its comments, reviews, commit list
      issues/<n>.json   the issue and its comments
      releases.json  labels.json  milestones.json  repo.json  state.json
      assets/         release files, when the switch is on:
        index.json                 the state of each file
        <release id>/files/<name>  files that the author attached
        <release id>/source/<name> GitHub's source archives (.zip, .tar.gz)
```

- The `.git` folders are standard bare repositories. Each git tool can read them.
- The `.meta` folders are not git data. Only repo-ripper (or a JSON tool) reads them.
- The protection copy has the same layout.

## 2. Option A: the built-in browser (recommended)

The browser is part of repo-ripper. It needs no setup.

What it shows:

- **Code**: the file tree at each branch, tag or commit; the rendered README;
  files with line numbers and syntax colours; a Raw link for each file.
- **Commits**: the history of a branch or of one file; each commit with its
  message, its parents and its diff.
- **Branches and tags**, and a text search in the files of one revision.
- **Pull requests** and **Issues**: lists with state filters and a search box.
  A pull request page has the conversation (comments, reviews, review
  comments), the commit list, and the changed files. The changed files are
  computed from the local mirror.
- **Releases**: the notes, and a download link for each release file that is
  stored (see "Release files" in the README; the download is off by default).
- **Wiki**: the files of the wiki mirror.

What it cannot do:

- It is read-only. You cannot edit, comment, merge or push.
- It is not a git server. You cannot `git clone` from its address. Use the
  commands in section 3.
- It does not load remote images in a README or a comment. It shows a link.
- Release files are stored only for repositories that have the "release
  files" switch on. For the others it shows their names and sizes.
- A file that is stored with Git LFS shows as its small pointer text.
- The search finds exact text in one revision. It does not search in issues
  or pull request text. (The list pages search in title, author and labels.)
- The Raw link does not serve a file that is larger than 32 MB.
- It has no blame view and no side-by-side diff.

### Check that it works

Do these steps in a web browser on a computer on your LAN.

1. Open `http://<host>:8019` and sign in.
   Expected result: the Dashboard shows.
2. Select **Repositories**. Select the name of a repository that has the
   status `ok`, then select the tab **Code**.
   Expected result: the file list of the default branch shows, with the README below it.
3. Select **Commits**. Select the first commit.
   Expected result: the commit message and the diff show.
4. Select **Pull requests**.
   Expected result: a list shows. If the page says "The metadata is not captured
   yet", run the job **Capture metadata** on the Dashboard, wait until it is
   done, and load the page again.
5. Open a pull request and select **Files changed**.
   Expected result: the diff shows. If the page says that a commit is not in
   the mirror, run the job **Pull mirrors** and load the page again.

## 3. Use the mirrors directly with git

A mirror is a normal bare repository. These commands do not need repo-ripper
to run.

### Clone from the NAS path

Machine: the Docker host or any Linux computer that mounts the share. Shell: Bash.

```bash
git clone <mirror-root>/<owner>/<repo>.git
```

The result is a normal working copy. Its `origin` is the mirror folder.

If git stops with `detected dubious ownership`, the mirror belongs to a
different user. Tell git that you trust this one folder, then do the clone again:

```bash
git config --global --add safe.directory <mirror-root>/<owner>/<repo>.git
```

### Clone over SSH from a different computer

This needs an SSH login on the computer that holds the share, and git
installed there. `<user>` is your SSH user on that computer.

Machine: workstation (Windows). Shell: PowerShell.

```powershell
git clone ssh://<user>@<host><mirror-root>/<owner>/<repo>.git
```

Example with the values from the table: `git clone ssh://me@nas.lan/mnt/nas/git-mirrors/octocat/hello-world.git`

### Add a mirror as a second remote of a working copy

Use this when you have a working copy from GitHub and want to compare it with
the mirror, or to continue to work when GitHub is not available.

Machine: workstation (Windows). Shell: PowerShell. Run it in the working copy.

```powershell
git remote add mirror ssh://<user>@<host><mirror-root>/<owner>/<repo>.git
git fetch mirror
```

After that, `git log mirror/main` shows the history of the mirror.

### Get the commits of one pull request

The mirror keeps the head of each pull request as `refs/pull/<n>/head`. This
also works when the source fork is deleted on GitHub.

Machine: workstation (Windows). Shell: PowerShell. Run it in a working copy
that has the remote `mirror` from the step before.

```powershell
git fetch mirror refs/pull/<n>/head:pr-<n>
git switch pr-<n>
```

To look only, with no working copy. Machine: Docker host. Shell: Bash.

```bash
git --git-dir <mirror-root>/<owner>/<repo>.git log --oneline refs/pull/<n>/head
```

### Restore a repository to GitHub or to a different host

First make a new, empty repository on the target host. `<target-url>` is its
address, for example `git@github.com:<owner>/<repo>.git`.

**Warning: this command overwrites the target.** It makes each branch and tag
on the target equal to the mirror, and it deletes branches and tags on the
target that are not in the mirror. Use it only on a new, empty repository, or
on a repository that you want to replace.

Machine: Docker host. Shell: Bash.

```bash
git --git-dir <mirror-root>/<owner>/<repo>.git push --prune <target-url> \
  "+refs/heads/*:refs/heads/*" "+refs/tags/*:refs/tags/*"
```

Notes:

- This command sends the branches and the tags. It does not send the
  `refs/pull/*` refs. `git push --mirror <target-url>` sends all refs.
  [Likely] GitHub refuses the `refs/pull/*` refs with the message
  `deny updating a hidden ref`, and the command then ends with an error, also
  when the branches and tags arrived. Thus the command above is the better one
  for GitHub.
- git restores the code and the history only. It does **not** restore pull
  requests, issues, comments, releases or settings. These stay in the `.meta`
  folder as a record that you can read (section 2 and section 5).
- [Likely] If the repository uses Git LFS, also run
  `git --git-dir <mirror-root>/<owner>/<repo>.git lfs push --all <target-url>`.

### Check a restore

1. Open the target repository in a web browser.
   Expected result: the default branch shows the files.
2. Machine: Docker host. Shell: Bash. Run:
   `git ls-remote --heads --tags <target-url> | wc -l` and
   `git --git-dir <mirror-root>/<owner>/<repo>.git for-each-ref refs/heads refs/tags | wc -l`
   Expected result: the second number is not larger than the first. (The
   first can be larger: `ls-remote` prints one more line for each annotated tag.)

## 4. Option B: a second viewer on the same folder

You do not need this. Use it only if you want a second, independent view of
the git data. A second viewer knows nothing about the `.meta` folders, so it
shows no pull request conversations and no issues.

### cgit: a simple read-only viewer

cgit is a small web viewer for git repositories. The image
`joseluisq/alpine-cgit` (Docker Hub) packs cgit with nginx. It scans the folder
`/srv/git` in the container and all folders below it, so the layout
`<owner>/<repo>.git` works with no more configuration.

**[Likely] cgit has no sign-in.** Each person who can reach the port can read all
mirrored code, private repositories included. The service below listens only
on the Docker host itself (`127.0.0.1`). Read section 6 before you change that.

This service was written from the documentation of the image. It was not run
as part of the repo-ripper tests. Do the check below after you start it.

Machine: Docker host. Add this service to the `services:` section of the
repo-ripper `docker-compose.yml` (replace `<mirror-root>`):

```yaml
  cgit:
    image: joseluisq/alpine-cgit:2
    container_name: repo-ripper-cgit
    restart: unless-stopped
    ports:
      - "127.0.0.1:8787:80"
    environment:
      CGIT_TITLE: "Git mirrors"
      CGIT_DESC: "Read-only view of the repo-ripper mirrors"
      # 1 = use the owner folder as the section heading in the list
      CGIT_SECTION_FROM_STARTPATH: "1"
      # number of repositories on one page of the list
      CGIT_MAX_REPO_COUNT: "500"
    volumes:
      # :ro = the container cannot change the mirrors
      - <mirror-root>:/srv/git:ro
```

Then, machine: Docker host, shell: Bash, in the folder of `docker-compose.yml`:

```bash
docker compose up -d cgit
```

Check:

1. Machine: Docker host. Shell: Bash. Run `curl -sI http://127.0.0.1:8787/ | head -n 1`.
   Expected result: a line with `200`.
2. Open `http://127.0.0.1:8787/` in a browser on the Docker host. (From your
   workstation, first open an SSH tunnel. Machine: workstation, shell:
   PowerShell: `ssh -L 8787:127.0.0.1:8787 <user>@<host>`. Then open
   `http://127.0.0.1:8787/` on the workstation.)
   Expected result: a list of repositories, in groups by owner. A wiki mirror
   shows as its own entry (`<repo>.wiki.git`). The `.meta` folders do not show.
3. Select a repository, then **log**.
   Expected result: the commit history shows.

What to know:

- The process in the container runs as the user `nginx`. If the list is
  empty, make sure that this user can read the mirror files (read permission
  for "others" on the folders and files).
- A new repository can take up to 15 minutes to show in the list. cgit keeps
  the result of its folder scan for that time (`cache-scanrc-ttl`, default 15).
- [Uncertain] After a pull, single pages can show old data for some minutes,
  because the image turns on the cgit page cache.
- To remove the viewer: `docker compose rm -sf cgit`. This does not touch the mirrors.

### Forgejo or Gitea: what works and what does not

Forgejo (and Gitea, from which Forgejo started) is a full git host with its
own database, users, issues and pull requests. It is not a viewer for a folder
of repositories. That difference causes each limit below.

The official Forgejo image is `codeberg.org/forgejo/forgejo:<major>` (major
version 17 in November 2026). The variant `<major>-rootless` runs with no root
user in the container.

**Do not point Forgejo at the mirror root.** Forgejo can "adopt" bare
repositories that are in its own repository folder (admin page
`/admin/repos/unadopted`). For the repo-ripper mirrors this is the wrong tool:

- Adoption writes into the repository (it runs `git update-server-info`, sets
  `HEAD`, and can create the file `git-daemon-export-ok`). Thus it cannot work
  on a read-only mount, and with a writable mount two programs change the
  same repositories.
- Adoption has conditions: the folder must be `<repository root>/<owner>/<repo>.git`,
  a Forgejo user or organization with the name `<owner>` must exist first, and
  the folder names must be lower-case. GitHub names with upper-case letters do
  not pass.
- Forgejo keeps the list of branches in its database. When a different
  program (the repo-ripper pull job) updates the repository on disk, that list
  can be out of date until an administrator runs the dashboard tasks "Sync
  missed branches from Git data to database" and "Sync tags from Git data to
  database". Such updates also make no entries in the activity feed.
  [Uncertain] The official documentation does not say whether file and commit
  pages show the new data immediately.
- [Likely] Forgejo does not turn `refs/pull/<n>/head` refs into pull requests.

**What works: a pull mirror in Forgejo, with its own storage.** Forgejo then
holds a second copy and updates it by itself.

1. Give Forgejo its own data volume. Do not share it with repo-ripper.
2. In Forgejo, select **New Migration** in the Create menu, give the address
   of the repository, and select **This repository will be a mirror**.
3. Forgejo then fetches on a schedule. The default interval is 8 hours
   (`[mirror] DEFAULT_INTERVAL`), and the shortest interval is 10 minutes
   (`[mirror] MIN_INTERVAL`).

The source of the migration can be GitHub (then Forgejo does not use
repo-ripper at all), or a path on the Forgejo server. For a path, mount
`<mirror-root>` read-only into the Forgejo container, set
`IMPORT_LOCAL_PATHS = true` in the `[security]` section of `app.ini` (the
default is `false`), and do the migration as an administrator.

Limits of a pull mirror:

- It holds the git data (and the wiki). It does not hold issues or pull
  requests: when the mirror option is on, Forgejo turns off the import of
  issues, pull requests, labels, milestones and releases.
- It drops the `refs/pull/*` refs of GitHub when it syncs.
- You can make a pull mirror only for a repository that is not in Forgejo
  yet. You cannot change an existing repository into a mirror.
- It uses the disk space a second time.

**Pull requests and issues in Forgejo.** A migration from GitHub with an
access token, with the mirror option off, can import the wiki, issues, pull
requests, labels, milestones and releases. [Uncertain] The documentation does
not describe a later update of these items; an open feature request in the
Gitea project asks for that function, so treat the import as one-time. The
result is a normal repository, not a mirror: it does not follow GitHub after
the import. Thus Forgejo gives you one of these, not both: a code mirror that
stays up to date, or a one-time copy with pull requests and issues.

Forgejo also has the commands `forgejo dump-repo` and `forgejo restore-repo`.
`restore-repo` reads a folder of YAML files in Forgejo's own layout. The
repo-ripper `.meta` JSON files are not in that layout, and repo-ripper has no
converter. [Uncertain] The documentation gives only the option list for this
command, so do not plan on it without a test.

Gitea differences: the registry is `docker.gitea.com/gitea`, and the admin
page is `/-/admin/repos/unadopted`. The adoption and mirror settings have the
same names.

Conclusion: for a record of GitHub pull requests that stays up to date and
that you can read offline, the built-in browser is the tool. Use Forgejo when
you want a place to continue development (new pushes, new pull requests)
after you leave GitHub. For that, do a restore (section 3) into a new Forgejo
repository.

## 5. Read the metadata with no app

The `.meta` files are plain JSON, stored as the GitHub REST API returns them.
Text bodies are raw Markdown.

Machine: Docker host. Shell: Bash. These commands need `jq`.

List the open pull requests:

```bash
jq -r '.items[] | select(.kind == "pull" and .state == "open") | "#\(.number)\t\(.user)\t\(.title)"' \
  <mirror-root>/<owner>/<repo>.meta/index.json
```

Print the conversation comments of one pull request:

```bash
jq -r '.comments[] | "--- \(.user.login) \(.created_at)\n\(.body)\n"' \
  <mirror-root>/<owner>/<repo>.meta/pulls/<n>.json
```

Print the review comments (comments on diff lines) of one pull request:

```bash
jq -r '.review_comments[] | "--- \(.user.login) on \(.path)\n\(.body)\n"' \
  <mirror-root>/<owner>/<repo>.meta/pulls/<n>.json
```

Machine: workstation (Windows). Shell: PowerShell. `<share>` is the folder of
the mirror root as Windows sees it, for example `\\nas\git-mirrors`.

List the open pull requests:

```powershell
(Get-Content -Raw -Encoding utf8 '<share>\<owner>\<repo>.meta\index.json' | ConvertFrom-Json).items |
  Where-Object { $_.kind -eq 'pull' -and $_.state -eq 'open' } |
  Format-Table number, user, title
```

Print the conversation comments of one pull request:

```powershell
(Get-Content -Raw -Encoding utf8 '<share>\<owner>\<repo>.meta\pulls\<n>.json' | ConvertFrom-Json).comments |
  ForEach-Object { "--- $($_.user.login) $($_.created_at)`n$($_.body)`n" }
```

The index entry of each item has these fields: `number`, `kind` (`pull` or
`issue`), `title`, `state`, `merged`, `draft`, `user`, `labels`, `created_at`,
`updated_at`, `closed_at`, `merged_at`, and comment counts.

## 6. Exposure and safety

- The browser shows private code and private conversations to each person
  who can sign in. Keep the sign-in password long and unique.
- Keep repo-ripper on the LAN. Do not forward a port from your router to it.
- If you need access from outside, use one of these:
  - a VPN into your network (for example WireGuard), or
  - a reverse proxy or tunnel that makes each visitor sign in before the
    request reaches repo-ripper.
- repo-ripper serves plain HTTP. On a network that you do not fully trust,
  put a reverse proxy with TLS in front of it.
- A second viewer such as cgit has no sign-in of its own. Do not publish its
  port on the LAN unless each person on the LAN may read all mirrored code.
  Put it behind a proxy that has a sign-in, or keep it on `127.0.0.1`.
- The raw file route sends each file as plain text, as one of four image
  types (PNG, JPEG, GIF, WebP), or as a download. An HTML or SVG file from a
  repository does not run in your browser.
- The mirror share holds all your code. Give write access to that share only
  to the repo-ripper container, and read access only to the users who need it.
