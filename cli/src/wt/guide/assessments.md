# Assessments: signals before incidents

ADR-054 (assess first), ADR-055 (the workspace), ADR-059 (correlation).

## Why

A concern isn't an incident until customer impact is established. Treating every alert as an
incident makes the response metrics meaningless and pages people for noise. So a signal opens an
**assessment** (ASM-0007), and the incident clock only starts if it is **promoted**.

## How an assessment is worked

Open, record evidence, conclude, then dispose:

    wt --as t1a new "checkout errors" --signal "5xx up" --customer "Example Gateway"
    wt --as t1a do ASM-0007 evidence summary="5xx at 4%" result="4% for 10m"
    wt --as t1a do ASM-0007 conclusion verdict=degraded confidence=high reason="sustained"
    wt --as t1a do ASM-0007 promote reason="customer impact"

The ways it can end:

| Outcome | When |
|---|---|
| **Closed · no issue** | A "no issue" conclusion. The evidence stays on record. |
| **Monitoring** | Inconclusive, or a supplier is still unstable. Stays open; a policy reruns its checks every 30 minutes by default (ADR-061). |
| **Promoted** | Impact confirmed. An incident opens at T1 (or T2 by policy), with the history attached. |

The server enforces the order. Promotion needs evidence of degradation (or an explicit
immediate-incident policy reason). Closing as "no issue" needs a "no issue" conclusion.

## Automation (policies)

A **policy** matches assessments by customer, service and supplier, and runs **diagnostic checks**:
saved queries against our own telemetry (Prometheus, Sumo Logic). From their results it can
conclude, then promote on breach, close on "no issue", or draft a customer communication. A
responder who steps in during automation takes over the decision.

## The workspace

Everything about an assessment lives in one history: notes, photos, checks, conclusions,
messages, specialist handoffs, and later the RCA. Every workspace action is available in `wt do`;
see `wt do --help`.
