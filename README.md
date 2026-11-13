# Watch

Incident intake & tiered-escalation platform — a document-intake system where an
incident enters via webhook, becomes a tracked Document, and moves through a
**T1 → T2 → T3** escalation timeline under time-based SLAs, with provable, auditable
escalation.

> **Design docs are authoritative.** [`watch-v1-spec.md`](watch-v1-spec.md) is the
> v1 vertical slice; [`watch-adrs.md`](watch-adrs.md) holds the decisions and their
> tradeoffs. This scaffold implements that slice — code follows the ADRs, not the
> other way round.

## Repository layout
```
backend/        Django + DRF — system of record (incidents app)
escalation/     Step Functions ASL + Python decision Lambdas (the escalation engine)
frontend/       React SPA on S3 + CloudFront (honest-degradation probe)
infra/          Terragrunt multi-stack IaC (network/data/app/escalation/pipeline/frontend)
observability/  OTel Collector config (→ Watchtower / LGTM)
local/          docker-compose support (AppConfig flags, etc.)
docker-compose.yml
```

## What's real vs. stubbed in this scaffold
| Area | State |
|---|---|
| Domain model, lifecycle, transitions (ADR-007) | **Implemented** + tests |
| Intake idempotency (ADR-009) | **Implemented** + tests |
| Auth/authz: session auth, tier-or-above (ADR-008) | **Implemented** + tests |
| Feature-flag seam (ADR-003) | **Implemented** + tests |
| API (DRF viewset + intake webhook + health) | **Implemented** |
| Step Functions ASL + Lambdas | ASL complete; Lambdas are thin stubs over the shared decision functions |
| Frontend / Terragrunt / OTel | Structured stubs + READMEs (lineage noted) |

## Quickstart (local, §5)
Host ports are offset to coexist with other local stacks; the app is on **8010**.
OTel exports to the **existing local Watchtower (LGTM)** stack via Grafana Alloy — it
does not run its own LGTM (see `observability/README.md`).

```bash
make dev       # infra (postgres:5433, valkey:6380, appconfig) in Docker + backend on
               # host (:8010); runs migrate + seed_demo + runserver, OTel -> Watchtower
make smoke     # (in another shell) push an incident through the intake webhook
```

`make dev` runs the app on the host against containerized infra — the primary local
loop, and the one that needs no image-registry/PyPI egress. `make up` runs the
fully containerized stack (backend image included) and is meant for CI / normal-network
environments where the image can build.

### Use it by hand
- **Browsable API:** http://localhost:8010/api/incidents/ — log in (top-right) as
  `t1` / `t2` / `t3` (password `watch`), or `admin` / `admin` for the Django admin.
- **Act on an incident:** open an incident, then `POST` to its `ack` / `escalate` /
  `resolve` action. Authz is tier-or-above (ADR-008); `expected_tier` guards against
  acting on a stale view (ADR-007).
- **See telemetry:** Watchtower Grafana http://localhost:3000 → Explore → Tempo →
  service `watch-backend`.

## Assessment workspace

Open http://localhost:8010/ui/assessments/ after starting `make dev`. Responders can
create a signal, record completed checks and results, save a conclusion, continue
monitoring, close with no issue, or promote to an incident. Promotion preserves the
assessment history and starts the incident clock. No-issue assessments do not create
incidents. An explicit, audited policy reason can require immediate promotion.

`make migrate-dev` applies new migrations to an already-running development stack.
`make seed-assessments` adds four fictional, repeatable examples without sending
notifications or contacting external systems. Log in as `t2a` to manage the examples.

Machine intake: `POST /api/assessments/intake`, authenticated with the existing
`X-Watch-Webhook-Secret` header, accepts `title`, `source`, `signal`, optional
`source_event_id`, `customer`, `service`, `supplier`, `signal_at`, and object `payload`.
Delivery dedupe is source-scoped and persists after closure. Authenticated reads are
under `/api/assessments/`, including `/<id>/history/`.

`make seed-operations` adds fictional supplier/customer assessment scenarios and a
resolved incident with specialist handoff, curated RCA and owned corrective action.
It also saves a real Watchtower Prometheus availability check (observability health,
not a booking SLI). Start `make operations` in another terminal to process queued
checks and enforce communication, specialist and local escalation deadlines. The
worker runs without a browser, sweeps deadlines independently of diagnostic jobs,
and exposes its last heartbeat under **Tools & configuration → Automation**.

The Add to history composer sits above the case tabs, with a sticky-header shortcut
for long evidence pages. Notes accept chosen, pasted or dropped photos, with previews
and removal before posting. Text, clickable dashboard links and photos are saved as
one source entry; a failed photo rejects the entire entry without orphaned files.
Photos also appear in the evidence view and remain source-linked in RCA exports.

The active workspace provides one history and composer for notes, executed checks,
operator-recorded evidence, uploads/image paste, hypotheses, recovery actions,
communications and specialist engagement. Separate severity/impact classification,
ownership and incident tier controls retain role checks. Source events and immutable
communication revisions remain inspectable. The RCA tab curates original events,
records findings, and assigns corrective actions with owners, due dates and completion
verification. Reports separate assessment receipt cohorts from incident promotion
cohorts, use first acknowledgement/resolution transitions, and exclude fictional
fixtures unless explicitly included.

**Automation setup:** configure saved read-only diagnostic checks, explicit
supplier → service → customer dependencies, and an active matching policy at
`/ui/automation/` (Tier 2 or above). Policies choose checks, thresholds, sample coverage,
severity, review/SLA windows, recipients, automatic promotion/no-issue closure and
optional T2 routing. The most specific active policy wins. Missing, stale, ambiguous,
nonfinite or insufficient results remain inconclusive. Local fixture checks require
both development mode and an explicit fixture payload. Real Sumo checks require the
existing Sumo endpoint/access credentials; each returns one numeric aggregate named
`value`, with separate baseline/sample-count queries as needed. Queries support
`${customer}`, `${service}`, `${supplier}` placeholders. Prometheus defaults to the
local Watchtower URL. Business SLIs must be configured against real customer traffic.

`POST /api/signals/intake` uses the same webhook authentication and payload as
assessment intake, plus dependency fan-out for supplier notices. Each customer/service
scope is independently deduplicated and assessed. Email/webhook collectors can adapt
incoming signals to this contract; no mailbox or supplier account is connected by
this implementation. Existing incident-only integrations retain their contract.

Communication drafts have append-only revisions and separate review/SLA deadlines.
Edit retains the original deadline; send/suppress use expected revision checks, and
suppression requires a reason. Automatic delivery requires explicit authorization on
the current revision. New checks/evidence revoke stale draft authorization. Worker
restarts preserve queued work and deadlines; abandoned running jobs become visible
failures after a one-hour lease and require review/retry.

**Delivery is local:** messages produce a transactional outbox receipt and downloadable
`.eml` at `/ui/outbox/`; no external email is sent. Assessment-driven incidents and SME
engagement use local in-app pages. Real external mail/paging and inbound mailbox
credentials require separate configuration and authorization. Runtime UI assets are
pinned locally, with licenses included; no CDN is needed to use this workspace.

## Tests
```bash
make test          # hermetic units — no Docker/network (idempotency, flag branches,
                   # tier authz, lifecycle transitions, ASL structure)
make integration   # spins the stack incl. Step Functions Local, then runs the
                   # integration suite: real Postgres, AppConfig Agent, SFN routing
```

## Decision map
| ADR | Where it lives |
|---|---|
| 001 Step Functions escalation | `escalation/` |
| 002 / 009 Durable + idempotent intake | `backend/incidents/intake.py`, `views.py` |
| 003 AppConfig flags behind a seam | `backend/incidents/flags.py` |
| 004 GitHub validates / AWS adjudicates | `infra/` (pipeline stack), CodeBuild |
| 005 Single-region + honest degradation | `frontend/src/health.js`, `backend/incidents/health.py` |
| 006 Terragrunt over CDK | `infra/` |
| 007 Lifecycle: status×tier, ack-keeps-clock, one token/tier | `backend/incidents/models.py`, `services.py`, `escalation.py` |
| 008 Session auth + tier-or-above authz | `backend/incidents/permissions.py`, `views.py` |

## License

Released under the [MIT No Attribution](LICENSE) license (MIT-0) — permissive, OSI-approved, no
attribution required.

## Preparing an upstream contribution

Use the [source/history/artifact review procedure](docs/upstream-contribution.md)
before publishing changes derived from operational examples or design material.
The local content guard and `make publication-test` support an explicit source-only
review; neither replaces human review or publication authorization.
