# Signals and outcomes

What comes into Watch, how it gets there, and what we expect to happen next. This is the
catalog that scenarios, seed data and `wt` checks should be written against. Decisions it
made here are recorded as ADR-057 (synthetic monitoring) and ADR-058 (email in and out).

## The rule every signal follows

A signal is a **question, not an answer** (ADR-054). "Square is degraded" or "a synthetic check
failed" asks *are our customers affected?* Watch answers it with our own evidence before anyone
is paged for an incident.

We sit in the middle: customers depend on our services, and our services depend on suppliers
(Square for payments, GitHub, inventory providers). A supplier problem is not automatically our
incident. It becomes one when our own telemetry shows customer impact.

```
 supplier / monitor / person                      Watch
 ───────────────────────────        ┌──────────────────────────────────────────────┐
 Sumo alert ──────── webhook ──┐    │  signal ─► assessment(s) per customer/service │
 Uptime Robot check ─ webhook ─┼──► │      │  (fan-out via ServiceDependency)       │
 Status page update ─ email/hook ┤  │      ▼                                        │
 Customer / supplier ─ email ───┘   │  diagnostics: our Sumo / Prometheus queries   │
 Operator ─────────── UI / wt ────► │      ▼                                        │
                                    │  outcome (below)                              │
                                    └──────────────────────────────────────────────┘
```

## Outcomes

Every assessment ends in one of these. A scenario is only fully specified once its expected
outcome is named.

| Outcome | What it means | What people see |
|---|---|---|
| **Closed · no issue** | Evidence shows no customer impact. | History only. With a policy, this happens automatically (`auto_close_no_issue`). |
| **Monitoring** | Inconclusive, or the supplier is still unstable. | Assessment stays open; nobody is paged for an incident. Rechecks are by hand today (gap 8). |
| **Promoted → incident** | Impact confirmed. The incident clock starts at promotion (ADR-054). | Tier T1 is paged (or T2 when the policy routes there), and the SLA timer runs. |
| **Communication** | Customers or stakeholders need to hear about it. | A draft enters the review window, then is sent or suppressed (ADR-055). |
| **Specialist engaged** | Subject knowledge is needed, not more authority. | The specialist is paged; the tier and the clock are unchanged (ADR-052). |
| **Direct incident** | The source is trusted enough to skip assessment. | `/api/intake/webhook` creates the incident immediately. Use sparingly. |

## Signal catalog

| Source | Arrives via | Watch endpoint today | Identity (dedupe) | Scope it carries | Status |
|---|---|---|---|---|---|
| **Sumo Logic monitor** (our logs/metrics) | Outbound webhook; the payload is templated in Sumo | `POST /api/signals/intake` | Sumo alert id | service (+ customer if the query is scoped) | Works. The template still needs writing. |
| **Prometheus / Grafana alert** (Watchtower) | Alertmanager or Grafana webhook | `POST /api/signals/intake` | alert fingerprint | service | Needs a small adapter: Alertmanager's body isn't our schema. |
| **Synthetic check on our endpoints** | Uptime Robot (or similar) webhook | `POST /api/signals/intake` | monitor id + down-event time | service | Works if the vendor can template the body and send our secret header. |
| **Synthetic check on a supplier** (e.g. Square API) | Uptime Robot webhook | `POST /api/signals/intake` with `supplier=Square` | monitor id + down-event time | supplier → fans out | Works. Fan-out needs `ServiceDependency` rows. |
| **Supplier status page** (Square, GitHub) | The status page's webhook or email subscription | `signals/intake` (webhook) or email-in | incident id from the status page | supplier → fans out | Webhook: an adapter. Email: needs email-in. |
| **Email from a person** (customer, supplier, colleague) | Today an address hosted by ServiceNow | none | `Message-ID` | sender → customer; subject/body → signal | **Gap: email-in.** |
| **Reply to a Watch email** | email | none | `In-Reply-To` / `[ASM-0025]` token | the existing assessment | **Gap: email-in threading.** |
| **Operator by hand** | UI or `wt new` | `POST /api/assessments/` | generated | whatever they enter | Works. |
| **Public status-page report** | Anonymous form (ADR-027) | `/api/report/incident` | rate-limited | none | Works. |
| **Session check** | Webhook (ADR-022) | `/api/checks/webhook` | check id | user session | Works. |

A signal for Watch's own intake has this shape (`/api/signals/intake`, shared-secret header):

```json
{"title": "Square payments API failing", "source": "uptimerobot",
 "signal": "Monitor 'Square /v2/payments' down: HTTP 503 from 3 regions",
 "supplier": "Square", "customer": "", "service": "",
 "source_event_id": "ur-778812-2026-11-16T14:02Z", "payload": {"...": "raw vendor body"}}
```

Anything that can template a webhook body sends this directly. Anything that can't gets a thin
adapter that translates its native body into this shape.

## Worked scenarios: Square as a payments supplier

Setup: `ServiceDependency` rows say that customers A and B use our **checkout** service, which
depends on **Square**. An active policy for checkout runs the diagnostic check
*"checkout payment error rate"* (a Sumo query on our own logs).

**1. Square's status page reports degraded payments; we're fine.**
The status page update arrives with `supplier=Square`. It fans out to assessments for
A/checkout and B/checkout. Each runs our payment error-rate query, which stays within bounds.
→ **Monitoring** while Square's incident stays open (recheck), then **closed · no issue** once it
resolves. Nobody is paged for an incident. The history shows we looked and why we concluded there
was no impact.

**2. Square's status page is silent, but our synthetic check on Square's API fails.**
Same fan-out, so the same path as scenario 1. If our error rate is up → **promoted** for each
affected customer, T1 paged, and the customer communication drafted with a review window. The
payments specialist area is engaged if T1 needs Square expertise.

**3. Our own Sumo alert fires: checkout 5xx at 4%. No supplier signal.**
One assessment, checkout scoped. The diagnostics confirm it → **promoted**. Twenty minutes later
Square posts an incident. *Expected:* it attaches as evidence to the open checkout assessment,
pointing at the likely root cause. *Today:* it opens new, separate assessments (see gaps).

**4. A synthetic check blips once and recovers within a minute.**
A down webhook, then an up webhook. *Expected:* the down signal's diagnostics find no sustained
impact → **closed · no issue**, with the recovery recorded as evidence. *Today:* the up webhook
would open a second assessment (see gaps).

**5. A customer emails "card payments failing since 9:40".**
*Expected:* email-in maps the sender's domain to customer A and opens an A/checkout assessment
with the email body as the signal and attachments as evidence. Our acknowledgement goes out from
Watch with a `[ASM-…]` token. When the customer replies, the reply lands in the same assessment's
history rather than opening a new one.

**6. Square emails planned maintenance next Tuesday.**
*Expected:* an informational signal. It shouldn't page anyone or need an assessment closed by
hand. *Today:* no concept of a maintenance window (see gaps).

**7. Uptime Robot itself goes quiet** (expired subscription, broken webhook).
*Expected:* the absence of check-ins is visible. This mirrors the operations heartbeat Watch already
keeps for its own worker. *Today:* not covered.

## Decisions

### D1 — Synthetic monitoring lives outside Watch. **Accepted: ADR-057.**
Uptime Robot, Checkly, Grafana synthetic monitoring and others already run continuous checks
cheaply, from many regions, with their own reliability. Watch's job is to **receive and assess**,
not to **probe**: a prober inside Watch fails exactly when Watch's own infrastructure is in
trouble (the ADR-002 argument). Prove it with an inexpensive Uptime Robot plan sending webhooks
to `signals/intake`.

What Watch *does* keep is **on-demand verification**: diagnostics run while assessing (today Sumo,
Prometheus and fixtures). A small `http` diagnostic provider would let an assessment ask "is
Square's API answering right now?" once, in context. That covers the "runner inside our project"
convenience without becoming a scheduler.

To verify before building: whether Uptime Robot's webhook can template a JSON body and set a
custom header for our secret. If it can't, we need an adapter, or a token in the URL as a weaker
fallback.

### D2 — Email in. **Accepted: ADR-058 (SES on a subdomain).**
Build the **parser** independently of the **transport**: raw RFC 822 message in, then a signal,
a reply on an existing assessment, or ignored. That part is the same whichever transport we pick,
and it can be developed and tested locally (`wt email-in message.eml`).

What the parser has to handle:
- **Sender → scope:** a table of domains/addresses → customer or supplier (like `ServiceDependency`).
  Unknown senders still create an assessment, just unscoped.
- **Threading:** `In-Reply-To`/`References`, or a `[ASM-0025]` token in the subject, append the
  message to the existing assessment instead of opening a new one.
- **Dedupe:** `Message-ID` is the `source_event_id`.
- **Attachments** become evidence, under the existing upload limits.
- **Loops and noise:** ignore `Auto-Submitted`, bounces and out-of-office replies, and our own
  outbound messages.
- **Trust:** keep the SPF/DKIM/DMARC verdict on the assessment. A spoofed "customer" email should
  look different from a real one.

Transport options (pick by whichever approval is easier at work):
1. **AWS SES inbound on a subdomain** (`watch.example.com`): an MX record, then SES receipt rule →
   S3 (raw copy kept) → Lambda → `signals/intake`. Lives in `platform` as Terraform. Needs an IT DNS
   change for the subdomain only; the main domain and the ServiceNow address are untouched.
2. **A Google Workspace mailbox read by the Gmail API**: no DNS change, but needs a service
   account with Workspace admin approval (domain-wide delegation), plus push (Pub/Sub) or polling.
3. **An inbound-parse service** (Postmark, SendGrid, Mailgun): they POST parsed JSON to a webhook.
   Quickest to stand up, but adds another vendor for customer email.

**Chosen:** SES inbound on a subdomain (option 1). Gmail API access needs domain-wide delegation, a broader grant than Watch should hold. It's AWS-native, managed as Terraform,
keeps a raw copy for audit, and pairs with SES for sending. During the move off ServiceNow, have
the existing ServiceNow address also forward a copy to the Watch address, so both systems see
every message until we cut over.

### D3 — Email out. **Accepted: ADR-058 (SES).**
Today every communication goes to the local outbox (ADR-055). Put one transport interface behind
it: `local` (today) and one real transport. Recommend **SES** with the same subdomain as D2, so
that:
- `Reply-To` routes replies back to Watch, and `Message-ID`/`References` thread them (D2);
- sending reputation is isolated from the company's main mail;
- delivery events (bounces, complaints) come back via SNS and land in the communication's history.

SES starts in sandbox mode and needs a production-access request, a `cloud`-labelled dependency.
The alternative is the Gmail API sending as a Workspace user. Choose it only if work requires mail
to come from Workspace.

## Gaps this catalog exposes

1. **Recovery correlation** (#64): an "up"/"resolved" signal should attach to the open assessment it
   resolves, not open a new one (scenario 4). Needs a correlation key alongside the dedupe key.
2. **Cross-signal correlation** (#65): a supplier signal arriving while a related assessment is open
   should be offered as evidence on it (scenario 3).
3. **Maintenance windows** (#66): scheduled supplier or our own maintenance should suppress or annotate
   signals for that scope and time (scenario 6).
4. **Email in / email out:** the parser (#71, local) and the SES transport (#72, `cloud`).
5. **Vendor adapters** (#69): Alertmanager, status page and any monitor that can't template our schema.
6. **Monitor silence** (#68): detecting that a monitoring source has stopped reporting (scenario 7).
7. **`http` diagnostic provider** (#70): on-demand verification of a supplier endpoint (D1).
8. **Monitoring rechecks** (#67): a "monitoring" assessment is never rechecked automatically, and it
   doesn't close when the supplier recovers (scenario 1). It needs a recheck interval in the
   policy, or recovery correlation (gap 1).

Gaps 1–3, 5–8 and the email parser can all be built and tested locally. The SES transport and
provider accounts are `cloud` work.
