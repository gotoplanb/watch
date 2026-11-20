# Tiers, on-call and paging

ADR-008 (authorization), ADR-012 (on-call), ADR-013 (paging), ADR-040/041 (handoff briefs).

## Who can act

Tiers are roles: T1, T2, T3. **To acknowledge, escalate or resolve an incident you need its
current tier or a higher one** ("senior override"):

| Incident is at | Who may act |
|---|---|
| T1 | T1, T2, T3 |
| T2 | T2, T3 |
| T3 | T3 |

It's role-based, not per-person: you don't have to be the assigned on-call. The server enforces
this on every action, from the UI, the API, `wt` or MCP alike. Try it:

    wt --as t1a escalate INC-0042     # at T1, T1 may act: allowed
    wt --as t1a resolve INC-0042      # now at T2, T1 may not: refused, 403
    wt --as t2a resolve INC-0042      # T2 may: resolved

## On-call

Who **may** act (tier roles) is separate from who **should** act (the on-call rota). Entering a
tier assigns the incident to that tier's current on-call person. A gap in the rota leaves it
unassigned but still actionable by anyone in the tier or above. Assigning grants no authority.

## Paging

Entering a tier (a new incident at T1, or an escalation) pages the on-call person; acknowledging does not page.
Locally pages go through ntfy. Deployed environments use Grafana IRM (ADR-066): Watch sends one alert per
incident and tier, IRM pages whoever its schedule has on call for that tier, and Watch withdraws the
alert when the incident leaves the tier. Watch still owns the clock; IRM only delivers. With nobody on call, the page goes to the whole tier. Paging is
best-effort, never blocks the escalation itself, and sits behind the `paging_enabled` flag
(ADR-003), which is off in local development.

## Handoffs

When an incident escalates, a brief is posted for the incoming responder: what has happened, why
this tier is engaged, and what to do now. The escalating person's stated reason is shown first.
Briefs sit behind the `handoff_brief` flag, which is on in local development.
