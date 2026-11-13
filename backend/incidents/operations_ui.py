import uuid

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from . import assessments, automation, communications, operations, reporting, services
from .assessment_forms import ConclusionForm, EvidenceForm, PromotionForm, ReasonForm
from .models import (
    Assessment, AssessmentPolicy, Communication, DiagnosticRule, DiagnosticRun, EvidenceAttachment,
    OperationsHeartbeat, OutboxMessage, RcaProjection, RemediationTask, ServiceDependency,
    SmeEngagement, SubjectArea,
)
from .operations_forms import (
    AttachmentForm, CommunicationForm, ContextForm, CurateForm, DependencyForm, DiagnosticRuleForm,
    OperationalActionForm, PhotoNoteForm, PolicyForm, QueryForm, RcaForm, RemediationForm, RevisionForm, SmeForm, SubjectAreaForm,
)
from .permissions import can_act_on, user_tier_rank


def participates(row, user):
    if user_tier_rank(user) >= 0:
        return True
    return row.engagements.filter(target=user).exists() or row.engagements.filter(area__members=user).exists()


def _forms(row, data=None, files=None, action=None):
    classes = {"evidence": EvidenceForm, "conclusion": ConclusionForm, "monitor": ReasonForm,
               "no_issue": ReasonForm, "promote": PromotionForm, "comment": PhotoNoteForm,
               "retry_engine": ReasonForm, "retry_automation": ReasonForm, "email": CommunicationForm, "query": QueryForm,
               "attachment": AttachmentForm, "operation": OperationalActionForm, "engage": SmeForm,
               "curate": CurateForm, "rca": RcaForm, "remediation": RemediationForm, "claim": ReasonForm, "context": ContextForm}
    subject, body = communications.draft_text(row)
    initial = {"context": {"severity": row.severity, "impact": row.impact}, "email": {"subject": subject, "body": body}, "engage": {"request_id": uuid.uuid4()}}
    projection = getattr(row, "rca_projection", None)
    if projection:
        initial["rca"] = {name: getattr(projection, name) for name in RcaForm.base_fields}
    forms = {}
    for key, cls in classes.items():
        form = cls(data if action == key else None, files if action == key else None,
                   prefix=key, initial=initial.get(key, {}))
        for field in ("selected", "event"):
            if field in form.fields:
                form.fields[field].queryset = row.history.all()
        forms[key] = form
    return forms


@login_required
@require_http_methods(["GET", "POST"])
def workspace(request, pk):
    row = get_object_or_404(Assessment.objects.select_related("assignee", "incident"), pk=pk)
    action = request.POST.get("action") if request.method == "POST" else None
    forms = _forms(row, request.POST if action else None, request.FILES if action else None, action)
    can_manage = can_act_on(request.user, row.incident) if row.incident_id else user_tier_rank(request.user) >= 0
    can_participate = participates(row, request.user)
    error, code = "", 200
    if action:
        participant_actions = {"comment", "attachment", "query", "evidence"}
        if not (can_manage or (action in participant_actions and can_participate)):
            return HttpResponseForbidden("Your responder role does not permit this action.")
        if action not in forms:
            error, code = "Choose a valid action.", 400
        elif not forms[action].is_valid():
            error, code = "Check the highlighted fields.", 400
        else:
            data = forms[action].cleaned_data
            try:
                actor = request.user.username
                if action == "comment":
                    operations.add_history_note(pk, user=request.user, **data)
                elif action == "evidence":
                    if row.incident_id:
                        assessments.append(row, "evidence_added", data["summary"], actor,
                                           {"query": data["query"], "result": data["result"], "source": data["source"], "execution": "operator_recorded"})
                    else:
                        assessments.add_evidence(pk, actor=actor, **data)
                elif action == "conclusion":
                    assessments.conclude(pk, actor=actor, **data)
                elif action in {"monitor", "no_issue"}:
                    assessments.dispose(pk, actor=actor, outcome="monitoring" if action == "monitor" else "no_issue", **data)
                elif action == "promote":
                    incident, _ = assessments.promote(pk, actor=actor, **data)
                    return redirect("ui:incident_detail", pk=incident.pk)
                elif action == "retry_automation":
                    with transaction.atomic():
                        current = operations.lock_case(pk, request.user)
                        if current.status in {"no_issue", "promoted"} or current.automation_state != "failed":
                            raise ValueError("Only failed, open automated assessments can be retried.")
                        current.automation_state = "manual"
                        current.save(update_fields=["automation_state", "updated_at"])
                        if not automation.schedule(current):
                            raise ValueError("Configure an active matching policy before retrying.")
                        assessments.append(current, "automation_retry_requested", data["reason"], actor)
                elif action == "retry_engine":
                    operations.authorize(Assessment.objects.select_related("incident").get(pk=pk), request.user)
                    assessments.comment(pk, actor=actor, body=f"Requested escalation retry: {data['reason']}")
                    assessments.start_engine(pk)
                elif action == "email":
                    with transaction.atomic():
                        operations.lock_case(pk, request.user)
                        mail = communications.draft(pk, actor=actor, **data)
                    return redirect("ui:communication_detail", pk=mail.pk)
                elif action == "operation":
                    data["selected"] = [e.pk for e in data["selected"]]
                    operations.record_action(pk, user=request.user, **data)
                elif action == "attachment":
                    operations.upload(pk, user=request.user, **data)
                elif action == "query":
                    with transaction.atomic():
                        current = Assessment.objects.select_for_update().get(pk=pk)
                        assessments.invalidate_conclusion(current)
                        run = DiagnosticRun.objects.create(assessment=current, rule=data["rule"])
                        assessments.append(current, "query_queued", f"Queued saved check: {data['rule'].name}", actor, {"run_id": str(run.pk)})
                elif action == "engage":
                    data["selected"] = [e.pk for e in data["selected"]]
                    operations.engage(pk, user=request.user, **data)
                elif action == "curate":
                    operations.curate(pk, user=request.user, **data)
                elif action == "rca":
                    operations.save_rca(pk, user=request.user, **data)
                elif action == "remediation":
                    operations.remediation(pk, user=request.user, **data)
                elif action == "context":
                    with transaction.atomic():
                        current = operations.lock_case(pk, request.user)
                        current.severity, current.impact = data["severity"], data["impact"]
                        current.save(update_fields=["severity", "impact", "updated_at"])
                        assessments.append(current, "impact_classified", data["reason"], actor, {"severity": current.severity, "impact": current.impact})
                elif action == "claim":
                    with transaction.atomic():
                        claimed = operations.lock_case(pk, request.user)
                        claimed.assignee = request.user
                        claimed.save(update_fields=["assignee", "updated_at"])
                        if claimed.incident_id:
                            claimed.incident.assignee = request.user
                            claimed.incident.save(update_fields=["assignee"])
                        assessments.append(claimed, "ownership_changed", f"{request.user.username} took ownership: {data['reason']}", actor)
                tab = "rca" if action in {"curate", "rca", "remediation"} else ("sme" if action == "engage" else "conversation")
                return redirect(reverse("ui:assessment_detail", args=[pk]) + f"?tab={tab}")
            except PermissionError as exc:
                return HttpResponseForbidden(str(exc))
            except assessments.AssessmentConflict as exc:
                error, code = str(exc), 409
            except ValueError as exc:
                error, code = str(exc), 400
    elif request.method == "POST":
        error, code = "Choose a valid action.", 400
    tab = request.GET.get("tab", "conversation")
    if action in {"curate", "rca", "remediation"}:
        tab = "rca"
    elif action == "engage":
        tab = "sme"
    if tab not in {"conversation", "evidence", "communications", "sme", "rca"}:
        tab = "conversation"
    history = row.history.all()
    before = request.GET.get("before", "")
    if before.isdigit():
        history = history.filter(pk__lt=int(before))
    source_event = request.GET.get("event", "")
    if source_event.isdigit():
        history = history.filter(pk=int(source_event))
    lens = request.GET.get("lens", "evidence" if tab == "evidence" else "all")
    if lens == "conversation":
        history = history.filter(kind__in=["comment", "hypothesis", "decision", "incident_promoted", "lifecycle_transition"])
    elif lens == "evidence":
        history = history.filter(Q(kind__in=["evidence_added", "evidence_attached", "query_started", "query_completed", "query_failed"]) | Q(kind="comment", data__has_key="attachments"))
    events = list(history.order_by("-occurred_at", "-pk").prefetch_related("annotations__author")[:500])[::-1]
    older_cursor = events[0].pk if len(events) == 500 else None
    projection = getattr(row, "rca_projection", None)
    current_action = action or request.GET.get("compose", "comment")
    current_form = forms.get(current_action, forms["comment"])
    return render(request, "operations/workspace.html", {
        "assessment": row, "history": events, "forms": forms, "current_form": current_form,
        "older_cursor": older_cursor, "before": before,
        "history_version": row.history.order_by("-pk").values_list("pk", flat=True).first() or 0,
        "case_tabs": [("conversation", "Conversation"), ("evidence", "Evidence"), ("communications", "Communications"), ("sme", "Specialists"), ("rca", "RCA")],
        "composer_actions": [(key, label) for key, label in [("comment", "Note"), ("query", "Run check"), ("attachment", "Upload evidence"), ("evidence", "Record evidence"), ("operation", "Action / hypothesis"), ("email", "Communication"), ("engage", "Engage specialist")] if can_manage or key in {"comment", "query", "attachment", "evidence"}],
        "assessment_decisions": [("conclusion", "Record conclusion"), ("monitor", "Monitor / inconclusive"), ("no_issue", "Close · no issue"), ("promote", "Promote to incident")],
        "compose": current_action if current_action in forms else "comment", "tab": tab, "lens": lens,
        "error": error, "can_manage": can_manage, "can_participate": can_participate,
        "is_open": row.status not in {"no_issue", "promoted"}, "source_event": source_event,
        "evidence_count": row.history.filter(Q(kind__in=["evidence_added", "evidence_attached"]) | Q(kind="comment", data__has_key="attachments")).count(),
        "communications": row.communications.prefetch_related("revisions"),
        "engagements": row.engagements.select_related("target", "area", "acknowledged_by").prefetch_related("evidence"),
        "projection": projection,
        "selections": projection.selections.select_related("event").order_by("event__occurred_at") if projection else [],
        "remediations": row.remediations.select_related("owner"),
        "engine_pending": bool(row.incident_id and row.incident.status == "OPEN" and not row.incident.escalation_execution_arn),
        "pending_communications": row.communications.filter(state__in=["review", "expired"]),
    }, status=code)


@login_required
@require_http_methods(["GET", "POST"])
def communication_detail(request, pk):
    row = get_object_or_404(Communication.objects.select_related("assessment"), pk=pk)
    current = row.current
    initial = {"subject": current.subject, "body": current.body, "recipients": ", ".join(current.recipients),
               "auto_send": row.auto_send, "expected_version": row.revision}
    form = RevisionForm(request.POST if request.POST.get("action") == "edit" else None, prefix="draft", initial=initial)
    error, code = "", 200
    allowed = can_act_on(request.user, row.assessment.incident) if row.assessment.incident_id else user_tier_rank(request.user) >= 0
    if request.method == "POST":
        if not allowed:
            return HttpResponseForbidden("Incident tier permissions are required to send, edit, or suppress this communication.")
        try:
            with transaction.atomic():
                operations.lock_case(row.assessment_id, request.user)
                action = request.POST.get("action")
                if action == "edit":
                    if not form.is_valid():
                        raise ValueError("Check the highlighted draft fields.")
                    communications.edit(pk, actor=request.user.username, **form.cleaned_data)
                elif action in {"send", "suppress"}:
                    version = int(request.POST.get("expected_version", "0"))
                    if action == "send":
                        communications.send(pk, actor=request.user.username, expected_version=version)
                    else:
                        communications.suppress(pk, actor=request.user.username, expected_version=version, reason=request.POST.get("reason", ""))
                else:
                    raise ValueError("Choose edit, send, or suppress.")
            return redirect("ui:communication_detail", pk=pk)
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
        except assessments.AssessmentConflict as exc:
            error, code = str(exc), 409
        except ValueError as exc:
            error, code = str(exc), 400
    return render(request, "operations/communication.html", {
        "communication": row, "current": current, "form": form, "can_manage": allowed,
        "error": error, "revisions": row.revisions.all(), "now": timezone.now(),
    }, status=code)


@login_required
def attachment(request, pk):
    item = get_object_or_404(EvidenceAttachment, pk=pk)
    inline = request.GET.get("inline") == "1" and item.content_type.startswith("image/")
    response = HttpResponse(bytes(item.content), content_type=item.content_type)
    response["Content-Disposition"] = f"{'inline' if inline else 'attachment'}; filename=\"{item.filename}\""
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


@login_required
@require_http_methods(["POST"])
def engagement_action(request, pk):
    item = get_object_or_404(SmeEngagement, pk=pk)
    try:
        operations.respond_engagement(pk, user=request.user, action=request.POST.get("action"), body=request.POST.get("body", ""))
        return redirect(reverse("ui:assessment_detail", args=[item.assessment_id]) + "?tab=sme")
    except PermissionError as exc:
        return HttpResponseForbidden(str(exc))
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)


@login_required
@require_http_methods(["POST"])
def remediation_action(request, pk):
    task = get_object_or_404(RemediationTask, pk=pk)
    try:
        operations.update_remediation(pk, user=request.user, state=request.POST.get("state"), verification=request.POST.get("verification", ""))
        return redirect(reverse("ui:assessment_detail", args=[task.assessment_id]) + "?tab=rca")
    except PermissionError as exc:
        return HttpResponseForbidden(str(exc))
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)


@login_required
def rca_export(request, pk):
    row = get_object_or_404(Assessment, pk=pk)
    response = HttpResponse(operations.rca_markdown(row), content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{row.number}-rca.md"'
    return response


@login_required
def outbox(request):
    return render(request, "operations/outbox.html", {"receipts": OutboxMessage.objects.select_related("revision", "communication__assessment").order_by("-delivered_at")[:100]})


@login_required
def outbox_download(request, pk):
    receipt = get_object_or_404(OutboxMessage.objects.select_related("revision"), pk=pk)
    response = HttpResponse(communications.message_bytes(receipt), content_type="message/rfc822")
    response["Content-Disposition"] = f'attachment; filename="watch-{receipt.pk}.eml"'
    return response


@login_required
@require_http_methods(["GET", "POST"])
def configuration(request):
    if user_tier_rank(request.user) < 1:
        return HttpResponseForbidden("Tier 2 or above is required to configure automation and routing.")
    registry = {"rule": (DiagnosticRule, DiagnosticRuleForm), "policy": (AssessmentPolicy, PolicyForm),
                "dependency": (ServiceDependency, DependencyForm), "area": (SubjectArea, SubjectAreaForm)}
    kind = request.POST.get("kind", request.GET.get("kind", "policy"))
    if kind not in registry:
        kind = "policy"
    model, form_class = registry[kind]
    item_id = request.POST.get("item_id", request.GET.get("item_id"))
    instance = get_object_or_404(model, pk=item_id) if item_id else None
    form = form_class(request.POST if request.method == "POST" else None, prefix="config", instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect(reverse("ui:operations_configuration") + f"?kind={kind}")
    return render(request, "operations/configuration.html", {
        "form": form, "kind": kind, "item_id": instance.pk if instance else "", "items": model.objects.all(),
        "heartbeat": OperationsHeartbeat.objects.filter(name="operations").first(),
    }, status=400 if request.method == "POST" else 200)


@login_required
def reports(request):
    try:
        days = min(365, max(1, int(request.GET.get("days", "30"))))
    except ValueError:
        days = 30
    filters = {key: request.GET.get(key, "")[:128] for key in ("customer", "service", "supplier", "severity")}
    report = reporting.summarize(days=days, include_examples=request.GET.get("examples") == "1", **filters)
    return render(request, "operations/reports.html", {"report": report, "filters": filters,
        "customers": Assessment.objects.exclude(customer="").order_by("customer").values_list("customer", flat=True).distinct(),
        "suppliers": Assessment.objects.exclude(supplier="").order_by("supplier").values_list("supplier", flat=True).distinct(),
        "services": Assessment.objects.exclude(service="").order_by("service").values_list("service", flat=True).distinct(),
    })


@login_required
def reports_data(request):
    from django.http import JsonResponse
    try:
        days = min(365, max(1, int(request.GET.get("days", "30"))))
    except ValueError:
        return HttpResponse("Invalid days", status=400)
    return JsonResponse(reporting.summarize(days=days, include_examples=request.GET.get("examples") == "1",
        **{key: request.GET.get(key, "")[:128] for key in ("customer", "service", "supplier", "severity")}))


@login_required
@require_http_methods(["POST"])
def incident_action(request, pk):
    from . import escalation
    from django.conf import settings
    row = get_object_or_404(Assessment.objects.select_related("incident"), pk=pk)
    if not row.incident or not can_act_on(request.user, row.incident):
        return HttpResponseForbidden("Incident tier permissions are required.")
    kind = request.POST.get("action")
    expected = request.POST.get("expected_tier")
    with transaction.atomic():
        incident = type(row.incident).objects.select_for_update().get(pk=row.incident_id)
        if not can_act_on(request.user, incident):
            return HttpResponseForbidden("Incident tier permissions changed; refresh before acting.")
        if expected != incident.current_tier:
            return HttpResponse("The incident moved tier; refresh before acting.", status=409)
        reason = request.POST.get("reason", "").strip()[:512]
        actor = str(request.user.pk)
        if kind == "ack":
            services.acknowledge(incident.pk, actor=actor, reason=reason)
        elif kind in {"escalate", "resolve"}:
            if not incident.escalation_execution_arn:
                return HttpResponse("Escalation has not started. Retry the engine first.", status=409)
            escalation.send_outcome(incident, escalation.OUTCOME_ESCALATE if kind == "escalate" else escalation.OUTCOME_RESOLVE,
                                    actor=actor, reason=reason)
            if settings.ESCALATION_LOCAL_MODE:
                (services.escalate if kind == "escalate" else services.resolve)(incident.pk, actor=actor, reason=reason)
        else:
            return HttpResponse("Choose acknowledge, escalate, or resolve.", status=400)
    return redirect("ui:assessment_detail", pk=pk)


@login_required
@require_http_methods(["GET"])
def case_state(request, pk):
    row = get_object_or_404(Assessment, pk=pk)
    return JsonResponse({"version": row.history.order_by("-pk").values_list("pk", flat=True).first() or 0})


@login_required
@require_http_methods(["GET"])
def communication_state(request, pk):
    row = get_object_or_404(Communication, pk=pk)
    return JsonResponse({"version": f"{row.state}:{row.revision}"})
