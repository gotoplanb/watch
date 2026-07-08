# CLAUDE.md — working in the Watch repo

Incident intake & tiered-escalation platform. **The design docs are authoritative:**
[`watch-v1-spec.md`](watch-v1-spec.md) (the v1 vertical slice) and
[`watch-adrs.md`](watch-adrs.md) (decisions + tradeoffs). Code follows the ADRs, not
the other way round. If you change a decision, update the ADR/spec in the same change.

> **RULE 0 — use the project's own interface first. ALWAYS look for a `make` target or an
> existing script in `scripts/` / `local/` before ad-hoc probing.** For anything operational
> (tunnels, dev loop, deploys, DB, AWS lifecycle, status checks) the answer is almost always a
> target — run `make help`, skim the `Makefile`, and read the relevant `*.sh`/Terraform outputs
> BEFORE improvising `curl`/CLI one-offs or asking the user. Ad-hoc probing is what wrecks things:
> it misreads state (e.g. hitting a generic port that belongs to another project), reinvents what
> already exists, and skips the guardrails the scripts encode. Concrete case: for "tunnels up" the
> answer was `make tunnel-status` / `make tunnel-up` (URLs from `local/tunnel/` Terraform) — not
> curling `localhost:4040`, which returned a neighbouring project's data and sent me down a wrong,
> apologetic rabbit hole. Read the interface, then act.

## Operating mode — ACTIVE BUILD MODE (read this after every compaction)
We are **rapidly building** this project and **create/destroy the whole AWS estate almost daily.**
This is the current normal, not an exception:
- **Run `make live` / `make teardown` / manual staging deploys directly** (bootstrap admin creds).
  Long-running ones go in the `watch` tmux session. Don't hedge or treat these as scary one-offs —
  they're routine. Cost ~$10/day during build is fine for velocity (destroy at end of session).
- The **"Claude is read-only on AWS, pipeline mutates via OIDC"** principle is the **eventual
  production discipline / target**, NOT a restriction on current build-mode work. Read-only applies
  when we're *verifying* a live prod estate; while building, I apply directly.
- Cloud IaC lives in **`~/platform`** (Terragrunt/OpenTofu); local dev is `make dev` here.
- **Known blocker:** new Org member accounts hit a **CloudFront account-verification** hold
  (`AccessDenied: account must be verified before you can add new CloudFront resources`) — the estate
  otherwise stands up; only `staging/frontend`/`prod/frontend` fail. Needs AWS Support / time.

## How decisions get made and recorded
1. Surface gaps/questions as **GitHub issues** under the `spec-gap` label (`gh issue create`).
2. Discuss and resolve them with the user — one coupled cluster at a time. The user
   reliably picks the option marked **(Recommended)**, so lead with a clear recommendation.
3. Fold the resolution into an **ADR** in `watch-adrs.md` (one ADR per coupled decision;
   amend an existing ADR for a missing assumption; use "Refines ADR-NNN" when elaborating).
   Keep `watch-v1-spec.md` consistent.
4. Commit to `main`, close the issue with a comment pointing at the ADR.

Audit of *design* decisions lives in this GitHub-issue→ADR trail (the runtime audit
boundary is AWS — see ADR-004).

## Architecture invariants (don't violate without a new ADR)
- **ADR-001 / 007 / 010 — escalation engine:** one Step Functions Standard execution
  per incident; **ASL orchestrates, Python decides.** Every transition is idempotent
  ("act if still applicable", never blind act) — `incidents/services.py` is the single
  decision implementation called by both the API and the Lambdas. In the **real engine**
  the **commit Lambda is the sole writer** of Transitions (actor flows via
  `$.decision.actor`; timeouts use `system:auto-escalation`); the API only
  `SendTaskSuccess`. Locally the Lambdas run via `run_lambda_shim` (host) +
  `sfn_register` against Step Functions Local; `ESCALATION_LOCAL_MODE=1` short-circuits
  to direct `services` calls for hermetic tests and the simple `make dev` loop.
- **ADR-007 — lifecycle:** state is orthogonal `status` (OPEN/RESOLVED) × `current_tier`
  (T1/T2/T3) + `acknowledged_at`. One tier = one `waitForTaskToken`; the token is
  consumed **exactly once per tier**. **ACK does not consume the token and does not stop
  the SLA clock.** RESOLVE/ESCALATE go through `SendTaskSuccess(outcome=…)`; RESOLVE
  routes to `Succeed` (no zombie timer). One outstanding token ⇒ single
  `current_task_token` field; the API never trusts a client token (looks up current,
  rejects on tier mismatch → 409).
  - ASL gotcha: `waitForTaskToken` tasks **must set `ResultPath`** (`$.decision`) or the
    task output replaces state and wipes `incidentId` for later tiers.
- **ADR-008 — auth:** Django **session auth** (cookies in Valkey), tiers as Django
  Groups. Authz for ack/escalate/resolve = the incident's `current_tier` role **or
  higher** (`incidents/permissions.py`). Webhook intake is machine-to-machine (shared
  secret), separate from human sessions.
- **ADR-009 — intake idempotency:** dedupe key = source event id else
  `sha256(normalize(payload))`; enforced by the **partial unique index**
  `UNIQUE(dedupe_key) WHERE status=OPEN` + `ON CONFLICT DO NOTHING`. Retry while OPEN =
  no-op; re-fire after RESOLVED = new incident.
- **ADR-003 — flags:** all evaluation behind `flags.is_enabled(name, default)`; never
  read a provider directly. Test both branches of every flag.

## Repo layout
```
backend/        Django + DRF, system of record (incidents app). DRF API under /api/;
                server-rendered HTMX/Alpine/Tailwind working surface under /ui/
                (ui_views.py + templates/, ADR-011) — reuses services + permissions.
escalation/     Step Functions ASL + Python decision Lambda stubs; test/MockConfigFile.json
infra/          pointer only — cloud IaC lives in gotoplanb/platform (Terragrunt for
                AWS/GitHub/Cloudflare; rollout plan + issues there). Local dev stays here.
observability/  OTel notes — exports to the EXISTING Watchtower, not a bundled LGTM
frontend/       React SPA — narrowed to a read-only status page (ADR-011)
local/flags/    AppConfig Agent local-dev files (named application:environment:profile)
```

## Running & testing locally
Use **`make dev`** — infra (Postgres/Valkey/AppConfig) in Docker + the backend on the
**host venv** (Python 3.12). The fully containerized `make up` needs image-registry/PyPI
build egress and is for CI / normal networks; in restricted sandboxes the Docker build
can't reach PyPI, so `make dev` is the working loop.

- App: http://localhost:8010  (ports offset off the conduct-* stack: app 8010, pg 5433,
  valkey 6380). Seeded users `t1a`/`t1b`/`t2a`/`t2b`/`t3a`/`t3b` (two per tier for
  on-call scheduling) + superuser `admin`. Passwords default to `watch` / `admin` but come from
  `SEED_USER_PASSWORD` / `SEED_ADMIN_PASSWORD` (.env) and re-apply on every `seed_demo` — set both
  to non-guessable values on any tunnel-exposed box.
- **Tests:** `make test` = hermetic units (sqlite via `config.settings_test`, no Docker).
  `make integration` = real Postgres + AppConfig Agent + Step Functions Local
  (`config.settings_integration`); integration tests are marked and skip cleanly when a
  dependency container is down.

## Quality gates (before committing)
Mirror the conduct project. **Both must be green before a commit:**
1. **≥90% unit-test coverage** — `make coverage` (gate enforced by `fail_under = 90` in
   `pyproject.toml`; writes `backend/coverage.xml`). Coverage `omit`/Sonar
   `coverage.exclusions` cover only bootstrap + integration/CLI glue (settings, wsgi,
   otel, migrations, lambda shim, management commands); everything else is unit-tested.
2. **Green SonarQube Quality Gate** — `make sonar-scan` (scanner → local Watchtower
   SonarQube at :9000, project `watch`). Reads `SONAR_TOKEN` from `.env`. Aim for 0
   bugs / 0 vulnerabilities / 0 code smells, not just a passing gate. For local AWS
   emulators use botocore `UNSIGNED` (no hardcoded creds — Sonar flags them).

Both gates are enforced by a **pre-commit hook** (`.githooks/pre-commit`, installed via
`make install-hooks` → `core.hooksPath`). It runs only when `backend/` is staged; the
coverage gate is hard, the Sonar gate runs when SonarQube is reachable. Bypass with
`SKIP_SONAR=1 git commit …` (coverage only) or `git commit --no-verify` (sparingly).
- **OTel → existing Watchtower:** the backend exports OTLP to the running Grafana
  **Alloy** (`localhost:4318`), viewable in `watchtower-grafana` (:3000) → Tempo, service
  `watch-backend`. Never stand up a second LGTM.

## Conventions
- **Long-running processes go in the `watch` tmux session** (windows `server`/`infra`/
  `shell`), not Claude background shells. Start with `tmux send-keys -t watch:<win> …`.
- **UI verification via the Playwright MCP.** The `/ui/` working surface (:8010) and the
  React status page (:5173) can be driven in a real browser through the `MCP_DOCKER`
  gateway (`mcp/playwright`). The container has **no host mount**, so
  `browser_take_screenshot` writes to `/home/node/<file>.png` *inside* it — retrieve with
  `docker cp $(docker ps --filter ancestor=mcp/playwright -q):/home/node/<file>.png <host>`
  then Read the PNG. `browser_evaluate` reading `getComputedStyle(...)` is a quick no-copy
  check. (Browser reaches the host via `host.docker.internal`, allow-listed in `make dev`.)
- Commit only when asked; branch off `main` if needed. End commit messages with the
  `Co-Authored-By: Claude …` trailer the harness provides.
- Don't bake secrets into images or inline `environment`; secrets come from SSM/Secrets
  Manager via the task-def `secrets` block (§4.3).
