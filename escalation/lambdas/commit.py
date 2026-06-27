"""
commit Lambda (ADR-001 / ADR-007).

The single writer of lifecycle transitions when the real engine runs. Invoked on a
decided edge — manual escalate/resolve (actor = user, from the SendTaskSuccess output)
or an SLA timeout (actor = system:auto-escalation). Delegates to the shared, idempotent
`incidents.services` decision functions, so manual and automatic transitions write the
SAME record shape (spec §3) and retries are safe (ADR-001).
"""
import _bootstrap

SYSTEM_ACTOR = "system:auto-escalation"


def handler(event, context=None):
    _bootstrap.setup_django()
    from incidents import services

    action = event["action"]
    actor = event.get("actor") or SYSTEM_ACTOR
    incident_id = event["incidentId"]
    reason = event.get("reason", "")

    if action == "ESCALATE":
        incident = services.escalate(incident_id, actor=actor, reason=reason)
    elif action == "RESOLVE":
        incident = services.resolve(incident_id, actor=actor, reason=reason)
    else:
        raise ValueError(f"unknown commit action: {action!r}")

    return {
        "committed": action,
        "incidentId": incident_id,
        "status": incident.status,
        "tier": incident.current_tier,
    }
