# Communications

ADR-055 (reviewable communication), ADR-051 (drafted update emails).

Customer and stakeholder updates are drafted, reviewed, and then sent or suppressed. Every edit is
a new numbered revision.

    wt --as t1a do ASM-0007 email recipients=ops@customer.example
    wt --as t1a comm <id>                     # read it
    wt --as t1a comm <id> edit subject=... body=... recipients=...
    wt --as t1a comm <id> send                # or: suppress reason=...

## Two deadlines

- **Review window:** how long a person has to review the draft.
- **Communication SLA:** when the customer must have heard from us.

Editing doesn't extend either deadline. Send and suppress name the revision they act on, so
nobody sends a version they haven't seen: a stale one is refused with 409.

## Automatic sending

A draft can be authorized to send itself when the review window expires. New evidence revokes
that authorization, so an automatic message never goes out with stale facts. The deadlines are
enforced by the background worker (`make operations`), not by anyone's open browser.

## Delivery

Today delivery is a local outbox with downloadable previews: no external email is sent. Real
delivery (SES) is cloud work under ADR-058.
