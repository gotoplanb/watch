"""
record_token Lambda (ADR-007).

Invoked when a tier's waitForTaskToken state is entered. Persists the tier's task token
+ SLA deadline to Postgres so the API can later SendTaskSuccess. Returns immediately;
the Step Functions task then waits for the token (human action) or times out
(auto-escalate). Writes NO transition — the commit Lambda owns the audit record.

The return value is ignored by Step Functions for waitForTaskToken tasks (the task
output comes from SendTaskSuccess), so this only needs to perform its side effect.
"""
import _bootstrap


def handler(event, context=None):
    _bootstrap.setup_django()
    from incidents import services

    incident = services.record_tier_token(
        event["incidentId"], event["tier"], event["taskToken"]
    )
    return {"recorded": True, "incidentId": event["incidentId"], "tier": incident.current_tier}
