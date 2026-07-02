# Watch — Architecture Decision Records

> One ADR per decision we reasoned through, with the tradeoff each one accepts. Format: Context → Decision → Consequences (including what we give up). These exist so a reviewer — or future-you — sees the *why*, not just the *what*.

---

## ADR-001 — Step Functions (not an in-Django scheduler) for escalation timing
**Status:** Accepted

**Context.** Tiered escalation is time-based: an incident must move T1→T2→T3 on SLA deadlines, and we must be able to *prove* the handoff happened on time. The obvious option is a DB-backed scheduler (Celery Beat / APScheduler) — everything in Django, easy for permanent staff to own. Its flaw (multiple Fargate tasks → multiple loops → races) is real but solvable with `select_for_update` + idempotent transitions. So the deciding question was *not* "can we fix the race" — it was "is the escalation timeline core domain value that deserves durable, inspectable infrastructure?"

**Decision.** Model escalation as **one Step Functions Standard execution per incident**, with `waitForTaskToken`+timeout states per tier. **Python Lambdas make the decisions; Step Functions owns orchestration and timing.** Django remains system-of-record.

**Assumed volume (#6).** This is sized for **< 100 incidents/day**, human-paced — low-priority research/investigation tasks, not a high-frequency alarm stream. That implies a handful of state transitions per execution and few concurrent open executions. Standard workflows' 25k-event history ceiling is therefore non-binding, and per-transition billing is negligible at this rate. **At machine-generated or high-frequency volume, one-execution-per-incident would be the wrong primitive and this ADR must be revisited.**

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

---

## ADR-010 — Local realization of the escalation engine: commit-Lambda-as-sole-writer + host shim
**Status:** Accepted · *Realizes ADR-001/007*

**Context.** ADR-001/007 put escalation in Step Functions with Python Lambdas deciding. Making the engine *actually run* locally (not the `LOCAL_MODE` shortcut where the API calls `services` directly) raised two questions: (a) **who writes the Transition** when a human escalates — the API or a Lambda — and how the actor reaches the writer; and (b) **how Lambdas run locally** with DB access when this sandbox can't build Lambda images (no PyPI/registry egress at build time). A naïve "API writes the transition *and* SendTaskSuccess advances the ASL, which re-tokenizes" double-writes and races the Lambda.

**Decision.**
1. **The commit Lambda is the sole writer of Transitions when the real engine runs.** The API only `SendTaskSuccess({outcome, actor})`; the acting user flows through the task output (`$.decision.actor`), and timeouts commit with `actor=system:auto-escalation`. One ASL graph: each tier `waitForTaskToken` (`record_token` persists token+SLA) → `Choice` → a `commit` task that calls the shared idempotent `incidents.services.escalate/resolve`. Removes the API-vs-Lambda race; matches the spec's "Lambdas decide and write back".
2. **Lambdas run on the host behind a tiny Lambda-Invoke shim** (`incidents/lambda_shim.py`, `run_lambda_shim`) that Step Functions Local reaches via `LAMBDA_ENDPOINT=http://host.docker.internal:9050`. Handlers `django.setup()` and call the same `services` the API uses — one decision implementation, two callers (ADR-001 guardrail).
3. **`ESCALATION_LOCAL_MODE` keeps the direct-`services` path** so hermetic unit tests and a no-SFN dev loop work without the engine; the real path is integration-tested (Step Functions Local + shim + real Postgres).

**Consequences.**
- *Gain:* the real **timeout → auto-escalate → Transition(system)** and human SendTaskSuccess round-trips run against AWS's own engine locally — the core domain value, proven by `test_escalation_e2e.py`. ASL routing is separately covered by mocked Step Functions Local tests and a hermetic structural test.
- *Gain:* manual and automatic transitions are identical in shape and idempotent; SFN retries and the LOCAL_MODE/real overlap are safe.
- *Cost:* the host shim is a local-only stand-in for deployed Lambdas (prod packages the same handlers via the Terragrunt escalation stack). It speaks just enough of the Lambda Invoke API — notably **chunked** request bodies and the function-error header.
- *Cost:* two code paths (LOCAL_MODE direct-call vs real engine). Accepted: LOCAL_MODE keeps units hermetic and the phone-first loop simple.
- *Guardrail:* each tier wait sets `ResultPath` so the task output never clobbers `incidentId`; transitions stay idempotent (ADR-001).

---

## ADR-011 — Server-rendered working surface (Django + HTMX) + React status page
**Status:** Accepted · *Refines §4.4*

**Context.** §4.4 specified a single React SPA on S3+CloudFront as *the* frontend. But two needs are distinct: (a) the **internal working surface** where investigators view / comment / ack / escalate / resolve — high-interaction, auth-gated, tightly coupled to the Django domain and its tier authz; and (b) a **public, at-a-glance status page**. Building (a) as an SPA duplicates the domain + authz across a JSON API and a client, and adds a JS build/deploy pipeline for what is mostly server-driven forms over existing models.

**Decision.**
1. **The working surface is server-rendered Django templates + HTMX + Alpine.js + Tailwind** (`/ui/...`). Mutating actions reuse the **same `incidents.services` decision functions and tier-or-above permissions as the API**; HTMX swaps one `#incident-body` partial. Assets load via **CDN (zero build)** for v1; prod compiles Tailwind to a fingerprinted bundle in CodeBuild (§4.6).
2. **The React SPA (S3+CloudFront) narrows to a status page** (system health + incident posture), keeping the honest-degradation story (ADR-005). Read-only; need not share the working surface's session auth.
3. **Added a `Comment` model** (flat: author, body, timestamp), rendered in the incident timeline next to Transitions. Internal-only (this tool runs alongside ServiceNow).

**Consequences.**
- *Gain:* one implementation of incident state + authz drives **both** the API and the UI — no duplicated domain logic in a client. The working surface ships without a JS build step and is fast to extend.
- *Gain:* clear separation of concerns — interactive internal work (Django/HTMX) vs public read-only status (React SPA).
- *Cost:* two frontend idioms instead of one. Accepted: each fits its job, and the SPA shrinks to a thin read-only surface.
- *Cost:* Tailwind via CDN isn't production-grade (no purge/fingerprint). Mitigated by the documented prod path (compile in CodeBuild, §4.6).
- *Guardrail:* UI actions go through the same idempotent services + permission checks as the API (ADR-001/008); no business logic in templates. New UI code is held to the same coverage + Sonar gates.

---

## ADR-012 — On-call schedule overlays tier Groups (capability vs. responsibility)
**Status:** Accepted · *Refines ADR-008 / §3*

**Context.** Tiers are static Django Groups granting who *may* act (ADR-008, tier-or-above). Real escalation also needs to know who is **on-call** for each tier right now — to auto-assign and to page (ADR-013). Making the schedule the source of authz would couple "can act" to the rota: a scheduling gap would leave a live incident with **nobody authorized**, and it loses senior-override / coverage.

**Decision.** Add an on-call schedule that **overlays** the Groups; it does **not** change authz.
- **Groups remain capability/authz** (tier-or-above, ADR-008), unchanged.
- An **`OnCallShift`** (tier, user, starts_at, ends_at) defines **responsibility**. `current_on_call(tier, at=now)` resolves the active shift (most recent if overlapping; `None` on a gap).
- On entering/escalating to a tier, the engine sets `incident.assignee = current_on_call(tier)` — realizing §3's "auto-route on escalation" and giving the unused `assignee` field meaning. A gap leaves `assignee` null; the incident stays actionable by any T-or-above member.
- v1: explicit shifts (admin + a small HTMX schedule view). Recurring-rotation generation is later.

**Consequences.**
- *Gain:* who-can-act and who's-on-call are orthogonal — a rota gap degrades to "anyone in the tier can act," never "no one can." Senior override preserved.
- *Gain:* `assignee` becomes real, giving paging a target (ADR-013).
- *Cost:* two concepts (membership + schedule) to manage — accepted; they answer different questions.
- *Guardrail:* assignment is **advisory** (who *should* take it); authz is still the gate on every action (ADR-008). Assigning grants no authority.

---

## ADR-013 — Escalation paging via ntfy, targeted at the on-call
**Status:** Accepted

**Context.** Escalation moves the tier silently — no human is notified. We need to page the responsible person when an incident reaches their tier (manual or auto). ntfy.sh is a lightweight pub-sub push service (HTTP POST to a topic; phone/web subscribers) that fits the phone-first loop.

**Decision.**
- On a **real tier change** (new incident at T1, or escalate to T2/T3 — **not** on ACK), **page the current on-call** (ADR-012) via ntfy: POST to the on-call's per-user topic `watch-<env>-user-<id>`; **fall back** to the tier topic `watch-<env>-tier-<T>` when the rota has a gap.
- **One hook in the single transition-writer** (`services`), so manual and auto escalation page through one path (ADR-001).
- **Behind a flag** `paging_enabled` (ADR-003); **best-effort**, with a notification **audit record** per attempt (sent/failed). Provider behind a thin `notify(...)` seam so ntfy can be swapped/self-hosted.

**Consequences.**
- *Gain:* humans actually get paged on time, targeted to the on-call, quiet, degrading to a tier broadcast on a gap.
- *Cost / caveat:* ntfy topics are **public by default** — prod uses **access tokens or a self-hosted ntfy** (topic names are guessable and pages can be sensitive). The seam keeps that swap to one class.
- *Cost:* best-effort paging is not guaranteed delivery. Upgrade path: enqueue (SQS) → a notifier with retries, decoupled from the engine; the audit record makes misses visible.
- *Guardrail:* paging is **fire-and-forget after the transition commits** — it never blocks or alters the escalation decision, and a paging failure ≠ an escalation failure (the latter is still the alarmable "failed execution", ADR-001).

---

## ADR-014 — Rollout modes: generalize the flag seam (on/off/sample), env-var + AppConfig providers
**Status:** Accepted · *Refines ADR-003*

**Context.** ADR-003 put a thin `flags.is_enabled(name, default)` seam in front of AppConfig so the provider is swappable. Two needs push past a boolean: (a) some controls want **always-on / always-off / random-sampling** — e.g. run AWS DevOps Agent on a *fraction* of incidents (`gotoplanb/platform#17`), or sample any expensive/experimental path; and (b) the right *storage* differs by case. A stable, reviewed per-environment posture ("always on for work") is better as an **env var set via Terraform** — a change is a PR + `plan` diff + redeploy, audited in the IaC (ADR-006/004) — while an occasional/on-demand toggle or a live-tuned sample rate ("occasionally in the personal project") wants **AppConfig**'s runtime flip (~45s, CloudTrail). So *env var vs feature flag is a **provider** choice behind the one seam, not an either/or.*

**Decision.**
1. **Generalize the seam to a rollout mode.** A control resolves to `on`, `off`, or `sample:<rate>` (0.0–1.0). The seam exposes `rollout.active(name, key=None) -> bool`: `on`→True, `off`→False, `sample:R`→**deterministic** `hash(key) < R` (stable per entity — a given incident is consistently in or out), or random when no `key`. `flags.is_enabled(name, default)` stays as the on/off convenience wrapper.
2. **Pluggable providers, one value format** (`on|off|sample:0.1`): **env var** (static per-env via Terraform/ECS task def — the **default**; keeps config changes in the IaC audit trail), **AppConfig** (runtime toggle + live-tunable rate via the agent; identical `localhost:2772` path, ADR-003), **in-memory** (tests).
3. **Flag taxonomy.** **Release flags** are short-lived forks ("done" = both branches tested + a documented removal step, ADR-003). **Operational toggles / kill-switches** (`devops_agent.*`, `paging_enabled`) are **permanent** — kept indefinitely, both branches tested forever; the removal step does not apply.
4. **Implementation lands with the first consumer** (paging `gotoplanb/watch#8` or DevOps Agent #17), behind the existing `incidents/flags.py` seam, with both branches + the sampling boundary unit-tested (90% gate). Built ahead of a consumer = unused, so deferred.

**Consequences.**
- *Gain:* one reusable primitive covers always-on / always-off / sampled rollouts for anything (cost-gating an agent, sampling an experiment, rate-limiting), with storage chosen per case.
- *Gain:* "always on for work" = env var via Terraform (reviewed, IaC-audited, stable); "occasionally in personal" / live rate tuning = AppConfig (nimble). Each environment picks without app changes.
- *Gain:* deterministic sampling makes sampled behaviour debuggable and stable per entity.
- *Cost / nuance vs ADR-003:* env var becomes a first-class (default) provider, softening "prefer the managed service" — justified, since a Terraform-set env var is *more* reviewed/auditable than a runtime flip for stable posture; AppConfig stays for when runtime dynamism is the point.
- *Guardrail:* AppConfig propagation is poll-based (~45s) — never assume sub-second flips (ADR-003). Operational toggles are permanent — don't "clean them up."

---

## ADR-015 — Cost profiles: lean (public-subnet, no-NAT) default with a documented HA upgrade
**Status:** Accepted · *Refines ADR-005*

**Context.** ADR-005 targets single-region Multi-AZ survival (RDS Multi-AZ, private subnets behind NAT, Fargate across AZs). For the **personal deployment** that runs ~$220–280/mo across a persistent staging+prod — **NAT gateways (~$36/env)**, **Multi-AZ RDS** (~2× the instance), and **two always-on environments** dominate the bill. Goal: keep dev/personal use **< $100/mo** without throwing away the HA design.

**Decision.** Two **cost profiles**, selected by Terragrunt variables; **lean is the default for personal/dev**, **ha** is one flag-flip away and fully documented.
- **lean (default):** **public subnets + public-IP Fargate (no NAT gateway)**; one persistent **prod** env + **ephemeral staging** (spun up for a pipeline run, then `terragrunt destroy`); RDS **single-AZ** (Multi-AZ optional). ≈ **$60–90/mo**.
- **ha:** private subnets + **NAT gateway(s)**, RDS **Multi-AZ**, Fargate across AZs — the ADR-005 design — applied for occasional secure testing then torn down, or kept on when the workload warrants.
- The **network stack ships the toggle from day one** (e.g. `enable_nat` / `private_networking`, default `false`) so switching profiles is a *variable change, not a rewrite*. App subnet placement + `assign_public_ip` and RDS `multi_az` follow the profile.
- **The architecture docs say so explicitly:** "these are public subnets, chosen for cost and simplicity while developing; see *Extending to private subnets + NAT* for the secure profile" (`platform/ROLLOUT.md`).

**Consequences.**
- *Gain:* ~$60–90/mo personal/dev cost; the HA/secure profile is a documented, low-friction toggle for occasional testing — best of both worlds. Ephemeral staging means you pay for pre-prod only while a pipeline run needs it.
- *Cost / trade-off:* lean runs the app in **public subnets with public IPs** and (optionally) **single-AZ** RDS — a conscious reduction of isolation/survival vs ADR-005, accepted for personal/dev. **Not** the posture for the day-job production estate, where `ha` (private + Multi-AZ) applies.
- *Guardrail:* keep **both code paths exercised** — `tofu validate`/`plan` both profiles so the `ha` path never bit-rots. Security groups stay least-privilege regardless of subnet placement; secrets/state posture is unchanged.

---

## ADR-016 — Telemetry topology: the app is backend-agnostic; OTLP to a local Alloy collector, topology varies per environment
**Status:** Accepted · *Refines ADR-003* · *Amended 2026-06-30: staging uses the sidecar+gateway topology too (mirrors prod, per ADR-019); the only per-env difference is the exporter destination; Watchtower runs in AWS as a platform slice (ADR-018), never the laptop.* · *Amended 2026-07-02: plane **deployed & verified end-to-end on staging** (app → local Alloy sidecar → per-env Alloy gateway → Tempo, viewed as live traces in Grafana). Realized as platform-repo modules `modules/{alloy,gateway,tempo,grafana}` — the shared Alloy renderer (`sidecar`/`gateway` roles) is the "shared config module" guardrail; the Watchtower slice is these modules (rewritten from the ~/watchtower draft), **co-located in the staging VPC and kept warm-minimal** (deviations → ADR-018/019). **Prod's gateway exports to Grafana Cloud** (managed vendor), confirming §2 — no Tempo/Watchtower in product prod. Tail-sampling (§3) is a gated var, currently off pending enablement.*

**Context.** The application must **never know what telemetry backend exists, in any environment.** An app that names a vendor or a remote endpoint couples it to that backend's identity and availability, and forces a redeploy to switch. Earlier notes ("OTel → existing Watchtower") conflated a local dev convenience with a deployed topology and implied prod depends on Watchtower — it must not (ADR-018).

**Decision.**
1. **The app emits OTLP to a *local* Alloy collector — identical app config everywhere.** `OTEL_EXPORTER_OTLP_ENDPOINT` → localhost (a sidecar); `OTEL_RESOURCE_ATTRIBUTES=deployment.environment=<env>`; `service.version` = git SHA. **No vendor name, no exporter-type switch, in the app, ever.** *Where* telemetry goes is a **collector-config IaC artifact per environment**, never an app env var. Switching a backend (Datadog → Sumo) is a collector-config change, **no app redeploy**.
2. **Both deployed envs share the same topology; the Alloy *config* is shared.** Local = Alloy as a compose service → local Watchtower/debug, 100%. **Staging and prod both** = Alloy **sidecar per app task** + a **gateway collector**, **tail-based** sampling (keep all errors + slow; sample boring successes). We don't run many apps, so a shared per-env collector isn't worth diverging staging from prod (ADR-019). The **only per-env difference is the gateway's last hop:** staging → the **in-AWS Watchtower platform slice** (ADR-018); prod → **managed vendor** (Grafana Cloud / Datadog / Sumo). *(Originally staging used one shared lean Alloy; amended — staging mirrors prod.)*
3. **The prod gateway owns the three things you don't spray across tasks:** vendor **credentials** (Secrets Manager → gateway, not every task); **redaction/masking before egress** (centralized at the boundary — the masking floor of §4.8 raised to the telemetry layer); **tail-based sampling** (requires seeing the whole trace, which a per-instance sidecar can't — this alone justifies the gateway).
   - **The tail-sampling policy is incident-tuned (#23, implemented 2026-07-02).** A trace is kept if it matches **any** policy: `errors` (any ERROR span), `slow` (over the latency floor, default 1s), `writes` (`http.method` ∈ POST/PUT/PATCH/DELETE — every ack/escalate/resolve/intake, i.e. every state transition), else a `reads` probabilistic slice (default 10%) of the boring GETs (health, status, lists). Rationale: on an incident tool the state transitions and failures are exactly what you replay during a postmortem — they must **never** be sampled away — while health/status polling is high-volume and low-value. Enabled on **both** gateways (staging rehearses prod); staging verified 6/6 writes kept vs 4/50 reads (~8%). One caveat folded in: **tail-sampling requires each policy attribute on its own line in Alloy River** (compact one-liners fail to parse) — and it's **traces-only**, so a **traces-only backend (Tempo) must drop metrics + logs at the gateway** (`dest_traces_only`) or they're rejected `Unimplemented`; a full LGTM/vendor (Grafana Cloud) takes all three.
4. **Sidecar everywhere in prod keeps egress off the app's critical path** — backend down ≠ app blocked; the sidecar buffers. The app never exports straight to a remote endpoint.
5. **Staging is a faithful prod rehearsal:** identical sidecar + gateway + processors/redaction/tail-sampling — only the gateway's exporter **last hop** differs (the **in-AWS Watchtower platform slice**, not the laptop). The one thing this can't cover — the **vendor-specific export** (auth, ingestion quirks, dashboards) — is validated in a vendor free-tier project; Watchtower can't stand in for it anyway.

**Consequences.**
- *Gain:* swap a backend = collector change, no redeploy; egress off the critical path; creds/redaction/tail-sampling centralized at one boundary; "the app knows nothing about its environment" enforced.
- *Cost:* both envs run a sidecar per task (small overhead) **plus** a gateway — consistent shape, fidelity over micro-optimizing staging. The **in-AWS Watchtower platform slice** (ADR-018) is staging's destination and must be deployed (its own platform-plane build).
- *Guardrail:* keep the Alloy **config** as a shared module consumed by both envs' sidecars/gateways; the **only per-env override is the exporter destination**. Watchtower stays out of product prod (prod → managed vendor) — the platform Watchtower slice serves staging + dogfooding, not a product's prod.

---

## ADR-017 — Build once, promote by digest (immutable artifact across environments)
**Status:** Accepted · *Refines ADR-004*

**Context.** Why is it safe to **skip the prod scan**? Not because "staging already scanned it" — that conflates the build plane with a runtime environment. The real reason is **immutable artifact promotion**. A pipeline that **rebuilds per environment** cannot make the guarantee, because prod would run different bytes than the gate approved.

**Decision.**
1. **Build + scan exactly once**, off a commit. Tag immutably and reference downstream **by digest** (`image@sha256:…`). **Never `:latest`, never rebuild per env.**
2. Staging deploys digest X; prod deploys the **same** digest X. Prod is covered because it runs **the same bytes** that passed the gate.
3. **Promotion = the approval gate, not a build job.** staging→prod is "approve digest X for prod" — and the auditable who/what/when record (ties to ADR-004's AWS-adjudicates trail). **No CodeBuild in the prod path;** prod is pure CodeDeploy of a known digest.
4. This is the guarantee **blue/green** already relies on (blue + green run the same image; instant rollback because the artifact is identical). Cross-environment promotion is that same idea on a longer axis.
5. **Corollary:** anything differing between staging and prod is **injected at runtime** (config, endpoints, OTLP target, secrets), never baked in. "Immutable promotion" and "the app knows nothing about its environment" (ADR-016) are the same discipline from two angles.

**Consequences.**
- *Gain:* prod runs exactly what the gate passed; promotion is an auditable approval, not a rebuild; rollback is instant (same artifact); the scan runs once, not per env.
- *Cost:* requires an artifact registry + a promotion/approval stage, and the pipeline must thread a **digest** (not a tag) through environments.
- *Guardrail:* the current pipeline (`platform#10`) builds in CodeBuild and deploys straight to prod — it must be **reshaped to build-once + promote-by-digest with no prod CodeBuild**. Open item: confirm one image is promoted, not rebuilt per env.

---

## ADR-018 — The platform/ops plane, and a clean account-isolation seam
**Status:** Accepted · *DEBT noted on account split* · *Amended 2026-07-02: the in-AWS Watchtower slice is **deployed** (Tempo + Grafana, platform-repo modules) but **co-located in the staging VPC**, not yet a separate platform account/VPC — a pragmatic warm-minimal start. This is the accepted single-account DEBT (§4) applied to the platform plane too; the Terragrunt/IAM seam is preserved so the split stays a config change, not a rewrite. Product prod still goes to Grafana Cloud (managed vendor), so "prod depends on Watchtower = no by construction" holds.*

**Context.** A recurring confusion: "does prod depend on Watchtower?", "is the SonarQube server prod?". Durable in-AWS **Watchtower-as-a-service**, the **SonarQube server**, and **throwaway dogfooding probes / example apps** used *while building* Watch / Watchtower / Conduct are infrastructure for **building the products** — not the production environment **of** any product.

**Decision.**
1. **Name a `platform` / `ops` / `tooling` plane** distinct from any product's prod. Durable Watchtower-as-a-service, the SonarQube **server** (durable: gate config, history, coverage trends), and dogfooding probes live there. Each product's prod path goes to the **managed vendor** — so "does prod depend on Watchtower?" is **no by construction.**
2. **Build-time gates vs runtime observability are separate planes.** The SonarQube **scan** is a CodeBuild pipeline step (reads *source*; gates **entry into** staging — it is not a property *of* staging). The SonarQube **server** is durable shared platform infra, **not** lifecycle-coupled to the throwaway staging stack. Local convenience grouping (Alloy + Grafana + Sonar in one compose) ≠ deployed-topology grouping.
3. Running throwaway probes that export to the in-AWS Watchtower **while developing the platform** is legitimate **platform-development telemetry**, not application-prod telemetry.
4. **Account isolation (accepted DEBT).** Staging + prod share **one AWS account** today — known, written-down debt, not a settled design (shared IAM blast radius, shared quotas, staging can affect prod). Target: **separate accounts per plane/env** under an Org with cross-account role assumption. The account-split question and "what plane does Watchtower live in" are the **same** question. Keep the Terragrunt/IAM seam (env-level account id + provider assume-role) so the split is a **later config change, not a rewrite** — splitting *after* assuming a shared account is painful.

**Consequences.**
- *Gain:* "prod depends on Watchtower?" is unambiguously no; the account split becomes configuration, not a refactor; Sonar server/history isn't tied to ephemeral staging.
- *Cost:* another named plane to model; the multi-account target is deferred (debt), with shared-account blast radius until it lands.
- *Guardrail:* don't lifecycle-couple platform-plane infra (Watchtower, Sonar server) to product stacks; keep the account seam clean even while single-account, so ADR-005's "leave seams clean" holds for environment isolation too.

---

## ADR-019 — Staging mirrors prod (ha); cost is controlled by ephemerality, not a leaner architecture
**Status:** Accepted · *Refines ADR-015 (reverses "staging is lean")* · *Amended 2026-07-02: for staging + the co-located observability slice, cost is now controlled by **scale-to-minimal (warm standby), not destroy/recreate**. They serve continuous CI/CD — staging is the DAST + functional-smoke gate target and the slice is its telemetry backend — so tearing them down each release (and eating the DNS negative-cache / re-auth churn) isn't worth it. `desired_count` carries `ignore_changes` so tasks scale down when idle without Terraform fighting it. This refines §2's ephemerality for these two; the `terragrunt destroy` / recreate loop still exists for a clean slate.*

**Context.** ADR-015 made **staging lean** (public subnets, no NAT) to save money, treating it as a cheap throwaway. But staging's whole job — the authoritative **build → scan → blue/green deploy → expand-migration rehearsal** (ADR-004 / ADR-017 / #12) — is only trustworthy when staging is **as close to prod as possible**. A lean staging that differs from prod (no NAT, public-subnet placement, different egress) doesn't actually exercise the prod topology, and it forced lean-specific workarounds prod never needs (no-NAT egress for the VPC escalation/intake Lambdas + the migration hook). **Fidelity beats lean-cost.**

**Decision.**
1. **Staging runs the `ha` profile — the same topology as prod** (private subnets + NAT, the ADR-005 shape). What passes staging genuinely reflects prod, and there are no lean-only egress hacks.
2. **Cost is bounded by *time*, not *shape*:** staging is **ephemeral** — `terragrunt destroy`-ed (or scaled to 0 tasks) **between releases** and recreated for a release run. At a ~weekly cadence it meters only during the release window, preserving ADR-015's "< $100/mo personal" goal via ephemerality.
3. **Smaller, not leaner:** when up, staging may run **fewer tasks + smaller instance sizes** and **single-AZ RDS** (it's disposable) — but the **same topology** (NAT, private subnets) as prod. Shrink the dials, don't change the shape.
4. This **reverses ADR-015's "staging is lean."** The lean *toggle* still exists for a truly-throwaway personal sandbox, but **staging is `ha`-ephemeral, not lean.**

**Consequences.**
- *Gain:* build/scan/deploy/migrate rehearse in a prod-identical environment (real fidelity); the #27 lean-egress work is moot — staging keeps NAT.
- *Gain:* **legitimate security testing.** A prod-faithful staging is a real DAST / penetration-test target (the actual private-subnet/NAT topology, SG/listener/IAM wiring) — not a lean stand-in with a different attack surface. Because it's ephemeral + disposable with no real data, aggressive/fuzzing/destructive scans are safe and prod is never touched. Pairs with build-time SAST (the Sonar gate, ADR-004): SAST at build, DAST/pentest against the running replica.
- *Gain:* cost stays bounded by destroying staging between releases, matching the weekly cadence; recreate is ~15 min wall-clock per release.
- *Cost:* staging meters ≈ prod (~$0.18/hr) **while up** — accepted, time-bounded.
- *Scope note:* this is the **network/compute/HA** axis. Per the ADR-016 amendment, staging's **telemetry topology also mirrors prod** (sidecar + gateway) — the only telemetry difference is the exporter's last hop (staging → in-AWS Watchtower slice, prod → vendor).
