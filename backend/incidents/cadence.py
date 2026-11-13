"""Durable deadline sweep; each action rechecks locked state before writing."""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import assessments, automation, communications, escalation, services
from .models import Assessment, Communication, DiagnosticRun, Incident, OperationsHeartbeat, SmeEngagement, Tier


def tick():
    counts = {"communications": 0, "handoffs": 0, "escalations": 0}
    now = timezone.now()
    # A crashed worker must leave a visible, recoverable state rather than run forever.
    cutoff = now - timedelta(hours=1)
    for pk in Assessment.objects.filter(automation_state="running", updated_at__lt=cutoff).values_list("pk", flat=True)[:100]:
        with transaction.atomic():
            row = Assessment.objects.select_for_update().get(pk=pk)
            if row.automation_state == "running" and row.updated_at < cutoff:
                row.automation_state = "failed"
                row.save(update_fields=["automation_state", "updated_at"])
                assessments.append(row, "automation_failed", "Worker lease expired. Review retained evidence and retry automated assessment.", "system:deadline")
    for pk in DiagnosticRun.objects.filter(state="running", started_at__lt=cutoff).values_list("pk", flat=True)[:100]:
        with transaction.atomic():
            run = DiagnosticRun.objects.select_for_update().get(pk=pk)
            if run.state == "running" and run.started_at < cutoff:
                run.state, run.error, run.finished_at = "failed", "Worker lease expired; run the saved check again.", now
                run.save(update_fields=["state", "error", "finished_at"])
                assessments.append(run.assessment, "query_failed", run.error, "system:deadline", {"run_id": str(run.pk)})
    for pk in list(Communication.objects.filter(state="review", review_deadline__lte=now).values_list("pk", flat=True)[:100]):
        communications.send(pk, actor="system:deadline", automatic=True)
        counts["communications"] += 1
    for pk in Assessment.objects.filter(verdict="no_issue", automation_policy__auto_close_no_issue=True).exclude(status__in=["no_issue", "promoted"]).values_list("pk", flat=True)[:100]:
        automation.finalize_no_issue(pk)
    for pk in list(SmeEngagement.objects.filter(state="paged", ack_deadline__lte=now).values_list("pk", flat=True)[:100]):
        with transaction.atomic():
            item = SmeEngagement.objects.select_for_update().get(pk=pk)
            if item.state == "paged" and not item.acknowledged_at:
                item.state = "overdue"
                item.save(update_fields=["state"])
                assessments.append(item.assessment, "sme_page_expired", "Specialist acknowledgement deadline elapsed; incident escalation clock continues.",
                                   "system:deadline", {"engagement_id": str(item.pk)})
                counts["handoffs"] += 1
    for pk in Assessment.objects.filter(status="promoted", incident__status="OPEN", incident__escalation_execution_arn="").values_list("pk", flat=True)[:100]:
        assessments.start_engine(pk)
    for pk in Assessment.objects.filter(status="promoted", automation_policy__route_to_t2=True, incident__current_tier="T1", incident__status="OPEN").values_list("pk", flat=True)[:100]:
        row = Assessment.objects.get(pk=pk)
        with transaction.atomic():
            incident = Incident.objects.select_for_update().get(pk=row.incident_id)
            if incident.current_tier != "T1" or not incident.escalation_execution_arn or row.history.filter(kind="automatic_t2_route").exists():
                continue
            if not settings.ESCALATION_LOCAL_MODE and not incident.current_task_token:
                continue
            escalation.send_outcome(incident, escalation.OUTCOME_ESCALATE, actor="system:automation", reason="Confirmed impact: configured Tier 2 routing.")
            if settings.ESCALATION_LOCAL_MODE:
                services.escalate(incident.pk, actor="system:automation", reason="Confirmed impact: configured Tier 2 routing.")
            assessments.append(row, "automatic_t2_route", "Confirmed impact routed to Tier 2 through the incident engine.", "system:automation")
    if settings.ESCALATION_LOCAL_MODE:
        for pk in list(Incident.objects.filter(status="OPEN", escalation_execution_arn__startswith="local:execution:", origin_assessment__isnull=False, sla_deadline_at__lte=now).values_list("pk", flat=True)[:100]):
            with transaction.atomic():
                incident = Incident.objects.select_for_update().get(pk=pk)
                if incident.status != "OPEN" or not incident.sla_deadline_at or incident.sla_deadline_at > now:
                    continue
                if incident.current_tier == Tier.T3:
                    incident.sla_deadline_at = None
                    incident.save(update_fields=["sla_deadline_at"])
                    assessments.append(incident.origin_assessment, "escalation_exhausted", "Tier 3 SLA expired. No higher tier is available; operator action is required.", "system:deadline")
                else:
                    services.escalate(pk, actor="system:auto-escalation", reason="Tier SLA deadline expired.")
                counts["escalations"] += 1
    OperationsHeartbeat.objects.update_or_create(name="operations", defaults={"last_tick": timezone.now(), "detail": counts})
    return counts
