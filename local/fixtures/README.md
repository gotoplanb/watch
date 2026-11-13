# Local incident examples

Internal scenario JSON lives here and is ignored by Git. Keep source evidence in its
original repository; use summaries without customer identifiers, credentials, raw
request/session IDs, or employee details. Do not force-add internal fixture files.

Start Watch with `make dev`, then run `make seed-incidents` in another terminal.
The default file is `local/fixtures/incident-examples.json`. Override with
`make seed-incidents SCENARIO_FILE=/absolute/path/examples.json`.
Existing demo users must already exist (`make seed-dev`). No passwords are changed.

The importer creates explicitly labeled exercise incidents, chronological notes and
transitions, annotations, and linked Problem/RCA records. It imports snapshots
directly, with no paging, webhooks, model calls, synthetic telemetry, or Step Functions
executions. Both `DEBUG` and local escalation mode must be enabled. Recent exercise
timestamps are independent of the historical dates retained in `facts`.

Stable collection/key identifiers make reruns preserve existing records, including
operator edits and resolved examples. Change the collection to create another set.
Imports validate the entire file and run in one database transaction.

Version 1 JSON contains `collection` (lowercase identifier) and `scenarios`. Each
scenario has `key`, `title`, `summary`, `source_reference`, `facts`, `assignee`,
`steps`, `problem`, and `rca`. Steps contain a chronological integer `minute`,
`kind` (`note`, `handoff`, `ack`, `escalate`, `resolve`), demo-user `actor`, and
`body`; optional `annotations` contain `tag` and `body`. Actions obey tier roles
and lifecycle order. Problems contain `title`, `description`, and `status`; RCAs
contain `title`, Markdown `document`, and a non-final `status`. Classification,
impact, uncertainty, verification criteria, and follow-up ownership belong in the
authored timeline and document. Proposed response steps must be labeled as modeled,
and source facts must preserve uncertainty and counting caveats.
