# Email

ADR-058. Email arrives on a dedicated subdomain through AWS SES (cloud, still to be set up), and
the same parser runs locally:

    wt email-in message.eml

## In

A raw message becomes one of three things:
- **A new signal.** The sender's address or domain maps to a customer, service or supplier
  (the email routes, set up in the admin). Supplier mail fans out like any supplier signal.
  Attachments become evidence.
- **A reply on an existing assessment.** It's recognized from its headers (a reply to one of our
  messages, to the original email, or to an earlier reply) or an `[ASM-0042]` tag in the subject.
  The quoted original is dropped.
- **Ignored, with a reason.** Out-of-office replies, bounces, bulk mail, our own mail, a repeat.

A sender whose domain's email authentication (DMARC) didn't pass, or wasn't checked, is marked
"not verified" on the assessment.

## Out

Communications are delivered to a local outbox today (`wt outbox`). In the cloud they go out
through SES from the same subdomain, so replies come back to Watch and thread onto the
assessment. See `wt guide communications`.

## Why SES on a subdomain

It isolates Watch from company mail: the main domain and the existing ServiceNow address stay
untouched, and the only IT dependency is DNS for the subdomain. Reading a Google Workspace mailbox
instead would need domain-wide delegation, a much broader grant than Watch needs.
