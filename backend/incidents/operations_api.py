"""The assessment workspace over the API (ADR-056), so `wt` can drive everything the browser can.

Workspace actions reuse the UI's own forms and handlers (`operations_ui.WORKSPACE_ACTIONS`), so the
API and the page cannot drift: same validation, same permissions, same history events. Callers send
plain field names (`{"body": "…"}`); the form prefix is added here. Users are accepted by username
where a form expects a user (`target`, `owner`).
"""
from datetime import timedelta

from django import forms
from django.contrib.auth.models import User
from django.db import transaction
from django.http import QueryDict
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.datastructures import MultiValueDict
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import assessments, communications, operations, reporting, services
from .models import (
    Communication, DiagnosticRule, MaintenanceWindow, OutboxMessage, RemediationTask, SmeEngagement,
    SubjectArea,
)
from .operations_forms import RevisionForm
from .permissions import user_tier_rank
from .operations_ui import (
    PARTICIPANT_ACTIONS, WORKSPACE_ACTIONS, _can_manage_case, _forms, participates,
)

USER_FIELDS = {"target", "owner"}
COMMUNICATION_ACTIONS = {"edit", "send", "suppress"}


# --- serialization -------------------------------------------------------------------------------

def _iso(value):
    return value.isoformat() if value else None


def events_json(rows) -> list[dict]:
    rows = list(rows)
    names = services.actor_labels(row.actor for row in rows)
    return [{"id": e.pk, "kind": e.kind, "actor": e.actor, "actor_name": names.get(e.actor, e.actor),
             "body": e.body, "data": e.data, "occurred_at": _iso(e.occurred_at),
             "recorded_at": _iso(e.recorded_at)} for e in rows]


def communication_json(c: Communication, revisions=False) -> dict:
    current = c.current
    out = {"id": str(c.id), "assessment": c.assessment.number, "purpose": c.purpose,
           "state": c.state, "revision": c.revision, "auto_send": c.auto_send,
           "subject": current.subject, "recipients": current.recipients,
           "review_deadline": _iso(c.review_deadline), "sla_deadline": _iso(c.sla_deadline),
           "sent_at": _iso(c.sent_at), "sent_by": c.sent_by, "is_automatic": c.is_automatic,
           "suppression_reason": c.suppression_reason}
    if revisions:
        out["body"] = current.body
        out["revisions"] = [{"version": r.version, "subject": r.subject, "body": r.body,
                             "recipients": r.recipients, "actor": r.actor,
                             "auto_send_approved": r.auto_send_approved,
                             "created_at": _iso(r.created_at)} for r in c.revisions.all()]
    return out


def engagement_json(e: SmeEngagement) -> dict:
    return {"id": str(e.id), "assessment": e.assessment.number, "state": e.state,
            "target": e.target.username if e.target else None,
            "area": e.area.name if e.area else None, "requested_by": e.requested_by.username,
            "reason": e.reason, "evidence": [ev.pk for ev in e.evidence.all()],
            "paged_at": _iso(e.paged_at), "ack_deadline": _iso(e.ack_deadline),
            "acknowledged_at": _iso(e.acknowledged_at),
            "acknowledged_by": e.acknowledged_by.username if e.acknowledged_by else None,
            "completed_at": _iso(e.completed_at), "action": e.action}


def remediation_json(t: RemediationTask) -> dict:
    return {"id": str(t.id), "assessment": t.assessment.number, "title": t.title,
            "owner": t.owner.username, "due_date": _iso(t.due_date), "state": t.state,
            "verification": t.verification, "completed_at": _iso(t.completed_at)}


def rca_json(row) -> dict:
    projection = getattr(row, "rca_projection", None)
    fields = ("summary", "impact", "root_cause", "contributing_factors", "detection_gaps", "state")
    return {
        "assessment": row.number,
        **({name: getattr(projection, name) for name in fields} if projection else {"state": None}),
        "selections": [{"event": s.event_id, "section": s.section, "note": s.note}
                       for s in (projection.selections.order_by("event__occurred_at")
                                 if projection else [])],
        "markdown": operations.rca_markdown(row),
    }


# --- workspace actions ---------------------------------------------------------------------------

def _username_to_pk(value):
    text = str(value)
    if text.isdigit():
        return text
    user = User.objects.filter(username=text).first()
    return str(user.pk) if user else text  # unknown names fail form validation as usual


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def prefixed(prefix: str, data, files=None) -> tuple[QueryDict, MultiValueDict]:
    """Plain API fields → the prefixed form data the UI posts (`body` → `comment-body`)."""
    out = QueryDict(mutable=True)
    items = data.lists() if hasattr(data, "lists") else data.items()
    for key, value in items:
        values = value if isinstance(value, list) else [value]
        if key in USER_FIELDS:
            values = [_username_to_pk(v) for v in values]
        out.setlist(f"{prefix}-{key}", [_scalar(v) for v in values if v is not None])
    upload = MultiValueDict()
    for key in (files or {}):
        upload.setlist(f"{prefix}-{key}", files.getlist(key))
    return out, upload


def with_page_defaults(unbound, payload):
    """Fill fields the caller left out with what the page pre-fills (initial values: review windows,
    the drafted email text, a fresh engagement request id, a select's first option). Checkboxes and files stay as sent:
    an unticked box and no upload are real answers."""
    for name, field in unbound.fields.items():
        if name in payload or isinstance(field, (forms.BooleanField, forms.FileField)):
            continue
        initial = unbound[name].initial
        if initial in (None, "") and type(field) is forms.ChoiceField and field.choices:
            initial = field.choices[0][0]  # a <select> shows its first option
        if initial not in (None, ""):
            payload[name] = initial
    return payload


def _error(message, code, **extra):
    return Response({"detail": message, **extra}, status=code)


def _guarded(fn):
    """Map the domain's exceptions to HTTP, as the UI does."""
    try:
        return fn()
    except PermissionError as exc:
        return _error(str(exc), status.HTTP_403_FORBIDDEN)
    except assessments.AssessmentConflict as exc:
        return _error(str(exc), status.HTTP_409_CONFLICT)
    except ValueError as exc:
        return _error(str(exc), status.HTTP_400_BAD_REQUEST)


def run_action(request, row, name):
    """POST /api/assessments/<n>/actions/<name>/ — one workspace action."""
    if name not in WORKSPACE_ACTIONS:
        return _error(f"Unknown action {name!r}; choose one of: {', '.join(sorted(WORKSPACE_ACTIONS))}.",
                      status.HTTP_400_BAD_REQUEST)
    can_participate = name in PARTICIPANT_ACTIONS and participates(row, request.user)
    if not (_can_manage_case(request.user, row) or can_participate):
        return _error("Your responder role does not permit this action.", status.HTTP_403_FORBIDDEN)
    payload = request.data.copy() if hasattr(request.data, "lists") else dict(request.data)
    data, files = prefixed(name, with_page_defaults(_forms(row)[name], payload), request.FILES)
    form = _forms(row, data, files, name)[name]
    if not form.is_valid():
        return _error("Check the fields.", status.HTTP_400_BAD_REQUEST, errors=form.errors.get_json_data())
    before = row.history.order_by("-pk").values_list("pk", flat=True).first() or 0

    def act():
        WORKSPACE_ACTIONS[name](request, row.pk, row, form.cleaned_data, request.user.username)
        row.refresh_from_db()
        return Response({"assessment": row.number, "status": row.status,
                         "incident": row.incident.number if row.incident_id else None,
                         "events": events_json(row.history.filter(pk__gt=before).order_by("pk"))})

    return _guarded(act)


# --- communications, engagements, remediations ---------------------------------------------------

@api_view(["GET"])
def communication_detail(request, pk):
    row = get_object_or_404(Communication.objects.select_related("assessment"), pk=pk)
    return Response(communication_json(row, revisions=True))


def _apply_communication(request, row, name):
    actor, data = request.user.username, request.data
    if name == "send":
        communications.send(row.pk, actor=actor, expected_version=int(data.get("expected_version", 0)))
        return None
    if name == "suppress":
        communications.suppress(row.pk, actor=actor, reason=data.get("reason", ""),
                                expected_version=int(data.get("expected_version", 0)))
        return None
    form = RevisionForm(prefixed("draft", data)[0], prefix="draft")
    if not form.is_valid():
        return _error("Check the draft fields.", status.HTTP_400_BAD_REQUEST,
                      errors=form.errors.get_json_data())
    communications.edit(row.pk, actor=actor, **form.cleaned_data)
    return None


@api_view(["POST"])
def communication_action(request, pk, name):
    """edit (new revision) / send / suppress, each pinned to `expected_version`."""
    row = get_object_or_404(Communication.objects.select_related("assessment"), pk=pk)
    if name not in COMMUNICATION_ACTIONS:
        return _error("Choose edit, send, or suppress.", status.HTTP_400_BAD_REQUEST)
    if not _can_manage_case(request.user, row.assessment):
        return _error("Incident tier permissions are required to send, edit, or suppress this "
                      "communication.", status.HTTP_403_FORBIDDEN)

    def act():
        with transaction.atomic():
            operations.lock_case(row.assessment_id, request.user)
            failure = _apply_communication(request, row, name)
        if failure is not None:
            return failure
        row.refresh_from_db()
        return Response(communication_json(row, revisions=True))

    return _guarded(act)


@api_view(["POST"])
def engagement_respond(request, pk):
    """`{"action": "ack" | "complete", "body": "…"}` — as the paged specialist."""
    get_object_or_404(SmeEngagement, pk=pk)

    def act():
        operations.respond_engagement(pk, user=request.user, action=request.data.get("action"),
                                      body=request.data.get("body", ""))
        return Response(engagement_json(SmeEngagement.objects.get(pk=pk)))

    return _guarded(act)


@api_view(["POST"])
def remediation_update(request, pk):
    """`{"state": "open" | "in_progress" | "done", "verification": "…"}`."""
    get_object_or_404(RemediationTask, pk=pk)

    def act():
        operations.update_remediation(pk, user=request.user, state=request.data.get("state"),
                                      verification=request.data.get("verification", ""))
        return Response(remediation_json(RemediationTask.objects.get(pk=pk)))

    return _guarded(act)


# --- reference data and read-only views ----------------------------------------------------------

@api_view(["GET"])
def diagnostic_rules(request):
    return Response([{"id": r.pk, "name": r.name, "provider": r.provider,
                      "comparator": r.comparator, "threshold": r.threshold}
                     for r in DiagnosticRule.objects.order_by("name")])


@api_view(["GET"])
def subject_areas(request):
    return Response([{"id": a.pk, "name": a.name, "description": a.description,
                      "members": [m.username for m in a.members.all()]}
                     for a in SubjectArea.objects.prefetch_related("members").order_by("name")])


@api_view(["GET"])
def outbox(request):
    receipts = OutboxMessage.objects.select_related("revision", "communication__assessment")
    return Response([{"id": str(r.id), "communication": str(r.communication_id),
                      "assessment": r.communication.assessment.number,
                      "subject": r.revision.subject, "recipients": r.revision.recipients,
                      "version": r.revision.version, "delivered_at": _iso(r.delivered_at)}
                     for r in receipts.order_by("-delivered_at")[:100]])


@api_view(["GET"])
def reports(request):
    try:
        days = min(365, max(1, int(request.query_params.get("days", "30"))))
    except ValueError:
        return _error("days must be a number.", status.HTTP_400_BAD_REQUEST)
    filters = {key: request.query_params.get(key, "")[:128]
               for key in ("customer", "service", "supplier", "severity")}
    return Response(reporting.summarize(
        days=days, include_examples=request.query_params.get("examples") == "1", **filters))


# --- maintenance windows (ADR-064) ----------------------------------------------------------------

def _window_json(w):
    return {"id": w.pk, "supplier": w.supplier, "customer": w.customer, "service": w.service,
            "starts_at": _iso(w.starts_at), "ends_at": _iso(w.ends_at), "reason": w.reason,
            "created_by": w.created_by}


def _window_errors(data):
    from django.utils.dateparse import parse_datetime

    errors = {}
    starts = parse_datetime(str(data.get("starts_at") or "")) if data.get("starts_at") else timezone.now()
    ends = parse_datetime(str(data.get("ends_at") or ""))
    if not any(str(data.get(k) or "").strip() for k in ("supplier", "customer", "service")):
        errors["scope"] = "Name a supplier, or a customer and/or service."
    if starts is None:
        errors["starts_at"] = "Use an ISO 8601 time, e.g. 2026-11-20T02:00:00Z."
    if ends is None:
        errors["ends_at"] = "Use an ISO 8601 time, e.g. 2026-11-20T04:00:00Z."
    elif starts is not None and not timedelta(0) < ends - starts <= timedelta(days=7):
        errors["ends_at"] = "End after the start, within 7 days."
    if not str(data.get("reason") or "").strip():
        errors["reason"] = "Say what the planned work is."
    return errors, starts, ends


@api_view(["GET", "POST"])
def maintenance_windows(request):
    """GET: current and upcoming windows. POST: plan one (any responder)."""
    if request.method == "GET":
        rows = MaintenanceWindow.objects.filter(ends_at__gt=timezone.now()).order_by("starts_at")
        return Response([_window_json(w) for w in rows])
    if user_tier_rank(request.user) < 0:
        return _error("A responder role is required to plan maintenance.", status.HTTP_403_FORBIDDEN)
    errors, starts, ends = _window_errors(request.data)
    if errors:
        return _error("Check the fields.", status.HTTP_400_BAD_REQUEST,
                      errors={k: [{"message": v}] for k, v in errors.items()})
    window = MaintenanceWindow.objects.create(
        starts_at=starts, ends_at=ends, reason=str(request.data["reason"]).strip()[:512],
        created_by=request.user.username,
        **{k: str(request.data.get(k) or "").strip()[:128] for k in ("supplier", "customer", "service")})
    return Response(_window_json(window), status=status.HTTP_201_CREATED)
