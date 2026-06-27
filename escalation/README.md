# Escalation engine (Step Functions)

The escalation timeline — **core domain value** (ADR-001). One Standard-workflow
execution per incident; each tier is a `waitForTaskToken` with `timeout` = that tier's
SLA. **ASL orchestrates, Python decides** (ADR-001/007/010).

## Files
- `statemachine.asl.json` — the ASL. Per tier: `Tn_Wait` (waitForTaskToken →
  `record_token`) → `Tn_Choice` → a **commit** task. `${...function_arn}` are bound by
  the escalation Terragrunt stack; locally to `record_token` / `commit` (the shim).
- `lambdas/record_token.py` — persists the tier's task token + SLA deadline to Postgres
  (no transition). `lambdas/commit.py` — the **single writer**: calls the shared
  `incidents.services.escalate/resolve`. `lambdas/_bootstrap.py` — `django.setup()`.
- `test/MockConfigFile.json` — mocked Lambda integrations for the routing tests.

## Contract (ADR-007/010)
| Event | Path | Effect |
|---|---|---|
| Human ESCALATE | API `SendTaskSuccess({outcome:ESCALATE, actor})` | Choice → `Tn_EscalateCommit` → `services.escalate(actor)` → next tier |
| Human RESOLVE | API `SendTaskSuccess({outcome:RESOLVE, actor})` | Choice → `ResolveCommit` → `services.resolve(actor)` → `Succeed` |
| Human ACK | **no token call** — Postgres-only, clock keeps running | — |
| SLA timeout (T1/T2) | `States.Timeout` → `Tn_AutoCommit` | `services.escalate(actor=system:auto-escalation)` |
| SLA timeout (T3) | `States.Timeout` → `Fail` | failed execution → CloudWatch alarm |

The API writes no state in real mode — only the commit Lambda does, so manual and
automatic transitions share one idempotent path (ADR-001). Each `Tn_Wait` sets
`ResultPath: $.decision` so the task output never clobbers `$.incidentId`.

## Running the real engine locally (ADR-010)
This sandbox can't build Lambda images, so the handlers run on the host behind a Lambda
Invoke shim that Step Functions Local calls via `LAMBDA_ENDPOINT`:

```bash
docker compose --profile integration up -d stepfunctions-local   # LAMBDA_ENDPOINT preset
python manage.py run_lambda_shim                                  # host :9050, in a tmux window
python manage.py sfn_register                                     # creates the state machine
# then set ESCALATION_LOCAL_MODE=0, ESCALATION_ENDPOINT_URL, ESCALATION_STATE_MACHINE_ARN
```

`ESCALATION_LOCAL_MODE=1` (default) short-circuits to direct `services` calls — used by
the hermetic unit tests and the simple `make dev` loop. The real engine (timeout
auto-escalation + human round-trip) is covered by
`backend/incidents/tests/integration/test_escalation_e2e.py`.
