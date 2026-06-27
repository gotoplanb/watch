"""
Step Functions client wrapper (ADR-001 / ADR-007).

ASL orchestrates and owns timing; Python decides. This module is the app-tier side
of the contract:
  - start_escalation(): one Standard execution per incident at creation.
  - send_outcome(): consume the current tier's task token with an outcome
    (ESCALATE / RESOLVE). ACK does NOT come here — ack is Postgres-only and never
    consumes the token (ADR-007).

In ESCALATION_LOCAL_MODE the calls are logged no-ops so the app runs without AWS
(or against Step Functions Local). Real AWS calls go through boto3.
"""
import json
import logging

import boto3
from django.conf import settings

logger = logging.getLogger(__name__)

# Outcomes carried in SendTaskSuccess output; an ASL Choice routes on these.
OUTCOME_ESCALATE = "ESCALATE"
OUTCOME_RESOLVE = "RESOLVE"


def _client():
    kwargs = {"region_name": settings.AWS_REGION}
    if settings.ESCALATION_ENDPOINT_URL:
        # Step Functions Local — dummy creds, explicit endpoint.
        kwargs.update(
            endpoint_url=settings.ESCALATION_ENDPOINT_URL,
            aws_access_key_id="x",
            aws_secret_access_key="x",
        )
    return boto3.client("stepfunctions", **kwargs)


def start_escalation(incident) -> str:
    """Start the per-incident execution. Returns the execution ARN (or a local stub)."""
    payload = {"incidentId": str(incident.id), "tier": incident.current_tier}
    if settings.ESCALATION_LOCAL_MODE or not settings.ESCALATION_STATE_MACHINE_ARN:
        arn = f"local:execution:{incident.id}"
        logger.info("escalation.start (local) incident=%s arn=%s", incident.id, arn)
        return arn
    resp = _client().start_execution(
        stateMachineArn=settings.ESCALATION_STATE_MACHINE_ARN,
        name=f"incident-{incident.id}",
        input=json.dumps(payload),
    )
    return resp["executionArn"]


def send_outcome(incident, outcome: str, actor: str = "") -> None:
    """
    Advance the incident's current tier via SendTaskSuccess, carrying the outcome
    (ESCALATE / RESOLVE) and the acting user. The commit Lambda then writes the
    Transition with that actor (ADR-001/007) — the API itself writes no state here.

    Idempotent: a SendTaskSuccess on an already-consumed token raises TaskDoesNotExist,
    which we treat as a no-op (ADR-007).
    """
    token = incident.current_task_token
    if settings.ESCALATION_LOCAL_MODE or not token:
        logger.info(
            "escalation.send_outcome (local) incident=%s outcome=%s", incident.id, outcome
        )
        return
    client = _client()
    try:
        client.send_task_success(
            taskToken=token, output=json.dumps({"outcome": outcome, "actor": actor})
        )
    except client.exceptions.TaskDoesNotExist:
        logger.warning("escalation.send_outcome token already consumed incident=%s", incident.id)
