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

`WT_AS` and `WT_SERVER` set the defaults. `--server` takes `local` (default, :8010) or a URL. For
another server, copy a user's MCP key from `/ui/settings` and `wt --server URL key set <user> <key>`,
and set `WT_WEBHOOK_SECRET` for intake.

Exit codes: 0 ok, 1 the server refused (the reason is printed), 2 `wait` timed out.

## Develop

```bash
make cli-test
```
