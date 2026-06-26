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
```bash
cp .env.example .env
docker compose up            # backend, postgres, valkey, appconfig-agent

# create an incident through the idempotent intake path
docker compose exec backend python manage.py consume_intake \
    --source sumo --title "Disk full" --event-id alert-123 --payload '{"host":"web-1"}'
```

## Tests (hermetic — no Docker/network, §6)
```bash
cd backend
pip install -r requirements.txt
pytest          # idempotency, both flag branches, tier authz, lifecycle transitions
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
