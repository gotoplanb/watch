# Watch — v1 Technical Spec
*Incident intake & tiered escalation platform*

> This document describes the **v1 vertical slice** — the minimal end-to-end spine that exercises every architectural decision once. Design decisions and their rationale live in the companion file, `watch-adrs.md`.

---

## 1. Purpose & framing

A document-intake and tiered-escalation system for production-support and incident workflows. An incident enters via webhook (e.g. Sumo Logic), becomes a tracked Document, is assigned, and moves through a T1 → T2 → T3 escalation timeline under time-based SLAs — with provable, auditable escalation.

Two intended uses from one codebase:
- **Day-job production support** — runs *alongside* ServiceNow (not a replacement), as the working surface for investigations.
- **Reference architecture** — a clonable golden-path for Django + AWS services in the idioms that matter (Terragrunt, ECS, Step Functions, OTel, expand/contract).

The escalation timeline is treated as **core domain value**, not a supporting mechanism — "did the handoff happen on time, and can we prove it" is the product's reason to exist. That single judgment drives the biggest design choice (see ADR-001).

---

## 2. Architecture overview

```
Sumo (or any source)
   │  webhook
   ▼
API Gateway ──► SQS ──► Intake consumer ──► Incident (Postgres)
                                               │  on create
                                               ▼
                                     Step Functions execution
                                     (one per incident — the escalation timer)
                                               │  Lambda tasks (Python) decide & write back
                                               ▼
                                     Django/DRF on ECS Fargate ◄──► RDS Postgres (Multi-AZ)
                                               ▲                     Valkey (sessions)
                                               │ REST
React SPA (S3 + CloudFront) ───────────────────┘
```

- **Django is system-of-record.** Incident state lives in Postgres.
- **Step Functions is the escalation engine.** It owns *timing and orchestration*; Python Lambdas own *decisions*. ASL orchestrates, Python decides.
- **Intake is decoupled and durable.** An incident is captured at the queue, independent of whether the app tier is healthy.

---

## 3. Domain model (v1)

**Incident**
- `id`, `source`, `payload` (raw intake), `title`, `created_at`
- **Lifecycle is two orthogonal fields** (ADR-007): `status` ∈ `{OPEN, RESOLVED}` and `current_tier` ∈ `{T1, T2, T3}`, plus `acknowledged_at` (nullable — set when a human acks the *current* tier, cleared on escalation). `RESOLVED` is reachable from any tier. (Replaces the old linear `NEW→…→RESOLVED` enum: `NEW = (OPEN, T1, ack=null)`, `TRIAGED_T1 = (OPEN, T1, ack=set)`.)
- `assignee` (user/role)
- `sla_deadline_at` (current tier's deadline), `escalation_execution_arn`
- `current_task_token` — the outstanding `waitForTaskToken` for the current tier; exactly one per incident, written atomically with `current_tier`/`sla_deadline_at` by the tier-entering Lambda, nulled on consume (ADR-007).
- relationships: `transitions[]` (audit), `documents[]` (attachments — v1: metadata only)

**Transition (append-only audit record)**
- `incident_id`, `from_state`, `to_state`, `actor` (user id *or* `system:auto-escalation`), `reason`, `at`
- Manual and automatic escalations write the **same record shape** — the audit trail is agnostic to *how* a transition happened.

**Role/tier** — T1, T2, T3 as roles; users belong to one or more. v1 keeps assignment simple (role-based, manual claim or auto-route on escalation).

---

## 4. Layers

### 4.1 Intake
- `Sumo webhook → API Gateway → SQS → consumer`.
- **Ack on enqueue, not on processing.** The source never waits on Django/RDS. (ADR-002)
- Consumer is idempotent on a source-provided dedupe key (or hash of payload) so retried deliveries don't create duplicate incidents.
- Consumer creates the Incident, then **starts one Step Functions execution** for it.

### 4.2 Escalation engine (Step Functions)
- **One Standard-workflow execution per incident**, started at creation. Standard workflows bill per state transition and run up to a year — ideal for slow, human-paced escalation timelines.
- Each tier modeled as a **`waitForTaskToken` task with `timeout` = that tier's SLA**. The token is consumed **exactly once per tier**, by a tier-ending decision (ADR-007):
  - **ESCALATE / RESOLVE** → app calls `SendTaskSuccess` with an `outcome` field → ASL `Choice` routes (ESCALATE → next tier; RESOLVE → `Succeed`, ending the execution cleanly — no zombie timer).
  - **ACK** ("I've got this, still working") → **Postgres event + audit record only; does *not* consume the token.** An acked-but-unresolved incident still auto-escalates at its SLA deadline — acking is not progress.
  - **Timeout fires** → caught as auto-escalation → advance to next tier (`actor=system:auto-escalation`).
  - This unifies **manual and automatic escalation on one path**.
  - The API never trusts a client-supplied token: it looks up `current_task_token`, rejects if `current_tier` moved from the client's expected tier (optimistic concurrency), and treats `SendTaskSuccess` on an already-consumed token (`TaskDoesNotExist`) as an idempotent no-op.
- Python Lambda tasks make every decision ("is it still unacked? who's next? what's the new SLA?") and **write the transition back to Postgres** with an audit record.
- **Idempotent transitions:** every escalation is "escalate *if still applicable*," never "escalate." Step Functions retries must be safe.
- **A missed escalation is a failed execution → CloudWatch alarm.** Absence of escalation is itself detectable and alarmable — escalation reliability is provable, not best-effort. (ADR-001)

### 4.3 API
- Django + DRF on **ECS Fargate** behind an ALB.
- **Stateless tasks** — sessions externalized to **Valkey (ElastiCache)**; durable data in **RDS Postgres Multi-AZ**.
- Secrets via SSM **SecureString** (static) and **Secrets Manager** (RDS rotation), referenced through the task definition's `secrets` block — never inline `environment`. Plain config goes inline.

### 4.4 Frontend
- **React SPA on S3 + CloudFront** (lineage: `gotoplanb/hermit-watch-gen`).
- **Fingerprinted assets (long TTL) + short-TTL `index.html`** — a deploy never serves a half-old/half-new bundle.
- **Honest degradation is a first-class feature:** the SPA probes backend health and, when the app tier is unreachable, shows a loud read-only/stale banner plus the documented "use ServiceNow" fallback. The static shell staying up must never *imply* liveness it doesn't have. (ADR-005)

### 4.5 Feature flags
- **AWS AppConfig**, via the **AppConfig Agent** — sidecar in the ECS task in prod, container in docker-compose locally. Identical `localhost:2772` evaluation path in both. (ADR-003)
- All evaluation behind a thin `flags.is_enabled(name, default)` seam. Swapping providers later = one class.
- Note: AppConfig propagation is poll-based (~45s default) — fine for flags, never assume sub-second flips.
- A flag is a fork → "done" for a flagged feature means **both branches tested and a documented flag-removal step** once permanent.

### 4.6 Pipeline
- **GitHub validates; AWS adjudicates.** (ADR-004)
  - GitHub: lint/format only, mirroring local pre-commit hooks — fast dev feedback, non-authoritative.
  - **CodePipeline → CodeBuild** runs the authoritative test/build/scan/image-push; **CodeDeploy** does the ECS blue/green.
  - CodeBuild reports commit status back to GitHub so devs still see green/red in the PR, even though the *record* lives in AWS (CloudTrail + pipeline/deploy history).
- **ECS blue/green via CodeDeploy:** two target groups + test listener, alarm-gated canary/linear shift, automatic rollback, lifecycle hooks (`BeforeAllowTraffic`/`AfterAllowTraffic`) for pre-traffic validation.
- **Frontend deploy:** CodeBuild builds → S3 sync → CloudFront invalidation. Runs in AWS for the same audit reason.

### 4.7 IaC
- **Terragrunt**, separate stacks: `network / data / app / pipeline / frontend / escalation`.
- Pattern lineage: existing multi-stack Terragrunt project. Terraform/Terragrunt over CDK because the estate spans providers and a readable `plan` diff is an audit artifact. (ADR-006)

### 4.8 Observability
- **OpenTelemetry from day one** — Django + frontend → Collector → **Watchtower (LGTM)**. Reuse, don't rebuild.
- **Telemetry-quality monitors** from commit one: missing metadata, malformed codes, wrong log levels.
- **Masked drains:** mask/redact at the app layer; CloudWatch Logs data protection policies as the sink-level floor. Authoritative records stay complete and immutable; the aggregated copy is masked.
- **E2E:** `SmokeShow` (OTel-instrumented Playwright).

### 4.9 Migrations
- **Expand → migrate → backfill → cut over → contract.** Contract is a *separate, later* release, gated on SLOs unchanged. v1 ships one real migration exercised through the full sequence as the worked example + runbook.

---

## 5. Local development
- **docker-compose** brings up: Django, Postgres, Valkey, AppConfig Agent, Step Functions Local + Lambda containers, OTel Collector → Watchtower.
- **ngrok** exposes the local stack (phone-first loop).
- Honest tax: **Step Functions Local is clunkier than a plain scheduler** — accepted in exchange for provable escalation (ADR-001). Unit tests avoid it entirely (below).

## 6. Testing strategy
- **Unit (hermetic, no Docker/network):** test Python escalation Lambdas directly with stubbed state; in-memory flag provider; both branches of every flag; idempotency of every transition.
- **Integration:** exercise the state machine via Step Functions Local; AppConfig Agent container; real Postgres.
- **E2E:** SmokeShow/Playwright against the local stack.
- Authoritative gates run in **CodeBuild**, not GitHub.

---

## 7. Non-goals (explicitly out of v1)
- Multi-region / active-passive failover (v1 is single-region Multi-AZ; seams left clean — ADR-005).
- ServiceNow synchronization.
- SLA dashboards / reporting.
- On-call / paging integration.
- Rich document management (v1 stores attachment metadata only).

---

## 8. Repo lineage (`gotoplanb/{project}`)
| Layer | Draws from |
|---|---|
| Frontend SPA on CloudFront | `hermit-watch-gen` |
| Observability / LGTM | `watchtower` |
| E2E OTel instrumentation | `smokeshow` |
| Terragrunt multi-stack pattern | existing Terragrunt project |
| AI-assisted exception triage (later) | `argus` |
