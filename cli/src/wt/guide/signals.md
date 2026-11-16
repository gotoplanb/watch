# Signals

ADR-054, ADR-057 (synthetic monitoring), ADR-059 (correlation). The full catalog, with worked Square
scenarios, is in `docs/signals-and-outcomes.md`.

## Where they come from

| Source | How it arrives |
|---|---|
| Sumo Logic alerts (our logs/metrics) | Sumo's outbound webhook → `POST /api/signals/intake` |
| Synthetic checks (our endpoints, or a supplier's) | A monitoring service, e.g. Uptime Robot, sends webhooks |
| Supplier status pages (Square, GitHub) | Their webhook, or their email (see `wt guide email`) |
| People: customers, suppliers, colleagues | Email |
| Operators | The UI, or `wt new` |

Watch does not run synthetic checks itself (ADR-057). A prober inside Watch would fail exactly
when Watch's own infrastructure is in trouble. Watch receives and assesses.

## Suppliers and fan-out

We sit in the middle: customers use our services, and our services depend on suppliers. A
supplier signal (`supplier=Square`) fans out to one assessment per dependent customer and
service, as recorded in the service dependencies. Each is answered with that customer's own data,
because a Square problem only matters where it reaches our customers.

    wt signal "Square API down" --signal "HTTP 503" --supplier Square --key monitor-778

## Re-fires and recoveries (ADR-059)

A **correlation key** is the source's stable id for the condition (the monitor id). With it:
- the same condition firing again is recorded on the open assessment, not opened again;
- a **resolved** signal is recorded as evidence and re-runs the policy's checks. Our own
  telemetry decides whether to close, not the monitor's recovery;
- a recovery never resolves an incident; a responder does.

    wt signal "Square API up" --signal "HTTP 200" --supplier Square --key monitor-778 --resolved

## One assessment per question (ADR-060)

An assessment asks "are this customer's users of this service affected?". If one is already open
for that customer and service, a signal from another source is added to it as evidence
(`related_signal`) instead of opening a second assessment. Say our Sumo alert opened
A/checkout, then Square's status page reports a payments incident: Square's notice lands on that
assessment as a likely cause, and Square's later recovery finds it too. Signals with no customer
and service never merge.

## Status-page screenshots (ADR-060)

Status pages have no timestamped permalinks, so what they said is lost once they change. A signal
can cite one (`--url`; email-in picks the link out of a supplier's email). Watch records the link,
and if the host is on the status-page allow-list (set up in the admin) the worker takes a
screenshot: stored as evidence with the capture time, the URL it landed on and a SHA-256, on every
assessment that cited it. Only allow-listed https hosts are loaded, a redirect off the list fails
the capture, and the browser can't reach internal addresses.

    wt signal "Square degraded" --signal "elevated errors" --supplier Square \
        --url https://status.example.com/incidents/abc

## When a monitor goes quiet (ADR-062)

Monitors like Uptime Robot only speak when something changes, so an expired plan or a broken webhook
looks exactly like "all fine". A source can be watched for silence (set up in the admin): it gets a
heartbeat URL, `/api/heartbeats/<token>`, that it calls on a schedule. Uptime Robot can poll it as an
ordinary monitored URL; Sumo can call it from a scheduled search. Any signal from the source also
counts. Silence past the source's limit opens an assessment ("uptimerobot has gone quiet"), and
the next sign of life records the recovery on it, so someone checks what might have been missed.

    wt --as t1a heartbeats
