"""Evidence, operational actions, specialist handoffs, and source-linked RCA enrichment."""
import hashlib
import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import assessments
from .models import (
    Assessment, AssessmentEvent, CuratedEvent, EvidenceAttachment, Incident, RcaProjection,
    RemediationTask, SmeEngagement,
)
from .permissions import can_act_on, user_tier_rank


def authorize(row, user):
    allowed = can_act_on(user, row.incident) if row.incident_id else user_tier_rank(user) >= 0
    if not allowed:
        raise PermissionError("Your responder role does not permit this action at the current tier.")


def lock_case(pk, user, *, participant=False):
    initial = Assessment.objects.get(pk=pk)
    if initial.incident_id:
        Incident.objects.select_for_update().get(pk=initial.incident_id)
    row = Assessment.objects.select_for_update().get(pk=pk)
    if not (participant and (row.engagements.filter(target=user).exists() or row.engagements.filter(area__members=user).exists() or user_tier_rank(user) >= 0)):
        authorize(row, user)
    return row


def evidence_ids(row, ids):
    ids = set(map(int, ids))
    valid = set(row.history.filter(pk__in=ids).values_list("pk", flat=True))
    if valid != ids:
        raise ValueError("All selected evidence must belong to this assessment/incident history.")
    return sorted(valid)


@transaction.atomic
def record_action(pk, *, user, kind, body, selected=()):
    row = lock_case(pk, user)
    if kind not in {"hypothesis", "decision", "operational_action", "mitigation", "recovery", "bridge_opened"} or not body.strip():
        raise ValueError("Choose an action type and record what happened.")
    return assessments.append(row, kind, body.strip(), user.username,
                              {"evidence_ids": evidence_ids(row, selected), "execution": "operator_recorded"})


@transaction.atomic
def upload(pk, *, user, file, description, images_only=False, record_event=True):
    row = lock_case(pk, user, participant=True)
    content = file.read(5 * 1024 * 1024 + 1)
    if not content or len(content) > 5 * 1024 * 1024:
        raise ValueError("Evidence files must be nonempty and at most 5 MB.")
    types = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"%PDF-", "application/pdf"))
    content_type = next((mime for magic, mime in types if content.startswith(magic)), None)
    if content_type is None and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        content_type = "image/webp"
    if content_type is None:
        if b"\x00" in content:
            raise ValueError("Supported evidence: PNG, JPEG, WebP, PDF, or UTF-8 plain text.")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Unsupported evidence file type.") from exc
        content_type = "text/plain"
    if images_only and content_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValueError("Photos must be PNG, JPEG or WebP images.")
    filename = re.sub(r"[^A-Za-z0-9_. -]", "_", file.name.replace("\\", "/").split("/")[-1])[:255] or "evidence"
    attachment = EvidenceAttachment.objects.create(
        assessment=row, uploaded_by=user, filename=filename, content_type=content_type,
        sha256=hashlib.sha256(content).hexdigest(), content=content,
    )
    if record_event:
        assessments.append(row, "evidence_attached", description.strip() or f"Attached {filename}", user.username,
                           {"attachment_id": str(attachment.pk), "filename": filename, "content_type": content_type,
                            "sha256": attachment.sha256, "bytes": len(content)})
    return attachment


@transaction.atomic
def add_history_note(pk, *, user, body="", photos=()):
    """Persist text and its photos as one source event, or roll back the entire entry."""
    row = lock_case(pk, user, participant=True)
    if len(photos) > 8:
        raise ValueError("Attach up to 8 photos per note.")
    if not body.strip() and not photos:
        raise ValueError("Add a note or at least one photo.")
    attachments = []
    for photo in photos:
        item = upload(pk, user=user, file=photo, description="", images_only=True, record_event=False)
        attachments.append({"attachment_id": str(item.pk), "filename": item.filename,
                            "content_type": item.content_type, "sha256": item.sha256, "bytes": photo.size})
    event = assessments.append(row, "comment", body.strip() or "Added photos.", user.username,
                               {"attachments": attachments} if attachments else {})
    row.save(update_fields=["updated_at"])
    return event


@transaction.atomic
def engage(pk, *, user, target=None, area=None, reason, selected, request_id, ack_seconds=300):
    row = lock_case(pk, user)
    if not row.incident_id:
        raise ValueError("Promote to an incident before engaging a specialist.")
    if bool(target) == bool(area) or not reason.strip():
        raise ValueError("Choose one specialist or one subject area, and provide a reason.")
    if area and not area.members.filter(is_active=True).exists():
        raise ValueError("The subject area has no active specialists.")
    if target and not target.is_active:
        raise ValueError("Choose an active specialist.")
    ids = evidence_ids(row, selected)
    engagement, created = SmeEngagement.objects.get_or_create(
        request_id=request_id, defaults={"assessment": row, "target": target, "area": area,
                                         "requested_by": user, "reason": reason.strip(),
                                         "ack_deadline": timezone.now() + timedelta(seconds=ack_seconds)},
    )
    if not created:
        if engagement.assessment_id != row.pk:
            raise ValueError("This handoff identifier belongs to another case.")
        return engagement
    engagement.evidence.set(ids)
    recipients = [target.pk] if target else list(area.members.filter(is_active=True).values_list("pk", flat=True))
    assessments.append(row, "sme_paged", f"Engaged {target.username if target else area.name}: {reason.strip()}", user.username,
                       {"engagement_id": str(engagement.pk), "recipient_ids": recipients, "evidence_ids": ids,
                        "ack_deadline": engagement.ack_deadline.isoformat(), "transport": "in_app",
                        "incident_tier": row.incident.current_tier, "sla_clock_changed": False})
    return engagement


@transaction.atomic
def respond_engagement(pk, *, user, action, body=""):
    item = SmeEngagement.objects.select_for_update().get(pk=pk)
    target = item.target_id == user.pk or (item.area_id and item.area.members.filter(pk=user.pk).exists())
    if not (target or user.is_superuser):
        raise PermissionError("Only the paged specialist or an administrator can acknowledge this handoff.")
    if action == "ack":
        if item.acknowledged_at:
            return item
        item.acknowledged_at, item.acknowledged_by, item.state = timezone.now(), user, "acknowledged"
        item.save(update_fields=["acknowledged_at", "acknowledged_by", "state"])
        assessments.append(item.assessment, "sme_acknowledged", f"Specialist acknowledged: {item.reason}", user.username,
                           {"engagement_id": str(item.pk), "late": item.acknowledged_at > item.ack_deadline})
    elif action == "complete":
        if item.completed_at:
            return item
        if not item.acknowledged_at or not body.strip():
            raise ValueError("Acknowledge the handoff and record the specialist action before completing it.")
        item.completed_at, item.action, item.state = timezone.now(), body.strip(), "completed"
        item.save(update_fields=["completed_at", "action", "state"])
        assessments.append(item.assessment, "sme_action", body.strip(), user.username,
                           {"engagement_id": str(item.pk), "evidence_ids": list(item.evidence.values_list("pk", flat=True))})
    else:
        raise ValueError("Choose acknowledge or complete.")
    return item


@transaction.atomic
def curate(pk, *, user, event, section, note="", remove=False):
    row = lock_case(pk, user)
    if not row.incident_id:
        raise ValueError("Promote to an incident before curating its RCA.")
    evidence_ids(row, [event.pk])
    if section not in dict(CuratedEvent._meta.get_field("section").choices):
        raise ValueError("Choose an RCA section.")
    projection, _ = RcaProjection.objects.get_or_create(assessment=row)
    if remove:
        CuratedEvent.objects.filter(projection=projection, event=event, section=section).delete()
    else:
        CuratedEvent.objects.update_or_create(projection=projection, event=event, section=section, defaults={"note": note})
    if projection.state == "final":
        projection.state = "review"
        projection.save(update_fields=["state", "updated_at"])
    assessments.append(row, "rca_selection_changed", f"{'Removed from' if remove else 'Selected for'} RCA {section}: event {event.pk}",
                       user.username, {"source_event_id": event.pk, "section": section, "note": note})
    return projection


@transaction.atomic
def save_rca(pk, *, user, **data):
    row = lock_case(pk, user)
    if not row.incident_id:
        raise ValueError("RCA is available after incident promotion.")
    projection, _ = RcaProjection.objects.get_or_create(assessment=row)
    for key in ("summary", "impact", "root_cause", "contributing_factors", "detection_gaps", "state"):
        if key in data:
            setattr(projection, key, data[key])
    if projection.state not in {"draft", "review", "final"}:
        raise ValueError("Choose a valid RCA state.")
    if projection.state == "final" and (row.incident.status != "RESOLVED" or not projection.summary.strip()
                                       or not projection.root_cause.strip() or not projection.selections.exists()):
        raise ValueError("Final RCA requires a resolved incident, summary, root cause, and selected source evidence.")
    projection.save()
    assessments.append(row, "rca_updated", f"RCA {projection.get_state_display().lower()} saved.", user.username,
                       {"projection_id": projection.pk, "state": projection.state,
                        "summary": projection.summary, "impact": projection.impact,
                        "root_cause": projection.root_cause, "contributing_factors": projection.contributing_factors,
                        "detection_gaps": projection.detection_gaps})
    return projection


@transaction.atomic
def remediation(pk, *, user, title, owner, due_date):
    row = lock_case(pk, user)
    if not title.strip() or not owner.is_active:
        raise ValueError("A corrective action and active owner are required.")
    task = RemediationTask.objects.create(assessment=row, title=title.strip(), owner=owner, due_date=due_date)
    assessments.append(row, "remediation_created", f"{title.strip()} — owner {owner.username}, due {due_date}", user.username,
                       {"task_id": str(task.pk), "owner_id": owner.pk, "due_date": str(due_date)})
    return task


@transaction.atomic
def update_remediation(pk, *, user, state, verification=""):
    initial = RemediationTask.objects.get(pk=pk)
    if initial.owner_id == user.pk:
        row = Assessment.objects.select_for_update().get(pk=initial.assessment_id)
    else:
        row = lock_case(initial.assessment_id, user)
    task = RemediationTask.objects.select_for_update().get(pk=pk)
    if state not in {"open", "in_progress", "done"} or (state == "done" and not verification.strip()):
        raise ValueError("Completed corrective actions require verification evidence.")
    task.state, task.verification = state, verification.strip()
    task.completed_at = timezone.now() if state == "done" else None
    task.save(update_fields=["state", "verification", "completed_at"])
    assessments.append(row, "remediation_updated", f"{task.title}: {task.get_state_display()}. {verification}", user.username,
                       {"task_id": str(task.pk), "state": state, "verification": verification})
    return task


def rca_markdown(row):
    projection = getattr(row, "rca_projection", None)
    lines = [f"# RCA — {row.number} · {row.title}", "", f"Status: {projection.state if projection else 'draft'}", ""]
    for label, key in (("Summary", "summary"), ("Impact", "impact"), ("Root cause", "root_cause"),
                       ("Contributing factors", "contributing_factors"), ("Detection gaps", "detection_gaps")):
        lines += [f"## {label}", "", getattr(projection, key, "") or "Not yet established.", ""]
    lines += ["## Curated source timeline", ""]
    selections = projection.selections.select_related("event").order_by("event__occurred_at", "event_id") if projection else []
    for selection in selections:
        event = selection.event
        lines += [f"- {event.occurred_at.isoformat()} · {selection.section} · event {event.pk}: {event.body}",
                  f"  Source: /ui/assessments/{row.pk}/?event={event.pk}#event-{event.pk}"]
        if selection.note:
            lines.append(f"  Reviewer note: {selection.note}")
        for photo in event.data.get("attachments", []):
            lines.append(f"  Photo: {photo['filename']} · /ui/evidence/{photo['attachment_id']}/ · SHA-256 {photo['sha256']}")
        if event.kind == "evidence_added":
            lines.append(f"  Check: {event.data.get('query', '')}\n  Result: {event.data.get('result', '')}")
    lines += ["", "## Corrective actions", ""]
    for task in row.remediations.select_related("owner"):
        lines.append(f"- {task.title} · {task.owner.username} · due {task.due_date} · {task.state}: {task.verification}")
    return "\n".join(lines) + "\n"
