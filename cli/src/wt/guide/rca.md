# Root-cause analysis

ADR-055 (curated RCA), ADR-021/031 (timeline and records).

The RCA is **curated from the history, not written separately**. Reviewers pick original events
(a check result, a decision, a specialist's action) into sections: detection, impact,
escalation, decision, mitigation, recovery, communication, root cause. Each selection links back
to its source event and attachments.

    wt --as t1a show ASM-0007                                  # events carry ids, e.g. #342
    wt --as t1a do ASM-0007 curate event=342 section=root_cause note="..."
    wt --as t1a do ASM-0007 rca summary=... root_cause=... state=final
    wt --as t1a rca ASM-0007                                   # the RCA as markdown

- **Final needs:** a resolved incident, a summary, a root cause and at least one selected
  event. Changing final selections returns it to review.
- **Corrective actions** have an owner, a due date and a state, and need verification to
  complete:

      wt --as t1a do ASM-0007 remediation title="Canary config" owner=t2a due_date=2026-12-01
      wt --as t2a remediation <id> state=done verification="canary live"
