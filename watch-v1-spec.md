# Watch — v1 Technical Spec
*Incident intake & tiered escalation platform*

> This document describes the **v1 vertical slice** — the minimal end-to-end spine that exercises every architectural decision once. Design decisions and their rationale live in the companion file, `watch-adrs.md`.

> **Assessment-first extension (ADR-054/055).** The primary operational entry point
> is `/ui/assessments/`: signal → customer-scoped evidence → conclusion → monitoring,
> no issue, or promotion. The implementation includes saved diagnostic execution,
> explicit automation policies, versioned communication review, specialist engagement,
> bounded evidence uploads, curated RCA and separate assessment/incident reporting.
> Incident clocks start at promotion and preserve the original signal history.
> Delivery is currently a local outbox/in-app receipt; external provider connections
> require separate configuration. Existing incident-only integrations keep their
> contract until deliberately migrated. The original vertical slice below remains
> the compatibility baseline; ADR-054/055 describe the operational extension.

---

## 1. Purpose & framing

A document-intake and tiered-escalation system for production-support and incident workflows. An incident enters via webhook from any alerting platform, becomes a tracked Document, is assigned, and moves through a T1 → T2 → T3 escalation timeline under time-based SLAs — with provable, auditable escalation.

Two intended uses from one codebase:
- **Production support** — the system responders work incidents in. Watch's boundary is a webhook in and webhooks out (ADR-049); which alerts reach it is decided in the alerting platform, not here.
- **Reference architecture** — a clonable golden-path for Django + AWS services in the idioms that matter (Terragrunt, ECS, Step Functions, OTel, expand/contract).

The escalation timeline is treated as **core domain value**, not a supporting mechanism — "did the handoff happen on time, and can we prove it" is the product's reason to exist. That single judgment drives the biggest design choice (see ADR-001).

---

## 2. Architecture overview

```
Alerting platform (any)
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

**Role/tier** — T1, T2, T3 as roles (Django Groups); users belong to one or more. Group membership = **capability/authz** (who *may* act, tier-or-above — ADR-008).

**OnCallShift (on-call schedule, ADR-012)** — `tier`, `user`, `starts_at`, `ends_at`. Defines **responsibility** (who's on-call now), distinct from capability. `current_on_call(tier)` resolves the active shift; on entering/escalating to a tier the engine auto-sets `incident.assignee` to it (realizes "auto-route on escalation"). A rota gap leaves `assignee` null; the incident stays actionable by any T-or-above member.

**Paging (ADR-013)** — on a real tier change (new incident at T1, or escalate to T2/T3), the single transition-writer pages the current on-call via **ntfy** (per-user topic, tier-topic fallback), behind the `paging_enabled` flag, best-effort with an audit record. ntfy topics are public by default → prod uses access tokens / self-hosted ntfy.

---

## 4. Layers

### 4.1 Intake
- `Alerting-platform webhook → API Gateway → SQS → consumer`. One vendor-neutral contract (`source`, `title`, `source_event_id`, `payload`); senders map their alert onto it with their own webhook template — Watch has no per-vendor parsers (ADR-049).
- **Webhook auth (ADR-008):** machine-to-machine, separate from human sessions — API Gateway authorizes via shared secret / request signature (or IAM), validated *before* enqueue. No shared credentials with the user-facing path.
- **Ack on enqueue, not on processing.** The source never waits on Django/RDS. (ADR-002)
- **Idempotent consumer (ADR-009):** dedupe key = source-provided event id when present, else `sha256(normalize(payload))` (volatile fields stripped). Enforced in Postgres via a **partial unique index `UNIQUE(dedupe_key) WHERE status = OPEN`** + `INSERT … ON CONFLICT DO NOTHING` — retries while OPEN are no-ops; a re-fire after `RESOLVED` opens a new incident. SQS stays standard (not FIFO); the DB constraint is the authority and resolves the concurrent-consumer race.
- Consumer creates the Incident, then **starts one Step Functions execution** for it.

### 4.2 Escalation engine (Step Functions)
- **One Standard-workflow execution per incident**, started at creation. Standard workflows bill per state transition and run up to a year — ideal for slow, human-paced escalation timelines. **Sized for < 100 incidents/day, human-paced** (ADR-001); not for high-frequency/machine-generated streams.
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
- **Auth (ADR-008):** Django built-in **session auth** (cookies in Valkey); tiers as **Django Groups** (T1/T2/T3); SSO/OIDC left as a clean seam. **Authorization for ACK/ESCALATE/RESOLVE:** a user may act iff they hold the incident's `current_tier` role *or higher* (senior override) — the guard in front of `SendTaskSuccess` (ADR-007). Role-based, not per-assignee.
- Secrets via SSM **SecureString** (static) and **Secrets Manager** (RDS rotation), referenced through the task definition's `secrets` block — never inline `environment`. Plain config goes inline.

### 4.4 Frontend
- **Working surface = server-rendered Django + HTMX + Alpine + Tailwind** (`/ui/...`, ADR-011): the internal investigation UI (view / comment / ack / escalate / resolve), reusing the same `services` + tier authz as the API. Assets via CDN in v1; Tailwind compiled to a fingerprinted bundle in prod (§4.6).
- **React SPA on S3 + CloudFront** (lineage: `gotoplanb/hermit-watch-gen`) — narrowed to a **read-only status page** (health + incident posture), per ADR-011.
- **Fingerprinted assets (long TTL) + short-TTL `index.html`** — a deploy never serves a half-old/half-new bundle.
- **Honest degradation is a first-class feature:** the SPA probes backend health and, when the app tier is unreachable, shows a loud read-only/stale banner plus a pointer to the team's documented fallback (Watch names no external system — ADR-049). The static shell staying up must never *imply* liveness it doesn't have. (ADR-005)

### 4.5 Feature flags / rollout modes
- All evaluation behind a thin seam (ADR-003/014). The seam resolves a **rollout mode** —
  `on` / `off` / `sample:<rate>` — via `rollout.active(name, key=None)` (deterministic
  sampling: a given entity is consistently in/out); `flags.is_enabled(name, default)` is
  the on/off wrapper. Swapping providers/format later = one class.
- **Pluggable providers** (one value format): **env var** (static per-env via Terraform/
  ECS task def — default; config changes stay in the IaC audit trail), **AWS AppConfig**
  via the **AppConfig Agent** (runtime toggle + live-tunable rate; identical `localhost:2772`
  path, sidecar in ECS / container locally), **in-memory** (tests). (ADR-014)
- Use env var for stable, reviewed per-environment posture ("always on"); AppConfig when
  you want a runtime flip / live sample tuning. AppConfig propagation is poll-based (~45s)
  — never assume sub-second flips.
- **Two flag kinds (ADR-014):** *release flags* (short-lived fork → "done" = both branches
  tested + a documented removal step) vs *operational toggles / kill-switches* (e.g.
  `devops_agent.*`, `paging_enabled`) which are **permanent** — kept indefinitely, both
  branches tested forever.

### 4.6 Pipeline
- **GitHub validates; AWS adjudicates.** (ADR-004)
  - GitHub: lint/format only, mirroring local pre-commit hooks — fast dev feedback, non-authoritative.
  - **CodePipeline → CodeBuild** runs the authoritative test/build/scan/image-push; **CodeDeploy** does the ECS blue/green.
  - CodeBuild reports commit status back to GitHub so devs still see green/red in the PR, even though the *record* lives in AWS (CloudTrail + pipeline/deploy history).
- **ECS blue/green via CodeDeploy:** two target groups + test listener, alarm-gated canary/linear shift, automatic rollback, lifecycle hooks (`BeforeAllowTraffic`/`AfterAllowTraffic`) for pre-traffic validation.
- **Frontend deploy:** CodeBuild builds → S3 sync → CloudFront invalidation. Runs in AWS for the same audit reason.

### 4.7 IaC
- **Terragrunt**, separate stacks: `network / data / app / pipeline / frontend / escalation`.
- **Cost profiles (ADR-015):** **lean** (public subnets / no NAT, ephemeral staging,
  single-AZ option) is the default for personal/dev (~$60–90/mo); a Terragrunt toggle
  flips to **ha** (private subnets + NAT + Multi-AZ, the ADR-005 design) for occasional
  secure testing. The public→private extension is documented in `platform/ROLLOUT.md`.
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
- Synchronization with any other ticketing system. Integrations ride the outbound event webhook (ADR-023/049); fan-out to several systems is the alerting platform's job.
- Enterprise cross-system reporting (local assessment/incident reporting is included under ADR-055).
- On-call / paging integration.
- General-purpose document management (bounded case evidence uploads are included under ADR-055).

---

## 8. Repo lineage (`gotoplanb/{project}`)
| Layer | Draws from |
|---|---|
| Frontend SPA on CloudFront | `hermit-watch-gen` |
| Observability / LGTM | `watchtower` |
| E2E OTel instrumentation | `smokeshow` |
| Terragrunt multi-stack pattern | existing Terragrunt project |
| AI-assisted exception triage (later) | `argus` |


### Assessment operations extension (ADR-055)

The local assessment workspace now includes saved read-only diagnostic execution,
customer/service/supplier dependency fan-out, explicit policy automation, immutable
communication revisions, separate human-review and customer-SLA deadlines, and an
independent durable operations worker. Automatic send captures a transactional local
outbox receipt only. A stale draft cannot silently overwrite a newer revision; new
checks/evidence revoke automatic approval. Suppression requires an audited reason.

The active workspace projects one canonical history, with evidence uploads/image
paste, operational observations/hypotheses, owned incident actions, named/subject-area
specialist handoffs, acknowledgement and action receipts. Handoffs never pause or
change incident tier clocks. RCA is a curated projection referencing source events,
with analysis and owned corrective actions requiring completion verification.
Reports separate assessment receipt and incident promotion cohorts, exclude no-issue
assessments from MTTA/MTTR, and expose source-linked drilldowns and scoped filters.
Legacy incident-only intake remains compatible; new collectors use assessment/signal
intake. Provider accounts and actual external delivery are separate integration work.
