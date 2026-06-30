# Expand → Contract migration runbook (§4.9 / platform#12)

Zero-downtime schema change under blue/green (ADR-005/ADR-017). The rule: **every schema
change is backward-compatible with the currently-running code**, because during a deploy
**both** old (blue) and new (green) tasks run against the **same** database, and a rollback
must leave blue fully working. You get there by splitting a change into **expand** and
**contract**, shipped as **separate pipeline runs**.

## The phases

| Phase | What | When | Reversible? |
|---|---|---|---|
| **Expand** | Add the new shape (nullable column, new table, new index `CONCURRENTLY`). Never drop/rename/NOT-NULL yet. | Release N, `BeforeAllowTraffic` | Yes — blue ignores it |
| **Migrate** | The expand DDL runs as the `BeforeAllowTraffic` hook **before** traffic shifts to green. | Release N, automatically | — |
| **Backfill** | Populate the new shape for existing rows (batched, idempotent management command). Safe to run while both versions serve. | Release N (post-deploy) or N+½ | Yes |
| **Cut-over** | New code reads/writes the new shape; old shape kept in sync (dual-write if needed). | Release N code | Yes (revert code) |
| **Contract** | Drop the old column/table/dual-write once green is 100% and **SLOs are unchanged**. | **Release N+1 (separate run)** | No — point of no return |

**Contract is a separate, later release, gated on SLOs.** Don't drop the old shape in the
same deploy that adds the new one — if you roll back Release N, blue still needs the old
column. Only after Release N is fully rolled out, stable, and SLOs are unchanged do you
ship Release N+1 that contracts.

## How migrations run in the deploy

`BeforeAllowTraffic` (CodeDeploy ECS blue/green) invokes the deploy-hook Lambda
(`<env>-deploy-hook`, platform#12), which runs `manage.py migrate --noinput` as a one-off
Fargate task on the **green** task definition's image, in the app's private subnets. If it
exits non-zero the hook reports **Failed** and CodeDeploy **auto-rolls back before any
production traffic moves**. Because expand migrations are additive, blue keeps serving
correctly throughout.

## Worked example — rename `Incident.title` semantics to a `summary` column

1. **Release N — expand.**
   - Migration `0006_add_summary`: `ADD COLUMN summary varchar NULL`. (Additive; blue ignores it.)
   - Backfill command `backfill_summary`: copy `title` → `summary` in batches (idempotent).
   - New code **dual-writes** `summary` (and still writes `title`); reads prefer `summary`,
     fall back to `title`. Blue (still writing only `title`) remains correct.
   - Deploy: `BeforeAllowTraffic` runs `0006`, green comes up dual-writing, traffic shifts.
   - Run the backfill (post-deploy). Watch SLOs for a soak period.
2. **Release N+1 — contract** (separate pipeline run, only if SLOs unchanged):
   - Code stops writing `title`; reads `summary` only.
   - Migration `0007_drop_title`: `DROP COLUMN title`. Now irreversible — but green has been
     stable and 100% for a full release, so blue-with-`title` is no longer a rollback target.

## Checklist before shipping a migration

- [ ] Expand only — no `DROP`, `RENAME`, or `SET NOT NULL` on an existing column in the same release as new code that depends on it.
- [ ] New indexes use `CREATE INDEX CONCURRENTLY` (no long table lock).
- [ ] Backfill is **batched + idempotent** (re-runnable; bounded lock/IO).
- [ ] Code tolerates **both** shapes for the whole expand release (dual-write / read-fallback).
- [ ] Contract is a **separate PR + pipeline run**, gated on "green 100% + SLOs unchanged".
- [ ] `migrate` is fast (the `BeforeAllowTraffic` hook blocks the deploy); long data moves go in the **backfill**, not the migration.
