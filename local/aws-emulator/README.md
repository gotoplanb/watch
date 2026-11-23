# AWS Emulator (opt-in, ADR-067)

AWS's local emulator of its APIs, kept here as an **opt-in** extra, not part of `make dev`, the
pre-commit gates or `make integration`. It's new, it ships several releases a day, and it is
expected to grow; this folder is the stub we re-check it with.

```bash
make emulator-up        # build the pinned image if needed, start it on 127.0.0.1:9297
make emulator-test      # integration tests marked `emulator` (today: the SQS worker path)
make emulator-probe     # can it run Watch's escalation Lambdas yet?
make emulator-down      # stop it (state is in memory only, so this wipes it)
source local/aws-emulator/env.sh   # point aws/boto3 in your shell at it
```

## What it's useful for today (2026-11-23, v1.0.587)
- **SQS**, from the host: the worker's queue path (`QUEUE_PROVIDER=sqs`, `run_sqs_worker`), which
  otherwise only runs in AWS (watch#61). `test_sqs_worker_emulator.py` covers send → receive →
  delete, and a failing job redriving to a dead-letter queue.
- **Step Functions**, from the host: task tokens, `TimeoutSeconds` in real time, and execution-role
  trust are all faithful.

## What it can't do yet
- **Run Watch's Lambdas.** Python functions run in an embedded RustPython: no compiled packages
  (psycopg), no TLS, and no network beyond the emulator. Our `record_token`/`commit` handlers are
  Django talking to Postgres, so Step Functions Local + `run_lambda_shim` stay. `make emulator-probe`
  says READY the day this changes.
- **Rehearse `make live`.** It has no Terraform fidelity tests, and CloudFormation passes ~27%.

## Upgrading
Bump `EMULATOR_VERSION` in the `Makefile` (versions:
`https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/versions.json`), then
`make emulator-build emulator-up emulator-test emulator-probe`. Telemetry is off in the image.
