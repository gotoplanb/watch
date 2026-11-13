"""Assessment decisions, evidence, and promotion. No customer communication is sent here."""
import hashlib
import json
import logging
from datetime import timedelta

from django.db import transaction
from django.conf import settings
from django.utils import timezone

from . import events, numbering
from .models import (
    Assessment, AssessmentConfidence, AssessmentEvent, AssessmentStatus, AssessmentVerdict,
    Incident, Tier,
)


logger = logging.getLogger(__name__)


class AssessmentConflict(ValueError):
    pass


def append(assessment, kind, body, actor="system", data=None):
    return AssessmentEvent.objects.create(
        assessment=assessment, kind=kind, body=body, actor=actor, data=data or {},
    )


def _open(assessment):
    if assessment.status in {AssessmentStatus.NO_ISSUE, AssessmentStatus.PROMOTED}:
        raise AssessmentConflict("This assessment has already been closed or promoted.")


@transaction.atomic
def receive(*, title, source, signal, customer="", service="", supplier="", payload=None,
            source_event_id=None, signal_at=None, actor="system", assignee=None):
    """Source-scoped delivery dedupe lasts beyond closure: a retry is not a new signal."""
    payload = payload or {}
    identity = source_event_id or json.dumps(
        [title, signal, customer, service, supplier, payload], sort_keys=True, separators=(",", ":")
    )
    dedupe = hashlib.sha256(f"{source}\0{identity}".encode()).hexdigest()
    assessment, created = Assessment.objects.get_or_create(
        dedupe_key=dedupe,
        defaults={"number": numbering.next_number("ASM"), "title": title, "source": source,
                  "signal": signal, "payload": payload, "customer": customer, "service": service,
                  "supplier": supplier, "signal_at": signal_at or timezone.now(), "assignee": assignee},
    )
    if created:
        append(assessment, "signal_received", signal, actor,
               {"source": source, "signal_at": assessment.signal_at.isoformat(), "payload": payload})
    return assessment, created


@transaction.atomic
def add_evidence(pk, *, actor, summary, query="", result="", source="operator"):
    assessment = Assessment.objects.select_for_update().get(pk=pk)
    _open(assessment)
    if not summary.strip() or not result.strip():
        raise ValueError("Evidence needs a summary and a recorded result.")
    event = append(assessment, "evidence_added", summary.strip(), actor,
                   {"query": query, "result": result, "source": source,
                    "execution": "operator_recorded"})
    invalidate_conclusion(assessment)
    return event


def revoke_draft_automation(assessment):
    for mail in assessment.communications.select_for_update().filter(state="review", auto_send=True):
        mail.auto_send = False
        mail.save(update_fields=["auto_send"])
        append(assessment, "communication_review_required", "Assessment evidence changed; automatic delivery revoked until a responder reviews a new revision.",
               data={"communication_id": str(mail.pk), "revision": mail.revision})


def invalidate_conclusion(assessment):
    revoke_draft_automation(assessment)
    if assessment.status in {AssessmentStatus.NO_ISSUE, AssessmentStatus.PROMOTED}:
        return
    assessment.status = AssessmentStatus.INVESTIGATING
    assessment.verdict, assessment.confidence, assessment.conclusion = "unknown", "unknown", ""
    assessment.save(update_fields=["status", "verdict", "confidence", "conclusion", "updated_at"])


@transaction.atomic
def conclude(pk, *, actor, verdict, confidence, reason):
    assessment = Assessment.objects.select_for_update().get(pk=pk)
    _open(assessment)
    if verdict not in AssessmentVerdict.values or confidence not in AssessmentConfidence.values:
        raise ValueError("Choose a valid verdict and confidence.")
    if not reason.strip():
        raise ValueError("Record the evidence-backed reasoning.")
    evidence = list(assessment.history.filter(kind="evidence_added").values_list("pk", flat=True))
    if verdict != AssessmentVerdict.UNKNOWN and not evidence:
        raise ValueError("Record evidence before concluding whether degradation exists.")
    revoke_draft_automation(assessment)
    assessment.verdict, assessment.confidence, assessment.conclusion = verdict, confidence, reason.strip()
    assessment.status = AssessmentStatus.INVESTIGATING
    assessment.save(update_fields=["verdict", "confidence", "conclusion", "status", "updated_at"])
    append(assessment, "conclusion_recorded", reason.strip(), actor,
           {"verdict": verdict, "confidence": confidence, "evidence_ids": evidence})
    return assessment


@transaction.atomic
def dispose(pk, *, actor, outcome, reason):
    assessment = Assessment.objects.select_for_update().get(pk=pk)
    _open(assessment)
    if not reason.strip():
        raise ValueError("Record a reason for the disposition.")
    if outcome == AssessmentStatus.NO_ISSUE:
        if assessment.verdict != AssessmentVerdict.NO_ISSUE:
            raise ValueError("A no-issue conclusion is required before closing without an incident.")
        assessment.closed_at = timezone.now()
    elif outcome != AssessmentStatus.MONITORING:
        raise ValueError("Choose monitoring or close with no issue.")
    assessment.status = outcome
    assessment.save(update_fields=["status", "closed_at", "updated_at"])
    append(assessment, "closed_no_issue" if outcome == AssessmentStatus.NO_ISSUE else "monitoring_started",
           reason.strip(), actor)
    return assessment


@transaction.atomic
def comment(pk, *, actor, body):
    assessment = Assessment.objects.select_for_update().get(pk=pk)
    if not body.strip():
        raise ValueError("Write a comment first.")
    event = append(assessment, "comment", body.strip(), actor)
    assessment.save(update_fields=["updated_at"])
    return event


@transaction.atomic
def promote(pk, *, actor, reason, policy_reason="", dispatch=True):
    """A locked one-to-one promotion preserves history and starts incident time here."""
    from . import services

    assessment = Assessment.objects.select_for_update().get(pk=pk)
    if not dispatch and not (settings.DEBUG and settings.ESCALATION_LOCAL_MODE):
        raise ValueError("Exercise promotion requires local development settings.")
    if assessment.incident_id:
        return assessment.incident, False
    _open(assessment)
    if not reason.strip():
        raise ValueError("Record why an incident is warranted.")
    evidence = list(assessment.history.filter(kind="evidence_added").values_list("pk", flat=True))
    if not policy_reason.strip() and (assessment.verdict != AssessmentVerdict.DEGRADED or not evidence):
        raise ValueError("Promotion needs evidence of degradation or an explicit immediate-incident policy reason.")
    incident = Incident.objects.create(
        source=assessment.source, title=assessment.title,
        dedupe_key=f"assessment:{assessment.pk}",
        payload={"assessment_id": str(assessment.pk), "signal": assessment.signal,
                 "customer": assessment.customer, "service": assessment.service,
                 "supplier": assessment.supplier, "original_payload": assessment.payload},
        assignee=services.on_call_user(Tier.T1),
    )
    assessment.incident = incident
    assessment.status = AssessmentStatus.PROMOTED
    assessment.promoted_at = incident.created_at
    assessment.save(update_fields=["incident", "status", "promoted_at", "updated_at"])
    append(assessment, "incident_promoted", f"Promoted to {incident.number}: {reason.strip()}", actor,
           {"incident_id": str(incident.pk), "evidence_ids": evidence,
            "policy_reason": policy_reason.strip(), "incident_clock_started_at": incident.created_at.isoformat()})
    # All durable writes above precede engine dispatch. The execution name is incident-id based;
    # failed dispatch is explicit and retryable, never a silent success or a second incident.
    if dispatch:
        # Keep the existing outbound contract; exercise snapshots deliberately skip dispatch.
        events.emit("incident.created", {
            "incident_id": str(incident.pk), "number": incident.number,
            "title": incident.title, "source": incident.source,
        })
        transaction.on_commit(lambda: start_engine(assessment.pk))
    else:
        incident.escalation_execution_arn = f"local:exercise:{incident.pk}"
        incident.save(update_fields=["escalation_execution_arn", "updated_at"])
        append(assessment, "exercise_loaded", "Local exercise snapshot; paging and external execution were not dispatched.")
    return incident, True


@transaction.atomic
def start_engine(pk, *, force=False):
    from . import escalation, services

    assessment = Assessment.objects.get(pk=pk)
    if not assessment.incident_id:
        return
    # Lock the same row as lifecycle writers. Locking a nullable outer join fails
    # on Postgres, and locking Assessment before Incident would invert their order.
    incident = Incident.objects.select_for_update().get(pk=assessment.incident_id)
    if incident.escalation_execution_arn or incident.status != "OPEN" or incident.current_tier != Tier.T1:
        return
    assessment = Assessment.objects.select_for_update().get(pk=pk)
    now = timezone.now()
    if not force and assessment.engine_start_retry_at and assessment.engine_start_retry_at > now:
        return
    try:
        arn = escalation.start_escalation(incident)
    except Exception as exc:
        assessment.engine_start_failures += 1
        delay = min(900, 30 * 2 ** min(assessment.engine_start_failures - 1, 5))
        assessment.engine_start_retry_at = now + timedelta(seconds=delay)
        assessment.save(update_fields=["engine_start_failures", "engine_start_retry_at"])
        if assessment.engine_start_failures == 1 and not assessment.history.filter(kind="escalation_start_failed").exists():
            append(assessment, "escalation_start_failed", "Incident created; escalation engine could not start. Automatic retries back off; an operator can retry now.",
                   data={"error_type": type(exc).__name__, "retry_at": assessment.engine_start_retry_at.isoformat()})
        logger.warning("Escalation start failed incident=%s error_type=%s retry_at=%s", incident.pk,
                       type(exc).__name__, assessment.engine_start_retry_at.isoformat())
        return
    assessment.engine_start_retry_at = None
    assessment.save(update_fields=["engine_start_retry_at"])
    incident.escalation_execution_arn = arn
    incident.save(update_fields=["escalation_execution_arn", "updated_at"])
    append(assessment, "escalation_started", "Incident escalation engine started at T1.",
           data={"attempts": assessment.engine_start_failures + 1})
    if settings.ESCALATION_LOCAL_MODE:
        services.record_tier_token(incident.pk, "T1", f"local:{incident.pk}:T1")
    services.page_on_tier_entry(incident, Tier.T1)


def mirror(record, *, kind, body, actor, data):
    """Existing lifecycle writes append snapshots only for promoted assessments."""
    if not isinstance(record, Incident):
        return
    assessment = Assessment.objects.filter(incident_id=record.pk).first()
    if assessment:
        append(assessment, kind, body, str(actor) if actor is not None else "", {"source_event": data})
