# Interfaces: API, wt, web UI, MCP

ADR-056 (wt), ADR-011 (web UI), ADR-038 (MCP), ADR-049 (webhooks in and out).

All of them go through the same server code and permission checks, so anything the UI allows or
refuses, `wt` and MCP allow or refuse identically.

| Interface | For | Notes |
|---|---|---|
| **HTTP API** (`/api/`) | Machines and `wt` | Sessions, or a per-user bearer key |
| **wt** (this CLI) | Developers, and explaining Watch | Acts as any seeded user: `--as t2a` |
| **Web UI** (`/ui/`) | Responders | Server-rendered; mobile-first; accessibility-checked |
| **MCP** | AI assistants | A curated subset of actions, e.g. acknowledge/escalate/resolve |
| **Webhooks** | Other systems | Signals in (shared secret); signed event notifications out |

## The build order

A feature is built **API first, verified with `wt`, then in the browser**, and only then (where an
assistant should have it) exposed over MCP. If `wt` can't drive something, that's a missing API,
not something to script around.

## Credentials

- People: a browser session, or their personal key (`wm_…`) as a bearer token for the API and
  MCP. One "rotate" in settings replaces all of a person's keys (ADR-030).
- Machines sending signals: a shared secret header. That secret grants intake only, never
  incident actions.
