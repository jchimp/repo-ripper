"""GitHub metadata capture: pull requests, issues, comments, reviews, releases.

The data is stored as plain JSON files next to each mirror
(`<mirror_root>/<owner>/<name>.meta/`), so it stays readable without the app
and the protection copy includes it with no extra work.

- `client.py`: a small GitHub REST client (pagination, rate limits, retries).
- `capture.py`: the incremental sync of one repository.
- `store.py`: the file layout, atomic writes, and the read API for the viewer.
- `job.py`: the scheduled job that runs the capture for every repository.
"""
