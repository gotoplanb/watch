"""The assessment workspace actions `wt do` can run, documented for people and their assistants.

Each entry: (what it does, {field: meaning}, rules the server enforces). The fields mirror the
workspace page's forms exactly. A backend test (incidents/tests/test_cli_contract.py) fails if
an action or field here drifts from the server.
"""

ACTIONS = {
    "comment": (
        "Add a note to the history (dashboard links welcome). `wt note` is the shortcut, with --photo.",
        {"body": "the note", "photos": "images (@file; up to 8 PNG/JPEG/WebP, 5 MB each)"},
        ["Needs a note or at least one photo."],
    ),
    "evidence": (
        "Record evidence you gathered yourself: what you checked and what you saw.",
        {"summary": "one line", "result": "what you observed, and over what window",
         "source": "where it came from (default: operator)", "query": "the query or check (optional)"},
        ["New evidence revokes any automatic-send approval on a pending communication."],
    ),
    "query": (
        "Run a saved diagnostic check (Prometheus / Sumo) now; the worker records the result.",
        {"rule": "the check's id (`wt rules`)"},
        ["Needs the worker running (`make operations`)."],
    ),
    "attachment": (
        "Attach one evidence file.",
        {"file": "@path (PNG, JPEG, WebP, PDF or plain text; 5 MB)", "description": "what it shows"},
        ["The type is checked by content, not by file name."],
    ),
    "operation": (
        "Record a hypothesis, decision or action taken, optionally citing evidence.",
        {"kind": "hypothesis | decision | operational_action | mitigation | recovery | bridge_opened",
         "body": "what was decided or done", "selected": "event ids it rests on (e.g. selected=[341,342])"},
        ["This records an action; Watch never executes commands."],
    ),
    "context": (
        "Set severity and customer impact.",
        {"severity": "P1 | P2 | P3 | P4", "impact": "unknown | none | low | moderate | high",
         "reason": "why"},
        [],
    ),
    "claim": (
        "Take ownership. If promoted, the incident is assigned to you too.",
        {"reason": "why you're taking it"},
        [],
    ),
    "conclusion": (
        "Record what the evidence shows.",
        {"verdict": "no_issue | degraded | unknown", "confidence": "low | medium | high | unknown",
         "reason": "the reasoning and the evidence's limits"},
        [],
    ),
    "monitor": (
        "Keep it open as inconclusive, or while a supplier is still unstable. Nobody is paged.",
        {"reason": "why"},
        [],
    ),
    "no_issue": (
        "Close with no customer impact. The evidence stays on record.",
        {"reason": "why"},
        ["Needs a 'no_issue' conclusion first."],
    ),
    "promote": (
        "Impact confirmed: open an incident at T1. Its clock starts now.",
        {"reason": "why this warrants an incident",
         "policy_reason": "an explicit immediate-incident policy reason (optional)"},
        ["Needs evidence of degradation (a 'degraded' conclusion) or a policy_reason.",
         "An assessment is promoted once; its history carries over to the incident."],
    ),
    "email": (
        "Draft a communication. It waits in review, then is sent or suppressed (`wt comm`).",
        {"recipients": "comma-separated addresses", "subject": "default: drafted from the case",
         "body": "default: drafted from the case", "purpose": "update | initial (default: update)",
         "review_seconds": "review window (default 300)", "sla_seconds": "deadline to reach the "
         "customer (default 900)", "auto_send": "true: send by itself when review expires"},
        ["Delivery is a local outbox today (`wt outbox`)."],
    ),
    "engage": (
        "Page a specialist for knowledge, without escalating. No tier or clock change.",
        {"target": "a username", "area": "or a subject area id (`wt areas`)", "reason": "what you "
         "need from them", "selected": "event ids to hand over", "ack_seconds": "time to "
         "acknowledge (default 300)", "request_id": "idempotency key (generated if omitted)"},
        ["Needs an incident: promote first.", "Give a target or an area."],
    ),
    "curate": (
        "Select an original event into an RCA section (or remove it).",
        {"event": "the event id (from `wt show`)", "section": "detection | impact | escalation | "
         "decision | mitigation | recovery | communication | root_cause", "note": "reviewer note",
         "remove": "true to remove the selection"},
        ["Changing selections on a final RCA returns it to review."],
    ),
    "rca": (
        "Write or update the RCA analysis.",
        {"summary": "", "impact": "", "root_cause": "", "contributing_factors": "",
         "detection_gaps": "", "state": "draft | review | final (default: draft)"},
        ["Final needs a resolved incident, a summary, a root cause and selected events."],
    ),
    "remediation": (
        "Add a corrective action with an owner and due date.",
        {"title": "what will be done", "owner": "a username", "due_date": "YYYY-MM-DD"},
        ["Its owner updates it: `wt remediation <id> state=done verification=...`."],
    ),
    "retry_automation": (
        "Rerun a failed automated assessment.",
        {"reason": "why"},
        ["Only for open assessments whose automation failed, with an active matching policy."],
    ),
    "retry_engine": (
        "Retry starting the incident's escalation engine after a failure, skipping the backoff.",
        {"reason": "why"},
        [],
    ),
}

# Actions someone engaged as a specialist (but outside the incident's tiers) may still take.
PARTICIPANT_ACTIONS = ("comment", "attachment", "query", "evidence")


def reference() -> str:
    """The text `wt do --help` prints after the usage."""
    lines = ["Actions, their fields, and what the server refuses:", ""]
    for name, (what, fields, rules) in ACTIONS.items():
        lines.append(f"  {name}: {what}")
        for field, meaning in fields.items():
            lines.append(f"      {field:22} {meaning}".rstrip())
        lines.extend(f"      ! {rule}" for rule in rules)
        lines.append("")
    lines += [
        "Who may act: the incident's tier or higher once promoted (any responder before), except",
        f"that an engaged specialist may also use: {', '.join(PARTICIPANT_ACTIONS)}.",
        "Once closed or promoted, the decisions (conclusion, monitor, no_issue, promote) refuse",
        "with 409; notes, evidence and RCA work continue. Omitted fields get the page's defaults.",
    ]
    return "\n".join(lines)
