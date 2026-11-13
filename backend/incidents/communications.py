"""Versioned review windows and transactional local delivery. No SMTP/network send."""
from datetime import timedelta
from email.message import EmailMessage

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from .assessments import AssessmentConflict, append
from .models import Assessment, Communication, CommunicationRevision, OutboxMessage


def validate_recipients(recipients):
    if isinstance(recipients, str):
        recipients = recipients.replace(";", ",").split(",")
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 30:
        raise ValueError("Provide between 1 and 30 recipient email addresses.")
    cleaned = []
    for address in recipients:
        if not isinstance(address, str):
            raise ValueError("Each recipient must be an email address.")
        try:
            validate_email(address.strip())
        except ValidationError as exc:
            raise ValueError("Each recipient must be a valid email address.") from exc
        if address.strip().lower() not in cleaned:
            cleaned.append(address.strip().lower())
    return cleaned


def _lock(pk):
    identity = Communication.objects.only("assessment_id").get(pk=pk)
    assessment = Assessment.objects.select_for_update().get(pk=identity.assessment_id)
    communication = Communication.objects.select_for_update().get(pk=pk)
    return assessment, communication


def _version(communication, expected):
    if communication.revision != expected:
        raise AssessmentConflict("This draft changed. Refresh before acting on it.")


def _content(subject, body, recipients):
    if not subject.strip() or len(subject) > 512 or "\n" in subject or "\r" in subject:
        raise ValueError("Provide a single-line subject of at most 512 characters.")
    if not body.strip() or len(body) > 50000:
        raise ValueError("Provide a message of at most 50,000 characters.")
    return subject.strip(), body.strip(), validate_recipients(recipients)


@transaction.atomic
def draft(pk, *, actor, subject, body, recipients, review_seconds=300, sla_seconds=900,
          auto_send=False, purpose="update", sla_deadline=None):
    assessment = Assessment.objects.select_for_update().get(pk=pk)
    subject, body, recipients = _content(subject, body, recipients)
    if not 1 <= review_seconds <= 86400 or not 1 <= sla_seconds <= 604800:
        raise ValueError("Choose a positive review window and SLA within the supported range.")
    if purpose not in {"initial", "update"}:
        raise ValueError("Choose initial response or status update.")
    now = timezone.now()
    # Initial response starts at signal receipt; later updates start at drafting.
    deadline = sla_deadline or ((assessment.created_at if purpose == "initial" else now) + timedelta(seconds=sla_seconds))
    row = Communication.objects.create(
        assessment=assessment, purpose=purpose, auto_send=auto_send,
        review_deadline=min(now + timedelta(seconds=review_seconds), deadline), sla_deadline=deadline,
    )
    CommunicationRevision.objects.create(communication=row, version=1, subject=subject, body=body,
                                          recipients=recipients, actor=actor, auto_send_approved=auto_send)
    append(assessment, "communication_drafted", f"Drafted: {subject}", actor,
           {"communication_id": str(row.pk), "revision": 1, "recipients": recipients,
            "auto_send_approved": auto_send})
    append(assessment, "review_window_started", "Human communication review window started.", actor,
           {"communication_id": str(row.pk), "review_deadline": row.review_deadline.isoformat(),
            "sla_deadline": deadline.isoformat()})
    return row


@transaction.atomic
def edit(pk, *, actor, expected_version, subject, body, recipients, auto_send):
    assessment, row = _lock(pk)
    _version(row, expected_version)
    if row.state != "review" or timezone.now() >= row.review_deadline:
        raise AssessmentConflict("The review window has closed. This draft can no longer be edited.")
    subject, body, recipients = _content(subject, body, recipients)
    row.revision += 1
    row.auto_send = auto_send
    CommunicationRevision.objects.create(communication=row, version=row.revision, subject=subject,
                                          body=body, recipients=recipients, actor=actor,
                                          auto_send_approved=auto_send)
    row.save(update_fields=["revision", "auto_send"])
    append(assessment, "communication_edited", f"Draft revision {row.revision} saved; deadline unchanged.", actor,
           {"communication_id": str(row.pk), "revision": row.revision, "auto_send_approved": auto_send})
    return row


@transaction.atomic
def suppress(pk, *, actor, expected_version, reason):
    assessment, row = _lock(pk)
    _version(row, expected_version)
    if not reason.strip():
        raise ValueError("A suppression reason is required.")
    if row.state == "suppressed":
        return row
    if row.state != "review" or timezone.now() >= row.review_deadline:
        raise AssessmentConflict("This communication has already been sent or its review window has closed.")
    row.state, row.suppression_reason = "suppressed", reason.strip()
    row.save(update_fields=["state", "suppression_reason"])
    append(assessment, "communication_suppressed", reason.strip(), actor,
           {"communication_id": str(row.pk), "revision": row.revision})
    return row


@transaction.atomic
def send(pk, *, actor, expected_version=None, automatic=False):
    assessment, row = _lock(pk)
    if expected_version is not None:
        _version(row, expected_version)
    if row.state == "sent":
        return row
    if row.state not in {"review", "expired"}:
        raise AssessmentConflict("Suppressed communications cannot be sent.")
    revision = row.current
    if automatic:
        if row.state != "review" or timezone.now() < row.review_deadline:
            return row
        append(assessment, "review_window_expired", "Communication review window expired without intervention.",
               data={"communication_id": str(row.pk), "revision": row.revision})
        if not (row.auto_send and revision.auto_send_approved and settings.WATCH_AUTO_SEND_ENABLED):
            row.state = "expired"
            row.save(update_fields=["state"])
            append(assessment, "communication_expired", "Automatic delivery is not authorized; manual action required.",
                   data={"communication_id": str(row.pk)})
            return row
    if settings.WATCH_DELIVERY_MODE != "local":
        raise ValueError("Only the local outbox is enabled. No external email transport is configured.")
    now = timezone.now()
    OutboxMessage.objects.get_or_create(communication=row, defaults={"revision": revision, "delivered_at": now})
    row.state, row.sent_at, row.sent_by, row.is_automatic = "sent", now, actor, automatic
    row.save(update_fields=["state", "sent_at", "sent_by", "is_automatic"])
    append(assessment, "communication_auto_sent" if automatic else "communication_sent",
           f"Delivered to the local outbox: {revision.subject}", actor,
           {"communication_id": str(row.pk), "revision": revision.version, "transport": "local",
            "sla_met": row.sla_met, "sla_deadline": row.sla_deadline.isoformat(), "sent_at": now.isoformat()})
    return row


def draft_text(assessment):
    scope = f"{assessment.customer or 'the affected customer'} / {assessment.service or 'the reported service'}"
    if assessment.verdict == "no_issue":
        lead = f"We have assessed the reported concern for {scope}. The configured checks found no evidence of degradation."
    elif assessment.verdict == "degraded":
        lead = f"We have confirmed {assessment.get_impact_display().lower()} customer impact for {scope}."
    else:
        lead = f"We are assessing the reported concern for {scope}. Customer impact remains inconclusive."
    supplier = f"\nSupplier context: {assessment.supplier}. Supplier status is being assessed against customer-specific evidence." if assessment.supplier else ""
    return f"Update: {assessment.title}", f"{lead}{supplier}\n\n{assessment.conclusion}\n\nWe will provide another update when there is a material change.\n\nWATCH Operations"


def message_bytes(receipt):
    revision = receipt.revision
    message = EmailMessage()
    message["Subject"] = revision.subject
    message["To"] = ", ".join(revision.recipients)
    message["From"] = "watch@local.example.test"
    message["Message-ID"] = f"<{receipt.pk}@local.example.test>"
    message["X-Watch-Delivery"] = "local-outbox"
    message.set_content(revision.body + "\n\nLocal delivery preview. No external email was transmitted.")
    return message.as_bytes()
