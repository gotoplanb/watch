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
make e2e E2E_INSTALL=0    # skip the browser install step (see macOS note below)
```

## macOS: browser install hangs
On some macOS setups Playwright's browser install **freezes at "extracting archive"** (the download
completes; the Node-based unzip stalls). Work around it by extracting with the native `ditto`, then
running with `E2E_INSTALL=0` so `make e2e` doesn't re-trigger the hanging installer:

```bash
D=~/Library/Caches/ms-playwright
B=https://playwright.azureedge.net/builds/chromium/1148
curl -L -o /tmp/hs.zip "$B/chromium-headless-shell-mac-arm64.zip"
rm -rf "$D/chromium_headless_shell-1148" && mkdir -p "$D/chromium_headless_shell-1148"
ditto -x -k /tmp/hs.zip "$D/chromium_headless_shell-1148/"
# (repeat for chromium-mac-arm64.zip -> $D/chromium-1148 if the full browser is also missing)

cd ~/watch && make e2e E2E_INSTALL=0
```
Tip: `export E2E_INSTALL=0` in your shell so the pre-commit hook's `make e2e` skips the installer too.
