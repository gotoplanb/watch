# wt — Watch from the terminal

A development CLI for Watch (ADR-056). It drives the server's `/api/` as any user, so verifying a
feature is a few commands instead of `curl` with hand-built headers or a browser session.
**It talks to the server, never to its database.**

The order we build in: **API → `wt` → browser → MCP subset.** A feature is drivable here first,
then checked in the browser, then (only if an assistant should have it) exposed over MCP.

## Install

```bash
make cli-install    # uv tool install -e ./cli: wt on your PATH, edits take effect at once
make cli-keys       # write every local user's wm_ key to ~/.config/wt/keys.json (mode 600)
```

Standard library only, Python 3.12+. `make dev` must be up. Rerun `make cli-keys` after reseeding
or rotating a user's keys.

## Use

```bash
wt users                                   # who wt can act as on this server
wt --as t1a whoami
wt intake "cpu high on web-2" --event-id e1   # fire the alert webhook (no user needed)
wt --as t1a incidents                      # open incidents; --tier T2, --status ALL
wt --as t1a ack INC-0042
wt --as t1a escalate INC-0042 --expect T1 --reason "needs T2"
wt --as t1a resolve INC-0042               # at T2 this is refused: tier authz is the server's
wt --as t2a wait INC-0042 --tier T3 --timeout 120
wt --as t2a show INC-0042                  # detail + transitions
wt assess "p99 latency" --signal "p99 > 2s" --customer "Example Gateway"
wt signal "p99 latency" --signal "p99 > 2s"   # through the routing policies
wt --as t1a assessments --status investigating
wt --as t1a show ASM-0007                  # assessment + its history
wt --as admin api incidents/?current_tier=T3
wt --as t1a api -X POST incidents/INC-0042/ack/ -d '{"reason": "x"}'
wt --as t1a --json show INC-0042           # the server's reply, raw
```

### The assessment workspace

Everything the workspace page does (ADR-054/055). `wt do` runs any of its actions with `key=value`
fields. Values parse as JSON when they can (`rule=2`, `auto_send=true`, `selected=[341,342]`),
`key=@file` uploads, and a repeated key makes a list. Fields you leave out get what the page
pre-fills: review windows, the drafted email text, a select's first option.

```bash
export WT_AS=t1a
wt new "payment timeouts" --signal "timeouts on /pay" --customer "Example Gateway"
wt note ASM-0025 "grafana: https://…" --photo shot.png
wt do ASM-0025 evidence summary="timeouts 6%" source=prometheus result="6% for 15m"
wt do ASM-0025 context severity=P2 impact=moderate reason="checkout affected"
wt do ASM-0025 conclusion verdict=degraded confidence=high reason="sustained"
wt do ASM-0025 query rule=2                 # `wt rules` lists saved checks
wt do ASM-0025 email recipients=ops@example.com review_seconds=600
wt comms ASM-0025 ; wt comm <id> ; wt comm <id> edit subject=… body=… recipients=…
wt comm <id> send                           # or suppress reason=…; pinned to the current version
wt do ASM-0025 promote reason="customer impact"
wt do ASM-0025 engage area=1 reason="processor expertise"   # or target=t3a; `wt areas`
wt --as t3a sme-respond <id> ack ; wt --as t3a sme-respond <id> complete --body "failover"
wt show ASM-0025                            # history with event ids (#342) for curation
wt do ASM-0025 curate event=342 section=root_cause note="…"
wt do ASM-0025 rca summary=… root_cause=… state=final
wt do ASM-0025 remediation title="Autoscale pool" owner=t2a due_date=2026-12-01
wt --as t2a remediation <id> state=done verification="verified"
wt rca ASM-0025 ; wt outbox ; wt report --days 7
```

Actions: comment, evidence, conclusion, monitor, no_issue, promote, email, operation, attachment,
query, engage, curate, rca, remediation, context, claim, retry_automation, retry_engine.

`WT_AS` and `WT_SERVER` set the defaults. `--server` takes `local` (default, :8010) or a URL. For
another server, copy a user's MCP key from `/ui/settings` and `wt --server URL key set <user> <key>`,
and set `WT_WEBHOOK_SECRET` for intake.

Exit codes: 0 ok, 1 the server refused (the reason is printed), 2 `wait` timed out.

## Develop

```bash
make cli-test
```
