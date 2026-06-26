# CLAUDE.md — working in the Watch repo

Incident intake & tiered-escalation platform. **The design docs are authoritative:**
[`watch-v1-spec.md`](watch-v1-spec.md) (the v1 vertical slice) and
[`watch-adrs.md`](watch-adrs.md) (decisions + tradeoffs). Code follows the ADRs, not
the other way round. If you change a decision, update the ADR/spec in the same change.

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
- **ADR-001 / 007 — escalation engine:** one Step Functions Standard execution per
  incident; **ASL orchestrates, Python decides.** Every transition is idempotent
  ("act if still applicable", never blind act) — `incidents/services.py` is the single
  decision implementation called by both the API and the Lambdas.
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
backend/        Django + DRF, system of record (incidents app)
escalation/     Step Functions ASL + Python decision Lambda stubs; test/MockConfigFile.json
infra/          Terragrunt multi-stack (network/data/app/escalation/pipeline/frontend)
observability/  OTel notes — exports to the EXISTING Watchtower, not a bundled LGTM
frontend/       React SPA stub (honest-degradation probe in src/health.js)
local/flags/    AppConfig Agent local-dev files (named application:environment:profile)
```

## Running & testing locally
Use **`make dev`** — infra (Postgres/Valkey/AppConfig) in Docker + the backend on the
**host venv** (Python 3.12). The fully containerized `make up` needs image-registry/PyPI
build egress and is for CI / normal networks; in restricted sandboxes the Docker build
can't reach PyPI, so `make dev` is the working loop.

- App: http://localhost:8010  (ports offset off the conduct-* stack: app 8010, pg 5433,
  valkey 6380). Seeded users `t1`/`t2`/`t3` (pw `watch`), `admin`/`admin`.
- **Tests:** `make test` = hermetic units (sqlite via `config.settings_test`, no Docker).
  `make integration` = real Postgres + AppConfig Agent + Step Functions Local
  (`config.settings_integration`); integration tests are marked and skip cleanly when a
  dependency container is down.
- **OTel → existing Watchtower:** the backend exports OTLP to the running Grafana
  **Alloy** (`localhost:4318`), viewable in `watchtower-grafana` (:3000) → Tempo, service
  `watch-backend`. Never stand up a second LGTM.

## Conventions
- **Long-running processes go in the `watch` tmux session** (windows `server`/`infra`/
  `shell`), not Claude background shells. Start with `tmux send-keys -t watch:<win> …`.
- Commit only when asked; branch off `main` if needed. End commit messages with the
  `Co-Authored-By: Claude …` trailer the harness provides.
- Don't bake secrets into images or inline `environment`; secrets come from SSM/Secrets
  Manager via the task-def `secrets` block (§4.3).
