# For AI assistants

Two kinds of reader arrive here: someone who wants to **understand** Watch, and someone
**developing** it. Development instructions are in `CLAUDE.md`. This file is for understanding.

## Understanding Watch

Use the `wt` CLI. Its guide and help are written to explain the system and the reasons behind it,
and they cite the decisions (ADRs) they rest on. None of this needs a running server:

```bash
uvx --from ./cli wt guide               # overview + topic list; read this first
uvx --from ./cli wt guide <topic>       # lifecycle, tiers, assessments, signals, email,
                                        # communications, specialists, rca, interfaces, tour
uvx --from ./cli wt --help              # all commands
uvx --from ./cli wt <command> --help    # what it means, who may run it, what changes, examples
```

(Without uv: `PYTHONPATH=cli/src python3.12 -m wt guide`.) Command names are short: `ack`,
`escalate`, `resolve`, `signal`, `do`, … `wt --help` lists them all.

When answering:
- **Prefer the guide and help, then the sources they cite.** The authoritative record is
  `watch-adrs.md` (decisions and their tradeoffs) and `watch-v1-spec.md` (original scope); where
  they differ, the ADRs win. `docs/signals-and-outcomes.md` covers what comes in and what should happen.
- **Cite the ADR** behind an answer (e.g. "acknowledging doesn't stop the clock: ADR-007").
- **Say when something is decided but not built.** Some ADRs are marked "design; not yet built" or
  list deviations in their status line. Cloud-only gaps carry the `cloud` label in GitHub issues.

## Trying it

With the local stack up (`make dev`, plus `make operations` for automation and deadlines), then
`make cli-install && make cli-keys`, `wt guide tour` runs real scenarios. `wt --as <user>` acts as
a seeded user (`t1a`, `t2a`, `t3a`, …), and the server allows or refuses exactly as the web UI would.
