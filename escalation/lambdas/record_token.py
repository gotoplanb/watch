"""
record_token Lambda (ADR-007).

Invoked when a tier's waitForTaskToken state is entered. Its only job: persist the
tier's task token + SLA deadline to Postgres atomically, so the API can later call
SendTaskSuccess to advance/resolve. It returns immediately; the Step Functions task
then waits for the token to be sent (human action) or to time out (auto-escalate).

Decision logic lives in Python; ASL only orchestrates. This stub shows the contract;
wire it to the Django ORM / a thin DB layer in the real build.
"""
import logging

logger = logging.getLogger()
logger.setLevel("INFO")


def handler(event, context):
    incident_id = event["incidentId"]
    tier = event["tier"]
    task_token = event["taskToken"]

    # TODO(real build): in one transaction —
    #   UPDATE incidents SET current_task_token = :token,
    #                        current_tier = :tier,
    #                        sla_deadline_at = now() + tier_sla(:tier)
    #   WHERE id = :incident_id;
    # Idempotent: writing the token is safe to retry (ADR-001).
    logger.info("record_token incident=%s tier=%s token=%s...",
                incident_id, tier, task_token[:12])
    return {"recorded": True, "incidentId": incident_id, "tier": tier}
