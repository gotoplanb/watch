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
