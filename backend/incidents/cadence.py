"""Independent deadline items; each action rechecks locked state before writing.

The direct SLA sweep is a local-development substitute for the cloud state machine,
not a second cloud escalation engine (ADR-001/007).
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import assessments, automation, communications, escalation, services
from .models import Assessment, Communication, DiagnosticRun, Incident, OperationsHeartbeat, SmeEngagement, Tier

logger = logging.getLogger(__name__)

DEADLINE_ACTOR = "system:deadline"
T2_ROUTE_REASON = "Confirmed impact: configured Tier 2 routing."


def _sweep(ids, action, counts, counter=None):
    # Slicing a queryset (or list) yields a value that is fully materialized on first
    # iteration, so no cursor stays open across the per-item write transactions.
    for pk in ids[:100]:
        try:
            # A failed database write must not poison the next item's transaction.
            with transaction.atomic():
                changed = action(pk)
            if counter:
                counts[counter] += bool(changed)
        except Exception:
            counts["errors"] += 1
            logger.exception("Operations item failed action=%s id=%s", counter or getattr(action, "__name__", type(action).__name__), pk)


def _expire_assessment(pk, cutoff):
    row = Assessment.objects.select_for_update().get(pk=pk)
    if row.automation_state == "running" and row.updated_at < cutoff:
        row.automation_state = "failed"
        row.save(update_fields=["automation_state", "updated_at"])
        assessments.append(row, "automation_failed", "Worker lease expired. Review retained evidence and retry automated assessment.", DEADLINE_ACTOR)


def _expire_run(pk, cutoff, now):
    run = DiagnosticRun.objects.select_for_update().get(pk=pk)
    if run.state == "running" and run.started_at < cutoff:
        run.state, run.error, run.finished_at = "failed", "Worker lease expired; run the saved check again.", now
        run.save(update_fields=["state", "error", "finished_at"])
        assessments.append(run.assessment, "query_failed", run.error, DEADLINE_ACTOR, {"run_id": str(run.pk)})


def _expire_handoff(pk):
    item = SmeEngagement.objects.select_for_update().get(pk=pk)
    if item.state != "paged" or item.acknowledged_at:
        return False
    item.state = "overdue"
    item.save(update_fields=["state"])
    assessments.append(item.assessment, "sme_page_expired", "Specialist acknowledgement deadline elapsed; incident escalation clock continues.",
                       DEADLINE_ACTOR, {"engagement_id": str(item.pk)})
    return True


def _route_to_t2(pk):
    row = Assessment.objects.get(pk=pk)
    incident = Incident.objects.select_for_update().get(pk=row.incident_id)
    if incident.status != "OPEN" or incident.current_tier != "T1" or not incident.escalation_execution_arn or row.history.filter(kind="automatic_t2_route").exists():
        return
    if not settings.ESCALATION_LOCAL_MODE and not incident.current_task_token:
        return
    escalation.send_outcome(incident, escalation.OUTCOME_ESCALATE, actor=automation.AUTOMATION_ACTOR, reason=T2_ROUTE_REASON)
    if settings.ESCALATION_LOCAL_MODE:
        services.escalate(incident.pk, actor=automation.AUTOMATION_ACTOR, reason=T2_ROUTE_REASON)
    assessments.append(row, "automatic_t2_route", "Confirmed impact routed to Tier 2 through the incident engine.", automation.AUTOMATION_ACTOR)


def _expire_local_tier(pk, now):
    incident = Incident.objects.select_for_update().get(pk=pk)
    if incident.status != "OPEN" or not incident.sla_deadline_at or incident.sla_deadline_at > now:
        return False
    if incident.current_tier == Tier.T3:
        incident.sla_deadline_at = None
        incident.save(update_fields=["sla_deadline_at"])
        assessments.append(incident.origin_assessment, "escalation_exhausted", "Tier 3 SLA expired. No higher tier is available; operator action is required.", DEADLINE_ACTOR)
    else:
        services.escalate(pk, actor="system:auto-escalation", reason="Tier SLA deadline expired.")
    return True


def tick():
    counts = {"communications": 0, "handoffs": 0, "escalations": 0, "errors": 0}
    now = timezone.now()
    cutoff = now - timedelta(hours=1)
    _sweep(Assessment.objects.filter(automation_state="running", updated_at__lt=cutoff).values_list("pk", flat=True),
           lambda pk: _expire_assessment(pk, cutoff), counts)
    _sweep(DiagnosticRun.objects.filter(state="running", started_at__lt=cutoff).values_list("pk", flat=True),
           lambda pk: _expire_run(pk, cutoff, now), counts)
    _sweep(Communication.objects.filter(state="review", review_deadline__lte=now).values_list("pk", flat=True),
           lambda pk: communications.send(pk, actor=DEADLINE_ACTOR, automatic=True), counts, "communications")
    _sweep(Assessment.objects.filter(verdict="no_issue", automation_policy__auto_close_no_issue=True).exclude(status__in=["no_issue", "promoted"]).values_list("pk", flat=True),
           automation.finalize_no_issue, counts)
    _sweep(SmeEngagement.objects.filter(state="paged", ack_deadline__lte=now).values_list("pk", flat=True),
           _expire_handoff, counts, "handoffs")
    _sweep(Assessment.objects.filter(status="promoted", incident__status="OPEN", incident__escalation_execution_arn="").filter(
               Q(engine_start_retry_at__isnull=True) | Q(engine_start_retry_at__lte=now)).values_list("pk", flat=True),
           assessments.start_engine, counts)
    _sweep(Assessment.objects.filter(status="promoted", automation_policy__route_to_t2=True, incident__current_tier="T1", incident__status="OPEN").values_list("pk", flat=True),
           _route_to_t2, counts)
    if settings.ESCALATION_LOCAL_MODE:
        _sweep(Incident.objects.filter(status="OPEN", escalation_execution_arn__startswith="local:execution:", origin_assessment__isnull=False, sla_deadline_at__lte=now).values_list("pk", flat=True),
               lambda pk: _expire_local_tier(pk, now), counts, "escalations")
    OperationsHeartbeat.objects.update_or_create(name="operations", defaults={"last_tick": timezone.now(), "detail": counts})
    return counts
