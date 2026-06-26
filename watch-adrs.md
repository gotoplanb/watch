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
