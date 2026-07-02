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
