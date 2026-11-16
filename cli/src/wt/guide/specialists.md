# Specialists (subject-matter experts)

ADR-052. **Escalating buys authority; engaging a specialist buys knowledge.** Needing the one
engineer who knows the payments integration is not a reason to escalate.

    wt --as t1a areas                                         # subject areas and their members
    wt --as t1a do ASM-0007 engage area=1 reason="processor expertise"
    wt --as t3a sme-respond <id> ack
    wt --as t3a sme-respond <id> complete --body "failed over the processor"

- Engaging pages a named person or a subject area's members, with your reason and selected
  evidence. It needs an incident: promote the assessment first.
- It does **not** change the tier, and does **not** pause or extend the incident's clock.
- Only the paged specialist (or an administrator) can acknowledge. Engagement grants no
  authority: a specialist can resolve or escalate only if their own tier allows it.
- Late acknowledgement and missed deadlines stay in the history.
