# The incident lifecycle

ADR-007 (state), ADR-001 and ADR-010 (the escalation engine), ADR-009 (intake dedupe).

## State

An incident has two independent fields:
- **status**: OPEN or RESOLVED.
- **current tier**: T1, T2 or T3.

It also records when it was acknowledged. An incident can be resolved at any tier.
Incidents have human numbers like INC-0042 (ADR-031).

## The clock

Each tier has a deadline: by default 15 minutes at T1, 30 at T2, 60 at T3 (`SLA_T*_SECONDS`,
set for the whole deployment; extending one incident's deadline is deliberately not supported yet,
ADR-007). In the cloud, AWS Step Functions holds one wait per tier. Locally, `make dev` runs the
same decisions without Step Functions.

When a deadline passes, **acknowledged or not**, the incident escalates automatically, recorded as
`system:auto-escalation`, and the next tier's clock starts. At T3 there is nowhere further to go:
the incident stays open at T3, and in the cloud the missed deadline raises the `escalation-failed`
alarm (ADR-001, ADR-048). A missed T3 deadline is treated as a failure worth paging about.

## The three actions

| Action | What it does | Stops the clock? |
|---|---|---|
| **Acknowledge** | "I've got this." Records the time on the history; changes nothing else. | **No.** An acknowledged incident still escalates at its deadline. Acknowledging is not progress. |
| **Escalate** | Moves to the next tier, starts that tier's clock, pages its on-call. | Ends this tier's clock. |
| **Resolve** | Closes the incident. | Ends it. No timer is left running. |

Escalate and resolve are tier-ending decisions: each tier's wait is consumed exactly once. Two
people acting at once can't both succeed: an action states the tier it expects (`--expect T1`
in `wt`), and the server refuses with 409 if the incident has already moved.

## Who resolves

A person resolves incidents. A recovering monitor never does (ADR-059): the signal is
recorded on the incident and a responder decides. The one automatic exception today is the T1
triage assistant (ADR-036), which closes an incident it classifies as a false positive while in
highway mode, with its decision recorded for review.

**Modes:** "highway" is normal operation; "race" is for incident-heavy periods, when a T2+
responder can switch the system into a stricter mode (ADR-035, ADR-053). The mode is recorded
today, but most of race mode's effects (tighter deadlines, a visible banner, human confirmation of
false positives) are designed and not yet built.

## Intake and duplicates

Duplicates are detected while an incident is open: the source's event id, or a hash of the
payload. A retry while it's open does nothing; the same event after it's resolved opens a new one
(ADR-009). Assessments dedupe differently: a source's delivery is remembered even after the
assessment closes, so a retried webhook never reopens a question already answered (ADR-054). Most signals now go through assessments first (see `wt guide assessments`). The direct
incident intake (`wt intake`) is for sources trusted enough to skip that step.
