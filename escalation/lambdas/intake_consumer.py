"""
SQS intake consumer Lambda (ADR-002 / ADR-009).

The durable buffer (API Gateway -> SQS) is drained here: each message body is the webhook
payload, validated by the SAME IntakeSerializer the API uses, then run through the shared
idempotent create path + per-incident escalation start (one decision implementation, many
callers — ADR-001). This is the prod form of the local `consume_intake` command.

Returns batchItemFailures so a single bad/transient message is retried (and eventually
DLQ'd at maxReceiveCount) without re-driving the whole batch.
"""
import json

import _bootstrap


def handler(event, context=None):
    _bootstrap.setup_django()
    from incidents import escalation
    from incidents.intake import create_incident_idempotent
    from incidents.serializers import IntakeSerializer

    failures = []
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            s = IntakeSerializer(data=body)
            s.is_valid(raise_exception=True)
            d = s.validated_data

            incident, created = create_incident_idempotent(
                source=d["source"],
                payload=d["payload"],
                title=d["title"],
                source_event_id=d["source_event_id"] or None,
            )
            if created:
                incident.escalation_execution_arn = escalation.start_escalation(incident)
                incident.save(update_fields=["escalation_execution_arn", "updated_at"])
        except Exception:
            # Per-message failure: SQS retries just this one (then DLQ).
            failures.append({"itemIdentifier": record["messageId"]})

    return {"batchItemFailures": failures}
