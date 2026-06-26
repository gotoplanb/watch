"""
auto_escalate Lambda (ADR-001/007).

Invoked when a tier's SLA timeout fires. Advances the incident to the next tier and
writes the SAME transition shape as a manual escalation, with actor
`system:auto-escalation` (spec §3) — the audit trail is agnostic to *how* it moved.

Idempotent: "escalate if still applicable", never blind "escalate" (ADR-001), so
Step Functions retries are safe. In the real build this calls the same decision used
by the API (incidents.services.escalate) over a thin DB layer.
"""
import logging

logger = logging.getLogger()
logger.setLevel("INFO")

SYSTEM_ACTOR = "system:auto-escalation"


def handler(event, context):
    incident_id = event["incidentId"]
    from_tier = event["fromTier"]

    # TODO(real build): call the shared, idempotent escalate() decision:
    #   services.escalate(incident_id, actor=SYSTEM_ACTOR, reason="SLA elapsed")
    # which advances tier (if still OPEN and not already advanced) and appends the
    # Transition audit record. No-op if already escalated/resolved.
    logger.info("auto_escalate incident=%s from_tier=%s actor=%s",
                incident_id, from_tier, SYSTEM_ACTOR)
    return {"escalated": True, "incidentId": incident_id, "fromTier": from_tier}
