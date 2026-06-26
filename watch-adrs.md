# Watch — Architecture Decision Records

> One ADR per decision we reasoned through, with the tradeoff each one accepts. Format: Context → Decision → Consequences (including what we give up). These exist so a reviewer — or future-you — sees the *why*, not just the *what*.

---

## ADR-001 — Step Functions (not an in-Django scheduler) for escalation timing
**Status:** Accepted

**Context.** Tiered escalation is time-based: an incident must move T1→T2→T3 on SLA deadlines, and we must be able to *prove* the handoff happened on time. The obvious option is a DB-backed scheduler (Celery Beat / APScheduler) — everything in Django, easy for permanent staff to own. Its flaw (multiple Fargate tasks → multiple loops → races) is real but solvable with `select_for_update` + idempotent transitions. So the deciding question was *not* "can we fix the race" — it was "is the escalation timeline core domain value that deserves durable, inspectable infrastructure?"

**Decision.** Model escalation as **one Step Functions Standard execution per incident**, with `waitForTaskToken`+timeout states per tier. **Python Lambdas make the decisions; Step Functions owns orchestration and timing.** Django remains system-of-record.

**Consequences.**
- *Gain:* the timer **cannot silently fail** — it's held by AWS, survives any task death, and a missed escalation surfaces as a *failed execution → alarm*. Escalation reliability moves from best-effort to provable. Each execution is visually inspectable (strong audit story).
- *Gain:* the pattern transfers directly to deadline-driven case routing (e.g. benefits turnaround SLAs) — same primitive.
- *Cost:* a second mental model (ASL alongside Python) and a **clunkier local loop** (Step Functions Local). Accepted *specifically because* escalation timing is the heart of this product — this would not be worth it for a generic CRUD app.
- *Guardrail:* business logic stays in readable Python Lambdas; ASL holds only orchestration. Transitions remain idempotent (Step Functions retries must be no-ops when already applied).

---

## ADR-002 — Durable, decoupled intake (webhook → queue → app)
**Status:** Accepted

**Context.** This is an incident tool. The moment it's most needed is often when infrastructure is degraded — i.e. when its own app tier may be impaired. An intake path that requires Django+RDS to be healthy will drop incidents exactly when they matter most.

**Decision.** `Sumo webhook → API Gateway → SQS → idempotent consumer → Incident`. **Acknowledge on enqueue, not on processing.**

**Consequences.**
- *Gain:* an incident is captured the instant it hits SQS (multi-AZ by default), even if the worker tier is mid-failover; processing catches up on recovery. The source never blocks on our database.
- *Cost:* eventual-consistency between "captured" and "visible as a worked incident" — acceptable and expected for intake.
- *Guardrail:* consumer dedupes on a source key / payload hash so retried deliveries don't double-create.

---

## ADR-003 — Managed feature flags (AppConfig), not a homegrown Django flag app
**Status:** Accepted

**Context.** A tempting idea: build a Django app to manage flags so local and cloud behave identically. The instinct (one mechanism everywhere) is right; the implementation is a trap.

**Decision.** Use **AWS AppConfig** via the **AppConfig Agent** (ECS sidecar in prod, container locally), behind a thin `flags.is_enabled()` seam. Unit tests use an in-memory stub.

**Consequences.**
- *Why not homegrown:* we'd be rebuilding a managed service (caching, propagation, rollback, an evaluation API) and — worse — flag-change audit would land in *application data* instead of the AWS audit boundary we deliberately standardized on (see ADR-004). It also becomes permanent in-house infrastructure and a bus-factor risk on a contract handed to permanent staff.
- *Parity is already solved:* the Agent gives an identical `localhost:2772` path locally and in Fargate. The only "difference" is the in-memory stub at the **unit-test** boundary — which is just "tests stub their dependencies," not a parity violation.
- *Principle:* prefer the managed service behind a thin interface over a homegrown version of it. The seam buys swappability for one class.
- *Cost:* poll-based propagation (~45s) — never design for sub-second flips.

---

## ADR-004 — GitHub validates, AWS adjudicates (CI/CD audit lives in AWS)
**Status:** Accepted

**Context.** The target environment does **not** have GitHub Enterprise, so GitHub's audit log is not a system-of-record we can rely on. CI/CD evidence ("which gate passed, who shipped, what shipped") must live where durable audit exists — AWS.

**Decision.** GitHub hosts repos and runs **lint/format only** (mirroring local pre-commit hooks, non-authoritative). The **authoritative** test/build/scan/deploy runs in **CodePipeline/CodeBuild/CodeDeploy**, captured in CloudTrail + pipeline/deploy history. CodeBuild reports commit status back to GitHub for developer visibility.

**Consequences.**
- *Gain:* binding gates can't be bypassed on the GitHub side; the audit trail is in AWS, consistent with the rest of the system. Closes the "local hooks can be skipped" gap — the GitHub check is a convenience mirror of a gate enforced authoritatively downstream.
- *Cost:* devs get authoritative pass/fail in CodePipeline, not natively in the PR UI; mitigated by status-back so they still see green/red in GitHub.

---

## ADR-005 — Single-region Multi-AZ + honest degradation (defer multi-region)
**Status:** Accepted

**Context.** CloudFront keeps the static SPA alive during a regional outage — but "alive but can't accept or escalate an incident" is arguably worse than honestly down, because people trust it when it's lying. True regional resilience (Aurora Global, active-passive, Route 53 failover) is a real, heavy project.

**Decision.** v1 targets **single-region, Multi-AZ** survival (RDS Multi-AZ, Fargate across AZs, SQS/ALB already multi-AZ) **+ durable decoupled intake (ADR-002) + honest degradation** in the SPA. Leave seams clean for multi-region; do not build it now.

**Consequences.**
- *Gain:* AZ-failure survival nearly for free; intake durability hardened independently; the SPA tells the truth about its own state and routes users to the ServiceNow fallback when degraded.
- *Cost:* a full regional outage degrades the worked-incident experience (intake still captures). Accepted for v1.
- *Guardrail:* don't let the "CloudFront stays up" fact lull anyone into claiming regional resilience the backend doesn't have. State the availability target explicitly.

---

## ADR-006 — Terraform/Terragrunt over CDK
**Status:** Accepted

**Context.** The estate isn't AWS-only — Cloudflare, the GitHub org, and SaaS need managing in the same IaC idiom. CDK is AWS-only.

**Decision.** **Terragrunt** with separate stacks. Default toward AWS-native services elsewhere, but IaC is the deliberate exception because the estate spans providers and a declarative `plan` diff is itself an audit artifact.

**Consequences.**
- *Gain:* one idiom across all providers; human-readable plan diffs for change control / audit.
- *Cost / tension:* slightly against the "prefer AWS-native when comparable" default — pre-empted by naming IaC as the intentional exception.

---

## ADR-007 — Incident lifecycle: orthogonal `status` × `tier`, ack doesn't stop the clock, one task token per tier
**Status:** Accepted

**Context.** Spec review surfaced three coupled gaps (#1/#2/#3). The original model used a single linear enum (`NEW → TRIAGED_T1 → ESCALATED_T2 → ESCALATED_T3 → RESOLVED`), which (a) conflated *tier* with *lifecycle* — implying resolution only follows T3, when most incidents resolve at T1/T2; (b) carried a `TRIAGED_T1` state with no T2/T3 equivalent (asymmetric); (c) left the `waitForTaskToken` token absent from the domain model, so the manual-escalation path had nothing to call `SendTaskSuccess` with; and (d) never said what resolution does to a still-parked execution — a resolved incident could later auto-escalate via timeout (zombie timer). Underneath all four sits one question: *what ends a tier's wait, and what merely annotates it?*

**Decision.**
1. **State is two orthogonal fields:** `status ∈ {OPEN, RESOLVED}` and `current_tier ∈ {T1, T2, T3}`, plus `acknowledged_at` (nullable). The old enum maps as `NEW = (OPEN, T1, ack=null)`, `TRIAGED_T1 = (OPEN, T1, ack=set)`. Resolution is legal from **any** tier.
2. **One tier = one `waitForTaskToken`** with `timeout` = that tier's SLA. The token is consumed **exactly once per tier**, by a tier-ending decision.
3. **Acknowledge does not stop the clock.** ACK is a Postgres event + audit record; it does **not** consume the token. An acked-but-unresolved incident still auto-escalates at its SLA deadline — acknowledging is not progress.
4. **Three tier-ending outcomes, one path.** ESCALATE and RESOLVE call `SendTaskSuccess` with an `outcome` field routed by an ASL `Choice` (RESOLVE → `Succeed`; ESCALATE → next tier); a timeout is caught and auto-escalates with `actor=system:auto-escalation`. Resolution terminates via `Succeed`, **not** `StopExecution`, so the execution history stays a clean, inspectable record.
5. **One outstanding token ⇒ one field.** `current_task_token` lives on Incident, written **atomically** by the tier-entering Lambda alongside `current_tier` + `sla_deadline_at`, and nulled on consume. The API **never trusts a client-supplied token**: it looks up the current one, rejects if `current_tier` moved from the client's expected tier (optimistic concurrency), and treats `SendTaskSuccess` on an already-consumed token (`TaskDoesNotExist`) as an idempotent no-op.

**Consequences.**
- *Gain:* resolve-from-any-tier is natural; the audit record carries both dimensions; the T1-only "triaged" asymmetry is gone. One token, one wait per tier — the simplest ASL that works.
- *Gain:* "ack doesn't stop the clock" keeps the core promise intact — **absence of resolution stays alarmable** (consistent with ADR-001), and the unified manual/automatic path holds because every tier-ending action goes through the same token.
- *Gain:* no zombie timers — RESOLVE consumes the token and ends the execution in `Succeed`.
- *Cost:* ACK has no protective effect — a tier whose SLA is set too short will auto-escalate even while actively worked. Mitigated by setting tier SLAs to realistic working windows; **per-incident SLA extension is deferred** as a later explicit feature, not v1.
- *Cost:* a single token field keeps no in-tier token history — acceptable, since a tier issues exactly one token and the `transitions[]` log already provides per-tier audit.
- *Guardrail:* token consumption is idempotent; concurrency races are resolved by the expected-tier check plus treating consumed-token errors as no-ops. Transitions remain idempotent (ADR-001).

---

## ADR-008 — Authentication & authorization: Django session auth, tier-role-or-above, separate webhook auth
**Status:** Accepted

**Context.** Spec review (#5) found auth unspecified: §4.3 externalizes sessions to Valkey and §3 names T1/T2/T3 roles, but no authentication mechanism was stated and no rule said *who* may advance or resolve an incident — i.e. nothing guarded the `SendTaskSuccess` call introduced in ADR-007. Two sub-problems: human auth (which must cohere with the existing Valkey *session-cookie* decision — a stateless JWT model would moot it), and machine intake auth (the webhook), plus the authorization rule itself. ADR-003's "prefer a managed service over homegrown" instinct points at Cognito, but Django's session auth is a framework feature, not a reimplemented managed service, and it's the option coherent with §4.3.

**Decision.**
1. **Human authentication = Django's built-in session auth**, server-side session cookies in **Valkey** (consistent with §4.3 and the stateless-task requirement). Tiers are **Django Groups** (T1/T2/T3). **SSO/OIDC federation is left as a clean seam** (pluggable Django auth backend), not built in v1.
2. **Authorization rule for tier-ending actions** (ACK / ESCALATE / RESOLVE): a user may act **iff they hold the incident's `current_tier` role or any higher tier** (senior override). Role-based, **not** per-assignee (v1 keeps assignment simple, §3). This is the guard in front of the `SendTaskSuccess` path from ADR-007.
3. **Intake/webhook auth is machine-to-machine and fully separate** from human sessions: **API Gateway** authorizes the Sumo webhook via a shared secret / request signature (or IAM), validated **before** SQS enqueue. The human path and intake path share no credentials.

**Consequences.**
- *Gain:* coherent with the existing Valkey session decision — no rework of §4.3; trivial for the local/phone loop; no external IdP dependency for v1.
- *Gain:* the tier role is a real **access boundary**, so "who was allowed to hand off" is enforced and auditable — the `actor` on each `Transition` is an authenticated, authorized user, reinforcing the core audit story (ADR-001).
- *Gain:* senior override ("or above") matches real ops without per-assignee complexity.
- *Cost:* **no SSO in v1** — users carry app-local credentials, real identity-duplication friction for a day-job tool; accepted, with the OIDC backend seam as the upgrade path (consistent with ADR-005's "leave seams clean").
- *Cost:* "or above" lets a higher tier act on a lower-tier incident the assigned tier hasn't yet seen — intentional (override), so tier is a floor, not an exclusive lock.
- *Guardrail:* the authz check sits at the API boundary in front of `SendTaskSuccess`; combined with ADR-007's expected-tier/optimistic-concurrency check, an authorized-but-stale action is still rejected. The webhook secret is handled per §4.3 (Secrets Manager / SSM), not inline.

---

## ADR-009 — Intake idempotency: source-id-first key, dedupe scoped to the open incident
**Status:** Accepted · *Refines ADR-002*

**Context.** ADR-002 required an idempotent consumer that "dedupes on a source key / payload hash," but spec review (#4) found that under-specified: a source key and a payload hash are **not** equivalent, and neither alone handles both retried deliveries *and* legitimate recurrences. A pure content hash conflates "the same alert fired again next week" with "this delivery was retried"; requiring a source id outright is brittle if a source can't emit one.

**Decision.**
1. **Dedupe key** = the source-provided event/delivery id when present, **else** `sha256` over a **normalized** payload (volatile fields — firing timestamp, delivery/sequence ids — stripped before hashing; the exact strip-list is per-source config).
2. **Dedupe is scoped to the open incident:** a partial unique constraint `UNIQUE(dedupe_key) WHERE status = OPEN`. The consumer creates via `INSERT … ON CONFLICT DO NOTHING`.
   - Retries/redeliveries while the incident is `OPEN` → idempotent no-op.
   - A re-fire **after** the incident is `RESOLVED` → a new incident (recurrence honored).
3. **Enforcement is in Postgres** (system-of-record), not the queue. SQS stays **standard** (not FIFO); FIFO content-dedup (5-min window, ordering/throughput constraints) is neither durable nor authoritative enough for this guarantee. The DB constraint also resolves the concurrent-consumer race.

**Consequences.**
- *Gain:* retried deliveries can't double-create (ADR-002's goal), and genuine recurrences after resolution still raise a fresh incident — both behaviors fall out of one partial index tied to the ADR-007 `status` field.
- *Gain:* precise dedupe for sources that emit a real event id; a safe fallback for those that don't, without rejecting deliveries.
- *Cost:* while an incident is `OPEN`, a *legitimately distinct* event that happens to normalize to the same key (no source id, identical content) is absorbed into the open incident rather than raised separately — accepted; it surfaces as repeat deliveries on the existing incident, and a real source id avoids it.
- *Cost:* per-source normalization (which fields are volatile) is config that must be maintained as sources are added — a small ongoing tax, localized to intake.
- *Guardrail:* the unique index is the authority; the consumer's `ON CONFLICT DO NOTHING` must be a true no-op (no partial side effects before the insert), keeping intake idempotent under retry and concurrency (consistent with ADR-001/002).
