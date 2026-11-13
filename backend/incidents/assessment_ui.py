import uuid

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from . import assessments, automation
from .assessment_forms import SignalForm
from .models import Assessment, AssessmentStatus, SmeEngagement
from .permissions import user_tier_rank


@login_required
@require_http_methods(["GET"])
def inbox(request):
    qs = Assessment.objects.select_related("assignee", "incident")
    query = request.GET.get("q", "").strip()
    state = request.GET.get("status", "active")
    if query:
        qs = qs.filter(Q(title__icontains=query) | Q(number__icontains=query) | Q(customer__icontains=query))
    if state == "active":
        qs = qs.exclude(status__in=[AssessmentStatus.NO_ISSUE, AssessmentStatus.PROMOTED])
    elif state in AssessmentStatus.values:
        qs = qs.filter(status=state)
    counts = dict(Assessment.objects.order_by().values_list("status").annotate(total=Count("pk")))
    return render(request, "assessments/inbox.html", {
        "assessments": qs[:100], "query": query, "state": state, "states": AssessmentStatus.choices,
        "counts": {"active": sum(counts.get(s, 0) for s in ["new", "investigating", "monitoring"]),
                   "no_issue": counts.get("no_issue", 0), "promoted": counts.get("promoted", 0)},
        "can_manage": user_tier_rank(request.user) >= 0,
        "my_handoffs": SmeEngagement.objects.filter(Q(target=request.user) | Q(area__members=request.user)).exclude(state="completed").select_related("assessment").distinct(),
        "response_queue": Assessment.objects.filter(incident__status="OPEN", incident__acknowledged_at__isnull=True).select_related("incident")[:10] if user_tier_rank(request.user) >= 0 else [],
    })


@login_required
@require_http_methods(["GET", "POST"])
def new(request):
    if user_tier_rank(request.user) < 0:
        return HttpResponseForbidden("A responder role is required to create assessments.")
    form = SignalForm(request.POST or None, initial={"source_event_id": uuid.uuid4()}, prefix="signal")
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        data["source_event_id"] = str(data["source_event_id"])
        assessment, created = assessments.receive(**data, actor=request.user.username, assignee=request.user)
        if created:
            automation.schedule(assessment)
        return redirect("ui:assessment_detail", pk=assessment.pk)
    return render(request, "assessments/new.html", {"form": form},
                  status=400 if request.method == "POST" else 200)
