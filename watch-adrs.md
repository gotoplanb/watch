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
**Status:** Accepted · *amended: unguessable topic names (secret salt)*

**Context.** Escalation moves the tier silently — no human is notified. We need to page the responsible person when an incident reaches their tier (manual or auto). ntfy.sh is a lightweight pub-sub push service (HTTP POST to a topic; phone/web subscribers) that fits the phone-first loop.

**Decision.**
- On a **real tier change** (new incident at T1, or escalate to T2/T3 — **not** on ACK), **page the current on-call** (ADR-012) via ntfy: POST to the on-call's per-user topic `watch-<env>-user-<id>`; **fall back** to the tier topic `watch-<env>-tier-<T>` when the rota has a gap.
- **Topic names carry a secret salt so they can't be derived from the (public) source.** `NTFY_TOPIC_SECRET` (env locally, SSM in prod — like `NTFY_TOKEN`) is HMAC-SHA256'd with each target's identity into a 12-hex suffix: `watch-<env>-user-<id>-<hmac>` / `watch-<env>-tier-<T>-<hmac>`. Each topic is **independently** unguessable and the secret never appears in the string (leaking one topic doesn't expose the secret or any other). Empty secret → the plain topic (local default, back-compatible). `manage.py paging_topics` prints the current topics to subscribe to (the suffix isn't derivable without the secret); each logged-in user also self-serves **their own** topic (+ the tier fallback topics for their tier) on the `/ui` settings page — scoped to the requester, never other users'.
- **One hook in the single transition-writer** (`services`), so manual and auto escalation page through one path (ADR-001).
- **Behind a flag** `paging_enabled` (ADR-003); **best-effort**, with a notification **audit record** per attempt (sent/failed). Provider behind a thin `notify(...)` seam so ntfy can be swapped/self-hosted.

**Consequences.**
- *Gain:* humans actually get paged on time, targeted to the on-call, quiet, degrading to a tier broadcast on a gap.
- *Cost / caveat:* ntfy topics are **public by default** and pages can be sensitive — mitigated in depth: **unguessable topic names** (the `NTFY_TOPIC_SECRET` salt above) so subscribe/publish is closed to source-readers, plus **access tokens or a self-hosted ntfy** in prod. The seam keeps the server swap to one class.
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
**Status:** Accepted · *Refines ADR-003* · *Amended 2026-06-30: staging uses the sidecar+gateway topology too (mirrors prod, per ADR-019); the only per-env difference is the exporter destination; Watchtower runs in AWS as a platform slice (ADR-018), never the laptop.* · *Amended 2026-07-02: plane **deployed & verified end-to-end on staging** (app → local Alloy sidecar → per-env Alloy gateway → Tempo, viewed as live traces in Grafana). Realized as platform-repo modules `modules/{alloy,gateway,tempo,grafana}` — the shared Alloy renderer (`sidecar`/`gateway` roles) is the "shared config module" guardrail; the Watchtower slice is these modules (rewritten from the ~/watchtower draft), **co-located in the staging VPC and kept warm-minimal** (deviations → ADR-018/019). **Prod's gateway exports to Grafana Cloud** (managed vendor), confirming §2 — no Tempo/Watchtower in product prod. Tail-sampling (§3) is a gated var, currently off pending enablement.* · *Amended 2026-07-05: tail-sampling **enabled** on the staging gateway (Session Check, ADR-022, now queries the staging Tempo end-to-end — provider `tempo`, SG-scoped query access). Added the **`authenticated` keep policy** (§3): session-bearing traces (`session.id`) are kept 100% so a Session Check is never fooled by a session that was sampled away; only unauthenticated volume is sampled.*

**Context.** The application must **never know what telemetry backend exists, in any environment.** An app that names a vendor or a remote endpoint couples it to that backend's identity and availability, and forces a redeploy to switch. Earlier notes ("OTel → existing Watchtower") conflated a local dev convenience with a deployed topology and implied prod depends on Watchtower — it must not (ADR-018).

**Decision.**
1. **The app emits OTLP to a *local* Alloy collector — identical app config everywhere.** `OTEL_EXPORTER_OTLP_ENDPOINT` → localhost (a sidecar); `OTEL_RESOURCE_ATTRIBUTES=deployment.environment=<env>`; `service.version` = git SHA. **No vendor name, no exporter-type switch, in the app, ever.** *Where* telemetry goes is a **collector-config IaC artifact per environment**, never an app env var. Switching a backend (Datadog → Sumo) is a collector-config change, **no app redeploy**.
2. **Both deployed envs share the same topology; the Alloy *config* is shared.** Local = Alloy as a compose service → local Watchtower/debug, 100%. **Staging and prod both** = Alloy **sidecar per app task** + a **gateway collector**, **tail-based** sampling (keep all errors + slow; sample boring successes). We don't run many apps, so a shared per-env collector isn't worth diverging staging from prod (ADR-019). The **only per-env difference is the gateway's last hop:** staging → the **in-AWS Watchtower platform slice** (ADR-018); prod → **managed vendor** (Grafana Cloud / Datadog / Sumo). *(Originally staging used one shared lean Alloy; amended — staging mirrors prod.)*
3. **The prod gateway owns the three things you don't spray across tasks:** vendor **credentials** (Secrets Manager → gateway, not every task); **redaction/masking before egress** (centralized at the boundary — the masking floor of §4.8 raised to the telemetry layer); **tail-based sampling** (requires seeing the whole trace, which a per-instance sidecar can't — this alone justifies the gateway).
   - **The tail-sampling policy is incident-tuned (#23, implemented 2026-07-02).** A trace is kept if it matches **any** policy: `authenticated` (any span carrying `session.id` — a session-bearing trace; added 2026-07-05), `errors` (any ERROR span), `slow` (over the latency floor, default 1s), `writes` (`http.method` ∈ POST/PUT/PATCH/DELETE — every ack/escalate/resolve/intake, i.e. every state transition), else a `reads` probabilistic slice (default 10%) of the boring GETs (health, status, lists). Rationale: on an incident tool the state transitions and failures are exactly what you replay during a postmortem — they must **never** be sampled away — while health/status polling is high-volume and low-value. **`authenticated` keeps *every* session-bearing trace** because **Session Check (ADR-022) looks a session's traces up by `session.id`**: a session that was authenticated but all *successful GETs* could otherwise be sampled down to **nothing** — a misleading "clean" — and dropping the GETs around a real error leaves the session **un-reconstructable**. So sampling only ever touches **unauthenticated** volume (the keep attribute is `keep_authenticated_attribute`, default `session.id`; note the SSE status page (ADR-024) already removed the largest read-volume source — polling — so aggressive read-sampling matters less regardless). Enabled on **both** gateways (staging rehearses prod); staging verified 6/6 writes kept vs 4/50 reads (~8%), and **4/4 authenticated GET traces kept at 10% read sampling** (chance ≈ 0.4 → the `authenticated` policy is what retains them). Two caveats folded in: **tail-sampling requires each policy attribute on its own line in Alloy River** (compact one-liners fail to parse); and it's **traces-only**, so a **traces-only backend (Tempo) must drop metrics + logs at the gateway** (`dest_traces_only`) or they're rejected `Unimplemented`; a full LGTM/vendor (Grafana Cloud) takes all three.
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

---

## ADR-020 — Multi-account isolation: cross the seam now (prod alone; clean management; cross-account promote)
**Status:** Accepted · *Resolves the account-split DEBT in ADR-018 · Refines ADR-017 (promote-by-digest across accounts)*

**Context.** ADR-018 kept account isolation as **written-down DEBT**: one account, with a clean env-level "account id + provider assume-role" **seam** so a split would later be config, not a rewrite. But **a seam you never cross is a seam you don't have** — untested cross-account config fails exactly when you split under pressure. And the debt is real *now*: product **prod shares an IAM blast radius, service quotas, and account boundary with a staging that ADR-019 deliberately makes a DAST / fuzz / pentest target.** The estate is currently **disposable and recreated daily**, so crossing the seam is *cheapest today* — no data to migrate, just a re-lay. Decision: **cross it now, for prod**, rather than carry untested debt.

**Decision.**
1. **An AWS Organization; the existing account (614933206631) creates it and becomes the management account, kept clean** — org + account governance + consolidated billing only, **no workloads**. Root hardened (MFA, no access keys).
2. **Two member accounts along the plane boundary:** `watch-prod` = the **product-prod plane**; `watch-nonprod` = the **build/CI/dogfood plane** (staging + the platform slice: ECR, pipeline, connection, ci-trigger, Watchtower/Tempo, Sonar). This makes "**prod ⊥ Watchtower** by construction" (ADR-018 §1) *physical*, and quarantines staging's aggressive security testing from prod.
3. **State: centralized in the management bucket initially; per-member state buckets are a later hardening.** The cross-account *deploy* (provider assume-role) is the crux and is proved first; keeping state in the management bucket (`watch-tfstate-<mgmt>`, keyed by path as today) avoids per-member state-backend bootstrap while the deploy path is validated. Full per-member state isolation (each member owns its bucket + lock table) is the target, staged after the deploy path is proven. *(Deviation from the original "each member owns its state" — recorded per the flag-deviations discipline.)*
4. **Cross-account promote-by-digest (the crux — proves ADR-017 across the boundary).** ECR + the pipeline stay in `watch-nonprod`; `watch-prod` pulls the **same image digest** cross-account (ECR repo policy → prod exec role); the pipeline deploys prod via a **cross-account CodeDeploy/ECS role** (artifact-bucket KMS shared cross-account). If a single digest promotes across accounts, ADR-017 is real, not theoretical.
5. **Terragrunt targets each account** via the env.hcl `account_id` + generated-provider `assume_role`/`allowed_account_ids` seam ADR-018 reserved — now *used*, per env.
6. **Prod owns its own everything in `watch-prod`:** ACM cert + Cloudflare validation, CloudFront status page, the Grafana-Cloud-token SecureString (`.env`/`get_env` unchanged — same values, applied into prod), its budgets, and its `watch-bootstrap`/`watch-ro` roles. `watch-nonprod` owns the foundation + staging + the obs slice.
7. **The org + member accounts are themselves Terraform** (`aws_organizations_organization` + `aws_organizations_account`, `prevent_destroy` — never let `destroy` close an account), managed from the management account. Account creation (root email per account via `+`-aliasing, billing) is the **one human/root step**; the cross-account wiring is code.

**Consequences.**
- *Gain:* prod's blast radius, quotas, and IAM are physically isolated from a DAST-fuzzed staging; "prod depends on Watchtower = no" is enforced by the account boundary; the promote-by-digest + cross-account deploy pattern is **proven, not assumed**; the daily teardown/recreate loop now exercises the multi-account path every cycle.
- *Cost:* **~$0** — Organizations + member accounts are free, consolidated billing, pay only for resources (free tier is shared org-wide, not multiplied; in-region cross-account transfer is negligible). Real cost is the **cross-account IAM plumbing** (ECR pull policy, cross-account CodeDeploy role, per-account bootstrap) and a **one-time re-lay** of the disposable estate into the member accounts.
- *Guardrail:* keep the management account clean (no workloads ever); `prevent_destroy` on accounts; secure the management root; the split is **prod-first** — a 3rd account (staging alone) is a later step only if staging+platform coupling bites.

**Crossing the seam — proven, plus the gotchas it surfaced (2026-07-03).** The estate was re-laid into the two member accounts and the crux (decision 4) was validated end-to-end.
- **Cross-account promote-by-digest: PROVEN.** A CodeDeploy blue/green in `watch-prod` — driven by the nonprod pipeline's identity assuming the `watch-prod-deploy` role — registered a digest-pinned task def and shifted traffic to a green task set that **pulled the image from the nonprod ECR across the account boundary** (ECR repo policy + prod exec role). ADR-017 is real across accounts, not theoretical.
- **New-account activation holds (external, expected).** Fresh Organizations member accounts sit under a fraud-prevention hold that **blocks CloudFront** ("account must be verified") and pins the **CodeBuild** account-level concurrent-build limit to **0** ("Cannot have more than 0 builds in queue") *even though per-compute Service Quotas read 10*. Not a Service-Quotas knob — lifted only by an **AWS Support "Account & billing" case** (free on Basic) per account. Plan for it: a day-old account can stand up its estate but can't run CodeBuild or create CloudFront until verified. Draft: `platform/docs/architecture/support-case-newaccount-activation.md`. **The holds are NON-DETERMINISTIC per account** — which subset an account draws is an independent AWS risk decision, *not* a property of the env or our config: here nonprod drew the CodeBuild hold but **not** CloudFront, while prod drew CloudFront. Sibling accounts created together can differ; don't assume parity, and don't chase a config fix (both frontends use the same module — the only difference is the account).
- **Member-account console access needs an IAM identity.** The **root user cannot switch roles**, so you can't reach a member account's console as management-root. Create an IAM admin user in management (or use IAM Identity Center), then switch-role into `OrganizationAccountAccessRole` (`https://signin.aws.amazon.com/switchrole?account=<member>&roleName=OrganizationAccountAccessRole`). CLI cross-account uses the same role via `scripts/lib/xacct.sh` (assume from base management creds — reset base creds before each hop; the assumed creds can't read the management state bucket, so run `terragrunt output` first, assume after).
- **IAM role-name collisions across composed modules.** `modules/prod-deploy` composes `codedeploy` (service role) + `xacct-deploy-role` (the cross-account assume role); both derived `watch-prod-deploy` and one 409'd. The cross-account role keeps the predictable name `watch-prod-deploy` (the pipeline references its ARN); the CodeDeploy **service** role is now `…-codedeploy`.
- **Refinement — decouple the API hostname from the status/CloudFront record.** `prod/dns` (the `watch.<domain>` → ALB record, depends only on `app`) is split from `prod/dns-status` (the `status.<domain>` → CloudFront record, depends on `frontend`), each record `count`-gated in `modules/dns-records` on its target being set (`moved` blocks adopt the pre-split staging state, no churn). So a CloudFront outage/hold blocks **only** the status page, never the API by name — validated: `watch.davestanton.com` served 200 with a valid cert while `status.davestanton.com` stayed parked on the held CloudFront. A new stack ⇒ `teardown.sh` updated (dependents-first).

---

## ADR-021 — Incident timeline: unified event stream + annotations, feeding RCA
**Status:** Accepted · *Refines ADR-011 (Comment); relates to ADR-007/010 (Transition), §8 AI triage*

**Context.** The incident detail view is where an escalation's whole story should live — human notes, escalation-engine narrative (auto-escalation / SLA / paging), and AI-assisted triage (§8, #17) — and it is the natural input to a root-cause writeup. Today `Transition` records state changes (authoritative, commit-Lambda-written, ADR-007/010) and a flat `Comment` records human notes; `_timeline` merges them. But `Comment` can't represent automated/AI entries (its `author` is a `User` FK, so a system note renders "unknown"), and nothing lets you **annotate a past event** ("this escalation shouldn't have fired", "root cause starts here") for RCA.

**Decision.**
1. **`Transition` stays unchanged** — the authoritative, provable, sole-Lambda-written state-change audit (ADR-007/010). This change does not weaken it.
2. **`TimelineEvent`** replaces `Comment`: `incident`, `type ∈ {note, system, ai}`, `actor` (username / `system:…` / `argus`), `body`, `data` (JSON), `occurred_at`. `note` = human message; `system` = escalation-engine narrative; `ai` = AI-assisted triage findings. Existing `Comment`s migrate to `TimelineEvent(type=note)`; `Comment` is dropped.
3. **`Annotation`** — a human note/tag attached to **any** event via a **`GenericForeignKey`** (targets a `Transition` *or* a `TimelineEvent`): `author`, `body`, `tag ∈ {note, unexpected, root-cause, contributing}`, `created_at`. Annotating is orthogonal to the event's own authorship, so any event — including an authoritative `Transition` — can be marked up for RCA **without touching the escalation write-path**.
4. **Timeline** = merge(`Transition`s, `TimelineEvent`s) ordered by `occurred_at`, each carrying its annotations; rendered on `/ui/` (extends `_timeline`). Adding a note, and tagging any event, are HTMX actions on the `#incident-body` partial.
5. **Emission:** auto-escalation writes a `TimelineEvent(type=system)` (SLA/paging narrative) alongside its `Transition`; a service (+ API) posts `TimelineEvent(type=ai)` — the hook for AI-assisted triage (#17).
6. **RCA:** an assembly renders the full annotated timeline to structured **Markdown** (download/copy on the detail page) — the clean, reviewable RCA input. An **AI-drafted RCA** (Anthropic API, behind `flags.is_enabled`, ADR-003) is the immediate follow-up, consuming the same assembly.

**Consequences.**
- *Gain:* one incident-history surface for human/engine/AI entries; any event is annotatable for RCA; the RCA input falls out of the timeline for free.
- *Gain:* `Transition` keeps its integrity as the provable audit — annotations layer on via GFK, never mutating it or the sole-writer path.
- *Cost:* `GenericForeignKey` gives up DB-level FK integrity for annotation targets and adds a contenttypes lookup. Accepted for a low-volume, human-paced timeline (ADR-001) — annotation reads are per-incident and small.
- *Cost:* two concepts (`TimelineEvent` + `Annotation`) vs one flat `Comment`. Accepted — they answer different questions (*what happened* vs *commentary on what happened*).
- *Guardrail:* UI/API writes go through `incidents.services` + tier-or-above permissions (ADR-001/008); the AI-drafted RCA follow-up is flag-gated with both branches tested (ADR-003); new code is held to ≥90% coverage + green Sonar.

---

## ADR-022 — Session Check: on-demand error-span lookup by session / user
**Status:** Accepted · *Realizes #31; rides the event webhook #29; relates to the observability plane*

**Context.** A partner (or a user) reports "something's off with these sessions"; today that means manually pulling traces per session and eyeballing for error spans. This is the **inverse of an incident** — *go look for problems* rather than *a human declared one* — and a natural dogfood of our webhooks + observability. The make-or-break is being able to **query traces by an id we control**, which requires ids on the spans and a queryable trace backend.

**Decision.**
1. **Span tagging (the prerequisite).** A middleware stamps two attributes on the active span:
   - `session.id` = a **non-secret per-session correlation UUID** (minted + stored in the Django session). **Never the session auth key** — that's a credential and must not be displayed/shared. This id is safe to show, copy, and paste into a ticket.
   - `session.user` = **keyed** `HMAC-SHA256(SESSION_USER_HMAC_KEY, <reporter-facing user/customer id>)`, truncated. Keyed ⇒ not brute-force reversible; the key is a **stable, non-rotated** per-env secret (rotation orphans emitted spans).
   Both must be searchable in the trace backend (Tempo TraceQL).
2. **Self-serve the id.** The `/ui/` header shows the logged-in user their `session.id` (copyable), so a real user can report it — session-id is the **primary** lookup; `session.user` is the fallback when another system has only the user.
3. **`SessionCheck` + `ErrorSpan`.** A `SessionCheck` (subject_kind {session|user}, subject_hash, window, source {partner|e2e|manual}, status {queued|running|done|indeterminate}, verdict {clean|errors_found|aged_out}) yields zero-or-more `ErrorSpan`s (trace_id, span_id, name, service, status, http_status, ts). Parallels Incident/Transition. Only **hashes** are persisted — no plaintext PII in Watch's DB.
4. **Trace-store seam.** `trace_store.find_error_spans(subject, window)` with a **provider** (`tempo` via TraceQL now; `none` no-op; vendor/Grafana-Cloud deferred), swappable like `flags`. Hermetic tests inject a fake provider.
5. **Flow + local mode.** Inbound webhook (token auth, shared-secret like intake) → create a `SessionCheck` → **run synchronously in local mode** (`CHECKS_LOCAL_MODE`, default on) via `incidents.services`; in the cloud it enqueues to SQS and a worker calls the same service (the ADR-010 "one decision implementation" discipline). "Error span" = OTel span `status = ERROR` for v1.
6. **Trust + E2E dogfood.** Tail-sampling **always keeps error spans**, so a **zero-error result is trustworthy**; a session past trace **retention** returns `aged_out`, never a false `clean`. On a passing E2E, fire an **outbound** webhook (the correlation id), **fire-and-forget**, that returns **inbound** as a `source=e2e` check — giving error-span coverage of what the test exercised *and* a live proof the outbound→inbound webhook path works (local + staging).

**Consequences.**
- *Gain:* the partner-report workflow is one webhook; error spans on a "green" E2E become visible without failing tests; the webhook round-trip self-tests both directions.
- *Gain:* one decision implementation (services) shared by the local sync path and the cloud worker; trace backend is swappable per env behind the seam.
- *Cost:* the span-tagging middleware + a stable HMAC key are a hard prerequisite; high-cardinality `session.id`/`session.user` must be indexed for TraceQL search.
- *Cost:* v1 defers the SQS/worker + the vendor (Grafana Cloud) trace store — the local synchronous path proves the domain first.
- *Guardrail:* only hashes stored (no PII); webhook is M2M shared-secret (not a user session, ADR-008); new code held to ≥90% coverage + green Sonar; the outbound E2E webhook never blocks the test.

---

## ADR-023 — Outbound event webhook: HMAC-signed server-to-server event delivery
**Status:** Accepted · *Realizes #29; the outbound counterpart of intake (ADR-009); unblocks the Session Check cloud path + E2E dogfood (ADR-022)*

**Context.** Watch needs to **push** domain events to other systems — the reusable primitive behind the Session Check E2E dogfood, ChatOps, external dashboards, and future integrations. It is the outbound counterpart to the inbound intake webhook (ADR-009): same M2M discipline, opposite direction. A browser can't be a target (that's SSE, #28) — this is server↔server.

**Decision.**
1. **`WebhookSubscription`** (url, secret, `event_types` filter — empty = all, active) registers a receiver. **`WebhookDelivery`** (subscription, event_type, event_id, payload, status {pending|delivered|failed}, status_code, attempts, error) is the per-attempt audit — the outbound mirror of the intake trail.
2. **`events.emit(event_type, payload)`** is the single fan-out: it builds a canonical envelope `{event, id, at, data}`, creates a `WebhookDelivery` for each matching active subscription, and — in **local mode** (`WEBHOOKS_LOCAL_MODE`, default on) — POSTs synchronously; the cloud path records `pending` and enqueues to SQS for a worker (deferred, like ADR-022). Emission is **fully guarded — it never raises into the domain**, so a bad subscriber can never roll back an escalation.
3. **Signing.** Each POST carries `X-Watch-Signature: sha256=HMAC(secret, raw_body)` + `X-Watch-Event` — receivers verify authenticity + integrity (the GitHub/Stripe pattern), the outbound analogue of intake's shared secret.
4. **Emitted from `services`** (ADR-010 "one decision implementation"), so the same events fire whether a human or the auto-escalation Lambda drove the change: `incident.created`, `incident.escalated`, `incident.resolved`, `check.completed`. `event_id` gives receivers at-least-once dedupe (ADR-009 discipline).
5. **Surface.** Admin + a thin `/ui/webhooks` (subscriptions + recent deliveries) to register receivers and see the delivery log.

**Consequences.**
- *Gain:* one push primitive serves the Session Check dogfood/health-check, ChatOps, and integrations; delivery is auditable per attempt; signing gives receivers real verification.
- *Gain:* emitting from `services` means auto-escalation events fire too, not just human-driven ones.
- *Cost:* local-mode delivery POSTs synchronously in the domain path (a documented dev caveat; cloud enqueues). v1 defers the SQS worker, retry/backoff, and a delivery-replay UI.
- *Guardrail:* emission never raises into the domain (guarded) so it can't roll back state; secrets are per-subscription and never logged; new code held to ≥90% coverage + green Sonar.

---

## ADR-024 — Status page live updates via SSE (server→browser push)
**Status:** Accepted · *Realizes #28; refines ADR-005/011; the server→browser complement of the server↔server webhook (ADR-023)*

**Context.** The status SPA polls `/api/status` every 10s — wasteful and laggy. We want near-real-time posture. A static SPA on CloudFront can't be a webhook target (that's server↔server, ADR-023); pushing to the **browser** means a client-held connection to the **API origin**. Of SSE / WebSocket / smarter-polling, **SSE** fits a one-way read-only feed with the least infrastructure.

**Decision.**
1. **`GET /api/status/stream`** returns `text/event-stream`: it emits the current posture immediately, then re-checks each `STATUS_STREAM_POLL_SECONDS` and sends a `status` event on change (a `:keepalive` comment otherwise). It **recycles after `STATUS_STREAM_MAX_SECONDS`** (the `EventSource` auto-reconnects) so no worker is pinned forever. `Cache-Control: no-cache` + `X-Accel-Buffering: no` so CloudFront/proxies don't buffer the stream.
2. **Posture is factored** into `health.status_posture()`, shared by the `/api/status` snapshot and the stream — one source of truth.
3. **Point `EventSource` at the API origin** (ALB / `watch.<domain>`), never CloudFront (which buffers streams) — the SPA already fetches `/api/status` cross-origin, so it's the same CORS pattern (`STATUS_PAGE_CORS_ORIGIN`).
4. **The SPA prefers SSE**, falling back to polling only where `EventSource` is unavailable; `EventSource` auto-reconnects on drop. **Honest degradation (ADR-005) preserved:** on disconnect the page keeps the last-known posture and flags "unreachable"/stale until it reconnects.

**Consequences.**
- *Gain:* near-real-time posture over **one** long-lived connection instead of a poll every 10s; the change is contained (one endpoint + a factored posture fn + an SPA effect).
- *Cost:* an SSE holds one worker/connection per viewer — fine at the status page's low viewer counts; the recycle bound + keepalives cap the exposure. Django sync workers limit concurrency (a documented caveat; an async worker model is the scale path).
- *Cost:* `time.sleep`-driven server-side polling is coarse (v1); true push (Valkey pub/sub on transition writes) is the later refinement.
- *Guardrail:* the stream is bounded (recycles) + testable (generator parameterized by iterations); public read-only aggregate counts only (ADR-005); new code held to ≥90% coverage + green Sonar.

---

## ADR-025 — Realize the async cloud path: SQS + an ECS worker (same image, different command)
**Status:** Accepted · *Realizes #32; realizes the deferral in ADR-022 (Session Check) and ADR-023 (event webhook); mirrors ADR-010 discipline*

**Context.** ADR-022 and ADR-023 both run their work synchronously in the request under `CHECKS_LOCAL_MODE`/`WEBHOOKS_LOCAL_MODE` (default on) and *defer the cloud path to "an SQS worker"*. In cloud mode the durable row is already created — `SessionCheck=queued`, `WebhookDelivery=pending` — but nothing consumes it, so the work strands. We want the real async path: enqueue on write, a worker drains it, retries survive a crash, poison messages land in a DLQ. Two knobs already gate the split; we need the queue + the consumer behind them.

**Decision.**
1. **A `queue` seam** (`incidents/queue.py`), same provider discipline as `flags`/`trace_store`: `enqueue(kind, id)` dispatches to a provider — `local` (no-op; local mode never enqueues) or `sqs` (`boto3` `send_message` of `{"kind","id"}` to `WATCH_QUEUE_URL`). `set_provider_for_tests` for hermetic both-branch tests. The domain never imports boto3 directly (ADR-003 spirit).
2. **Enqueue on write, in cloud mode only.** `checks.create_and_run` and `events._deliver` already branch on their `*_LOCAL_MODE`; the cloud branch now calls `queue.enqueue("check", check.id)` / `queue.enqueue("delivery", delivery.id)` after the row commits. Emission stays guarded — enqueue failure is logged, never raised into the domain.
3. **The worker is the same image, a different command** — `manage.py run_sqs_worker` (build-once/promote-by-digest; no second artifact). It long-polls SQS, and for each message dispatches by `kind` to the **one services implementation** (ADR-010): `check` → `checks.run_session_check(SessionCheck.objects.get(id))`; `delivery` → `events.redeliver(WebhookDelivery.objects.get(id))`. Success → `DeleteMessage`; an exception → the message returns after the visibility timeout; after `maxReceiveCount` the queue redrives it to a **DLQ**. Idempotent by construction (`run_session_check` clears+recomputes; delivery keys on `event_id`), so at-least-once redelivery is safe.
4. **Infra (platform):** an SQS queue + DLQ per env and a **worker ECS service** — it reuses the app's `container_definitions` with the app container's `command` overridden to `run_sqs_worker` and `portMappings=[]` (**no ALB/target group**, plain ECS rolling deploys, `desired_count` low). Task-role IAM is least-privilege and split: the **app** role gets `sqs:SendMessage`; a separate **worker** role gets `sqs:ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` **plus `SendMessage`** (a completed check emits `check.completed`, which itself enqueues a delivery). All of it lives **inside `modules/app`, gated on `enable_worker`** (default false → prod untouched: no queue, no worker, no new IAM) rather than a standalone stack — so it shares the app's create/destroy lifecycle (no `teardown.sh` `ENV_STACKS` surgery) and the worker task-def reuses the module's `container_definitions` local directly. The three new task-def secrets (`SESSION_USER_HMAC_KEY`, `CHECKS_WEBHOOK_SECRET`, `WEBHOOK_ECHO_SECRET`) are generated in the config stack (`secrets-appconfig`) and injected by ARN via the `secrets` block, same discipline as the Django/intake secrets. *(Deviation from the first draft, which proposed separate `app/queue`+`app/worker` Terragrunt stacks — folded into the app module for lifecycle+reuse.)*
5. **Flip staging to cloud mode** to exercise it for real: `CHECKS_LOCAL_MODE=0`, `WEBHOOKS_LOCAL_MODE=0`, `TRACE_STORE_PROVIDER=tempo`, `WATCH_QUEUE_URL=…`. Prod follows once staging is proven.

**Consequences.**
- *Gain:* the request returns immediately; slow trace queries and partner POSTs move off the hot path; a worker crash loses nothing (the row + the SQS message both survive); poison messages isolate in the DLQ instead of wedging the queue.
- *Gain:* one worker drains **both** kinds — Session Check and webhook delivery share the queue + consumer; the same image runs API and worker, so a promoted digest updates both.
- *Cost:* a standing worker service (min 1 task) + a queue to operate; at-least-once means consumers must be idempotent (they are). Retry/backoff is SQS-native (visibility timeout + redrive), not app-tuned — a documented v1 caveat.
- *Guardrail:* least-privilege split roles (send vs receive), DLQ bounds blast radius, the seam keeps boto3 out of the domain, both provider branches unit-tested; new code held to ≥90% coverage + green Sonar.

---

## ADR-026 — Multi-vendor trace store for Session Check (own telemetry + query-only adapters)
**Status:** Accepted · *Realizes the deferral in ADR-022; refines the `trace_store` seam*

**Context.** ADR-022's Session Check queries a trace backend for a session's error spans, and left the vendor impl deferred (only `none` + in-VPC `tempo`). Two distinct needs surfaced: **(a)** query Watch's **own** prod telemetry — which goes to **Grafana Cloud** (ADR-016 §2), not an in-VPC Tempo; and **(b)** use Watch as an **SRE tool against *existing* telemetry at work** — **Datadog** and **Sumo Logic** — where Watch does **not** own ingest and the traces are put there by unrelated systems.

**Decision.**
1. **One provider per backend behind the existing `find_error_spans(subject_kind, subject_hash, window)` seam** — `TRACE_STORE_PROVIDER` ∈ `none | tempo | grafana_cloud | datadog | sumologic`. Each provider builds a vendor query for "spans where the subject attribute = the hash **and** status = error, in the window", calls the vendor API, and **normalizes to the same span dict** (`trace_id, span_id, name, service, status, http_status, ts`) that `ErrorSpan` stores. A backend that can't answer raises `TraceStoreError` → the check is **indeterminate**, never a false `clean`.
2. **The queries are read-only adapters; Watch owns ingest only for its own telemetry.** `tempo`/`grafana_cloud` are Watch's own spans (session-tagged by the middleware, ADR-022). `datadog`/`sumologic` point at **work** telemetry — **no gateway/export/ingest work here, and their ingest cost is not ours**. All Session Check does is issue queries.
3. **Per-vendor query APIs, shared normalization:**
   - `grafana_cloud` = **Tempo** (identical TraceQL `{ span.session.id = "…" && status = error }` + response as `tempo`), just **HTTPS + HTTP basic auth** (user = Tempo instance id, password = an access-policy token). It subclasses the Tempo provider.
   - `datadog` = **APM v2 spans search** (`POST /api/v2/spans/events/search`), query `@session.id:<hash> status:error`, `DD-API-KEY` + `DD-APPLICATION-KEY` headers. Only **indexed** spans (retention filters) are searchable — Datadog's analogue of our tail-sampling keep-policy.
   - `sumologic` = **async Search Job API** (create job → poll to *DONE GATHERING RESULTS* → fetch messages), basic auth (accessId:accessKey), region endpoint.
4. **Auth is config, secrets via SSM in prod** (tokens/keys never in code/state), same discipline as the other app secrets. Querying is **not per-query billed** anywhere; ingest/retention/indexing is — so Session Check's read pattern is cheap, and the only cost lever is *what's kept* (our tail-sampling for own telemetry; the work systems' retention for theirs).

**Consequences.**
- *Gain:* Session Check works against Watch's own prod telemetry (Grafana Cloud) **and** as an SRE tool over existing Datadog/Sumo telemetry, all behind one seam, one normalized result, provider chosen by config.
- *Cost / caveat:* the providers are unit-tested (query construction, auth, response parsing — all mocked), but **live-validated only against a real account**. Two documented assumptions to confirm before prod use: exact **Datadog span attribute field names** (`resource_name`, `custom.http.status_code`, `start_timestamp`) and the **Sumo tracing query dialect** (`_view=spans`, span field names) — both isolated to their provider + kept configurable.
- *Guardrail:* a backend error is always `indeterminate` (never a false `clean`); secrets via SSM, never logged; new code held to ≥90% coverage + green Sonar.

## ADR-027 — Public self-service report surface on the status page (unauthenticated intake + Session Check)
**Status:** Accepted · *Refines ADR-011 (status page) and ADR-008 (auth boundary)*

**Context.** The status page (ADR-011) is public and read-only. We want a visitor experiencing a problem to *act* from it: **report an incident**, or **submit their own session id for a trace check** — with **no sign-in**. But every existing write path is gated: intake and the Session Check webhook are **machine-to-machine, shared-secret** (ADR-008/009/022), and the `/ui/` + action APIs are session-authenticated (ADR-008). A public form can't carry the M2M secret (embedding it in the SPA would leak it), so this needs a *new* class of endpoint: **anonymous writes**. That's a genuine new decision — it opens the first un-authenticated write surface in the system — so it goes through the ADR trail rather than being bolted onto the secret-gated webhooks.

**Decision.**
1. **Two new `AllowAny`, secret-less endpoints, distinct from the M2M webhooks:** `POST /api/report/incident` and `POST /api/report/check`. They **reuse the same services** as the authenticated paths — `create_incident_idempotent` (ADR-009 dedupe + one escalation execution, ADR-001/007) and `checks.create_and_run` (ADR-022) — so a public report is a *real* incident / a *real* check, not a second-class shadow. Source is stamped server-side: incidents get `source="status-page"`, checks get `CheckSource.SELF_REPORT` — the origin is never client-trusted.
2. **The abuse surface is bounded without auth**, since there's no secret to bound it:
   - **Per-IP throttle** (`SimpleRateThrottle`, scope `public_report`, `DEFAULT_THROTTLE_RATES` — `10/min` default, env-tunable via `PUBLIC_REPORT_THROTTLE` so a noisy source can be clamped without a deploy). CORS **preflights return no cache key** and don't spend the budget.
   - **Tight input validation**: incident `title` ≤200 / `detail` ≤2000 chars; the check `session` must match `^[0-9a-f]{32}$` (a non-secret uuid4 hex correlation id, ADR-022) — so the anonymous path can only enqueue **well-formed** work. The body is stored as the incident payload, never trusted/executed.
3. **No verdict leaks to the anonymous submitter.** The check endpoint returns only `{id, status}` — the error-span verdict is for the on-call, not the world. Incident reports return `{id, created}` (dedupe is honest: a repeat while OPEN is a `200` no-op, ADR-009).
4. **CORS for the write path mirrors the read path** (ADR-011): a small mixin answers the JSON-POST preflight and stamps `Access-Control-Allow-Origin = STATUS_PAGE_CORS_ORIGIN` (single origin in prod, `*` locally), `Vary: Origin`, no credentials. The endpoints use `authentication_classes = []` (no session, so no CSRF), exactly like the webhooks.

**Consequences.**
- *Gain:* the status page becomes a two-way surface — a visitor can raise an incident or self-submit a session for a trace check in one click, no account — while every downstream invariant (idempotent intake, one escalation execution, paging on tier entry, Session Check semantics) is inherited unchanged.
- *Cost / caveat:* this is the system's **first anonymous write path**; the throttle + validation are the whole defense. If abused, the lever is `PUBLIC_REPORT_THROTTLE` (or a WAF rule / CAPTCHA later) — noted, not built. A public reporter can create incidents that page the on-call, which is the intended behavior but also the abuse vector; the per-IP cap keeps a single source from flooding the rota.
- *Guardrail:* source is server-stamped (never client-trusted); verdicts aren't exposed to anonymous callers; held to ≥90% coverage + green Sonar; both throttle branches (allowed / `429`) and the preflight-skip are tested.

## ADR-028 — Per-environment ops status + digests: schema-less ingest, authenticated display
**Status:** Accepted · *Migrates the useful half of the `hermit-watch-gen` precursor*

**Context.** A precursor project (`hermit-watch-gen`) runs an external AI-SRE agent that, per environment, produces (a) a rich **ops status** object and (b) periodic **health digests** (markdown, pasted into Slack), and serves them behind a token page. We want that **ops-facing detail in Watch** — richer than the public posture (ADR-011) — but **Watch is only the store + display**: the existing tooling keeps generating the content and just re-points its POSTs at Watch. So Watch adds no AI, no triage, no scheduler, no "service" model. Two things make this unlike the rest of Watch's API: the status has **no rigid schema** (the posted JSON itself defines the groupings), and everything is keyed by **environment** (`prod`/`nonprod` today).

**Decision.**
1. **Two tiny models, both keyed by a free `environment` string** (validated `^[a-z0-9-]+$`; `prod`/`nonprod` are seeded/used now, a new label just works — no migration):
   - `EnvStatus { environment, payload: JSONField, created_at }` — `payload` is stored **verbatim, unvalidated** (opaque JSON). **Keep history**; the newest row per env is "current".
   - `Digest { environment, content: TextField (markdown), title, special: bool, created_at }` — `special` (the "speci" flag) marks a digest published **ad-hoc during an incident** vs the routine scheduled ones (default `false`). History per env, browsable.
2. **M2M ingest, env in the path** (mirrors the intake webhook's machine auth, ADR-008 — a shared secret `OPS_INGEST_SECRET` in `X-Watch-Ops-Secret`, never a human session):
   - `POST /api/environments/{env}/status` — body is arbitrary JSON, stored as `payload`.
   - `POST /api/environments/{env}/digest` — `{content, title?, special?}`.
   - Reads for the display: `GET .../status` (latest), `.../statuses` (history), `.../digests` (history, filterable by `special`).
3. **Schema-less display via a generic renderer.** The `/ui` (session-auth, ADR-008) shows an environment switcher → the current status rendered by **walking the arbitrary JSON** (objects → titled groups, arrays → item lists, scalars → key/value; light, optional heuristics: a `state`/`severity` value → coloured badge, a URL → link) — **never** assuming a `services`-shaped schema — plus the browsable digest pane (rendered markdown, a **Copy for Slack** button, a SPECIAL/ROUTINE badge). Any authenticated user may view.

**Consequences.**
- *Gain:* the ops team gets the detailed per-env status + Slack-ready digests inside Watch (one auth, one place), with the precursor tooling unchanged bar its endpoint URLs. The renderer is schema-agnostic, so the tooling can restructure its groupings freely without a Watch change.
- *Cost / caveat:* storing opaque JSON means Watch can't reason about the status (no validation, no querying inside `payload`) — deliberate; Watch is a dumb store here. `keep-history` grows unbounded; a retention trim is a later chore (noted, not built). The renderer must stay defensive (arbitrary depth/types, missing keys).
- *Guardrail:* ingest is server-secret-gated (never client-session), env is validated, payload never executed; display is session-auth'd; held to ≥90% coverage + green Sonar; the generic renderer is unit-tested against several unrelated JSON shapes so "no rigid schema" is real.
