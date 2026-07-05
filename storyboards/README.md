# Release demo videos (storyboards)

Short, version-controlled demo clips of **user-facing UI features**, recorded at release time and
attached to the GitHub release. Each `*.yml` is a [`shot-scraper video`](https://shot-scraper.datasette.io/en/stable/video.html)
storyboard — a declarative routine (scenes of `click` / `fill` / `type` / `scroll` / `wait_for`,
plus a visible cursor) that Playwright records to `.webm` + `.mp4`. A storyboard re-runs each release,
so the demo can't silently drift from the real UI (same reason the e2e smoke is valuable).

## Record

```bash
# prerequisites (once): local stack up + shot-scraper installed
make dev            # backend :8010
make status-page    # status SPA :5173  (a separate shell/tmux window)
uv tool install shot-scraper     # dev-only tool, never in the app image

make demo           # records every storyboards/*.yml -> /tmp/watch-demos/*.{webm,mp4}
```

Attach to a release: `gh release upload vX.Y.Z /tmp/watch-demos/<name>.mp4`.

## macOS browser install (the gotcha)

shot-scraper pulls its **own** Playwright (currently 1.61 → Chrome-for-Testing build 1228), separate
from `e2e/`'s. `shot-scraper install` hits the same macOS extract-freeze as the e2e browser
(see [`e2e/README.md`](../e2e/README.md)). Seed the browser out-of-band instead — get the exact URLs
from `playwright install chromium ffmpeg --dry-run` (run from shot-scraper's venv), then
`curl` + `ditto -x -k` each zip into `~/Library/Caches/ms-playwright/<name>/`. **ffmpeg is required**
(video encoding), plus `chromium-<rev>` and `chromium_headless_shell-<rev>`.

## Storyboard notes

- **Selectors are Playwright selectors** and **strict** — a selector matching >1 element errors. Prefer
  a unique one (`button[type=submit]`, `input[type=submit]`, `button:has-text("copy link")`).
- The DRF login submit is `input[type=submit]` (not a `<button>`).
- Cross-origin note: the status SPA (`:5173`) POSTs to the backend (`:8010`); works locally because
  `STATUS_PAGE_CORS_ORIGIN=*`.

## ⚠️ Don't film live secrets

A demo of a security feature can leak the very thing it protects. The paging-settings clip shows a
salted ntfy topic — only ever record it against a **throwaway local** `NTFY_TOPIC_SECRET`, and rotate
it after recording if the clip goes public. Never film prod topics / real session ids / tokens.
