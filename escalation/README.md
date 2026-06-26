# Escalation engine (Step Functions)

The escalation timeline — **core domain value** (ADR-001). One Standard-workflow
execution per incident, started at creation; each tier is a `waitForTaskToken` task
with `timeout` = that tier's SLA.

## Files
- `statemachine.asl.json` — the ASL. Orchestration only; `${...}` placeholders
  (function ARNs, `sla_tN_seconds`) are rendered by the `escalation` Terragrunt
  stack from `TIER_SLA_SECONDS`.
- `lambdas/record_token.py` — persists the current tier's task token + SLA deadline
  to Postgres so the API can `SendTaskSuccess` (ADR-007). Stub.
- `lambdas/auto_escalate.py` — timeout handler; advances a tier with actor
  `system:auto-escalation`, same transition shape as a manual move (spec §3). Stub.

## Contract (ADR-007)
| Event | Path | Effect |
|---|---|---|
| Human ESCALATE | API `SendTaskSuccess(outcome=ESCALATE)` | Choice → next tier's wait |
| Human RESOLVE | API `SendTaskSuccess(outcome=RESOLVE)` | Choice → `Succeed` (clean terminal) |
| Human ACK | **no token call** — Postgres-only, timer keeps running | — |
| SLA timeout (T1/T2) | `States.Timeout` caught → auto_escalate | advance to next tier |
| SLA timeout (T3) | `States.Timeout` → `Fail` | failed execution → CloudWatch alarm |

The decision logic in both Lambdas is intentionally thin and idempotent ("act if
still applicable") so Step Functions retries are no-ops. In the real build the
Lambdas call the **same** `incidents.services.escalate/resolve` functions the API
uses, over a thin DB layer — one decision implementation, two callers.

## Local
Step Functions Local + Lambda containers via docker-compose (§5). Honest tax: it's
clunkier than a plain scheduler (ADR-001) — unit tests avoid it entirely and test
the decision functions directly (`backend/incidents/tests/test_transitions.py`).
