# E2E (Playwright)

Functional smoke that runs locally (`make dev`) and against staging (pipeline Smoke stage).

## Suite tiering (#30) — local ⊆ staging
Tag tests by their **minimum environment**:
- **untagged / `@local`** → run everywhere (local + staging). This is the default; new tests get
  local coverage unless you opt out.
- **`@staging`** → run **only** on staging — for AWS-managed behavior or long waits that can't (or
  shouldn't) run in `make dev`: real Step Functions SLA-timeout auto-escalation, inbound webhooks, etc.

Runners:
- **Local** (`make e2e`, pre-commit): `--grep-invert=@staging` (via `E2E_GREP`) — everything except
  staging-only.
- **Staging** (pipeline Smoke): override `E2E_GREP=""` (or `--grep="@local|@staging"`) — the full
  superset. So anything that runs locally also runs on staging.

## Running
```
make e2e                 # local: @local tests vs http://localhost:8010 (needs `make dev` up)
make e2e E2E_INSTALL=1    # also (re)install browsers via npx — opt-in (see macOS note below)
```
Browser install is **opt-in**: `E2E_INSTALL` defaults to `0`, so `make e2e` never runs
`npx playwright install` unless you ask. Seed browsers once with `ditto` (below) and every later
run — including the pre-commit hook's `make e2e` — just uses them. CI is unaffected: the pipeline
Smoke stage runs on the prebuilt `mcr.microsoft.com/playwright` image with browsers baked in.

## macOS: browser install hangs
On some macOS setups Playwright's `npx playwright install` **freezes at "extracting archive"** (the
download completes; the Node-based unzip stalls) — which is why install is opt-in. Seed the browser
natively with `ditto` instead; then plain `make e2e` (default `E2E_INSTALL=0`) uses it:

```bash
# Playwright 1.61.1 -> chromium build 1228 (Chrome for Testing 149.0.7827.55). Bump these together
# when @playwright/test moves: `npx playwright install --dry-run chromium chromium-headless-shell`
# prints the current build number + download URLs.
D=~/Library/Caches/ms-playwright
B=https://cdn.playwright.dev/builds/cft/149.0.7827.55/mac-arm64
curl -L -o /tmp/hs.zip "$B/chrome-headless-shell-mac-arm64.zip"
rm -rf "$D/chromium_headless_shell-1228" && mkdir -p "$D/chromium_headless_shell-1228"
ditto -x -k /tmp/hs.zip "$D/chromium_headless_shell-1228/"
# (repeat for chrome-mac-arm64.zip -> $D/chromium-1228 if the full browser is also missing)

cd ~/watch && make e2e   # E2E_INSTALL=0 is the default — no installer, uses the seeded browser
```
Because install is opt-in by default, a plain `git commit` runs the hook's `make e2e` against the
seeded browser and can't hang. You only need `E2E_INSTALL=1` to (re)provision on a box where the
installer works.
