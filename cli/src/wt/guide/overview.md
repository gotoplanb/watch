# Watch: overview

Watch is an incident intake and tiered-escalation platform. Signals come in from monitors, suppliers
and people. Watch decides whether customers are actually affected, and when they are, it runs an
incident through response tiers (T1 → T2 → T3) under timers, paging the people on call.

The design is recorded as numbered decisions (ADRs) in `watch-adrs.md`. The original scope is in
`watch-v1-spec.md`. The ADRs win where they differ.

## The shape of it

    signal ──► assessment ──► (impact confirmed) ──► incident at T1 ──► T2 ──► T3
                  │                                    │
                  └─ closed: no issue                  └─ resolved by a person, at any tier

1. **Signals are questions** (ADR-054). "Square is down" asks *are our customers affected?*
   Watch opens an **assessment** and answers it with our own telemetry before anyone is paged.
2. **Most noise stops there.** Blips, and supplier outages that didn't reach our customers,
   close as "no issue" with the evidence recorded. Only confirmed impact becomes an incident.
3. **An incident escalates on a clock** (ADR-001, ADR-007). Each tier has a deadline; if nobody
   resolves or escalates in time, it escalates automatically, whether or not anyone acknowledged
   it. Acknowledging does not stop the clock.
4. **Authority comes from tiers** (ADR-008). To act on an incident you need its current tier or a
   higher one: a T2 incident can be handled by T2 or T3 responders, not T1.
5. **Everything is history.** Every signal, check, note, decision and message is an append-only
   event, and the RCA (root-cause analysis) is curated from those events (ADR-055).

## Topics

    wt guide lifecycle       incidents: status, tiers, timers, acknowledge vs escalate vs resolve
    wt guide tiers           who can do what, on-call, paging
    wt guide assessments     signals become assessments; how they close or get promoted
    wt guide signals         where signals come from; suppliers; correlation of re-fires/recoveries
    wt guide email           email in and out
    wt guide communications  reviewed, deadline-driven customer updates
    wt guide specialists     engaging subject-matter experts without escalating
    wt guide rca             curated root-cause analysis and corrective actions
    wt guide interfaces      the API, wt, the web UI and MCP, and how they relate
    wt guide tour            a hands-on walkthrough against a local server
