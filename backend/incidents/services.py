"""
Transition service — the one place that mutates incident lifecycle state and writes
the matching append-only audit record (ADR-007). Manual (API) and automatic
(Lambda timeout) paths both call through here so the Transition shape is identical
regardless of *how* it happened (spec §3).

Every transition is idempotent: "act if still applicable", never blind "act"
(ADR-001). Callers wrap these in select_for_update where concurrency matters.
"""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Incident, Status, Tier, Transition, next_tier


def _record(incident, *, to_status, to_tier, actor, reason, from_status, from_tier):
    Transition.objects.create(
        incident=incident,
        from_status=from_status,
        from_tier=from_tier,
        to_status=to_status,
        to_tier=to_tier,
        actor=actor,
        reason=reason,
    )


@transaction.atomic
def acknowledge(incident_id, actor: str, reason: str = "") -> Incident:
    """ACK: Postgres-only, does NOT consume the task token; the SLA clock keeps
    running (ADR-007). Idempotent — re-acking is a no-op."""
    incident = Incident.objects.select_for_update().get(pk=incident_id)
    if incident.status == Status.OPEN and incident.acknowledged_at is None:
        incident.acknowledged_at = timezone.now()
        incident.save(update_fields=["acknowledged_at", "updated_at"])
        _record(
            incident,
            from_status=incident.status,
            from_tier=incident.current_tier,
            to_status=incident.status,
            to_tier=incident.current_tier,
            actor=actor,
            reason=reason or "acknowledged",
        )
    return incident


@transaction.atomic
def escalate(incident_id, actor: str, reason: str = "") -> Incident:
    """Advance one tier. Manual (actor=user) and auto (actor=system) share this path.
    Idempotent via select_for_update + applicability check."""
    incident = Incident.objects.select_for_update().get(pk=incident_id)
    if incident.status != Status.OPEN:
        return incident
    target = next_tier(incident.current_tier)
    if target is None:
        # At T3 with nowhere to escalate — the ASL routes this to a Fail state, which
        # surfaces as a failed execution -> alarm (ADR-001). Nothing to do app-side.
        return incident

    from_tier = incident.current_tier
    incident.current_tier = target.value
    incident.acknowledged_at = None  # new tier, not yet acknowledged
    incident.current_task_token = ""  # T-prev token consumed; record_token sets the next
    incident.save(
        update_fields=["current_tier", "acknowledged_at", "current_task_token", "updated_at"]
    )
    _record(
        incident,
        from_status=incident.status,
        from_tier=from_tier,
        to_status=incident.status,
        to_tier=target.value,
        actor=actor,
        reason=reason or "escalated",
    )
    return incident


@transaction.atomic
def record_tier_token(incident_id, tier: str, token: str, sla_seconds: int | None = None) -> Incident:
    """Called by the record_token Lambda when Step Functions enters a tier's
    waitForTaskToken state (ADR-007). Persists the outstanding token + SLA deadline so
    the API can later SendTaskSuccess. Idempotent — safe to retry. Writes no Transition;
    the commit Lambda (services.escalate/resolve) owns the audit record."""
    incident = Incident.objects.select_for_update().get(pk=incident_id)
    if incident.status != Status.OPEN:
        return incident  # resolved between dispatch and entry — leave it terminal
    if sla_seconds is None:
        sla_seconds = settings.TIER_SLA_SECONDS.get(tier, 0)
    incident.current_tier = tier
    incident.current_task_token = token
    incident.sla_deadline_at = timezone.now() + timedelta(seconds=sla_seconds)
    incident.save(
        update_fields=["current_tier", "current_task_token", "sla_deadline_at", "updated_at"]
    )
    return incident


@transaction.atomic
def resolve(incident_id, actor: str, reason: str = "") -> Incident:
    """RESOLVE: terminal. Consuming the token (via the caller / send_outcome) ends the
    execution in Succeed, so no zombie timer can later auto-escalate (ADR-007)."""
    incident = Incident.objects.select_for_update().get(pk=incident_id)
    if incident.status == Status.RESOLVED:
        return incident
    from_status = incident.status
    incident.status = Status.RESOLVED
    incident.current_task_token = ""  # consumed
    incident.save(update_fields=["status", "current_task_token", "updated_at"])
    _record(
        incident,
        from_status=from_status,
        from_tier=incident.current_tier,
        to_status=Status.RESOLVED,
        to_tier=incident.current_tier,
        actor=actor,
        reason=reason or "resolved",
    )
    return incident
