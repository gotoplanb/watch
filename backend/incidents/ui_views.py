"""
Server-rendered incident-management UI (ADR-011): Django templates + HTMX + Alpine +
Tailwind. The working surface for investigators (runs alongside ServiceNow). Mutating
actions reuse the same services/permissions as the API; HTMX swaps the incident body
partial so the page updates without a full reload.
"""
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from . import escalation, services
from .models import Comment, Incident, OnCallShift, Status, Tier, next_tier
from .permissions import can_act_on


def _timeline(incident):
    """Transitions + comments merged into one chronologically-ordered feed."""
    events = [{"kind": "transition", "at": t.at, "obj": t} for t in incident.transitions.all()]
    events += [
        {"kind": "comment", "at": c.created_at, "obj": c}
        for c in incident.comments.select_related("author").all()
    ]
    events.sort(key=lambda e: e["at"])
    return events


def _detail_ctx(request, incident):
    return {
        "incident": incident,
        "timeline": _timeline(incident),
        "can_act": can_act_on(request.user, incident),
        "next_tier": next_tier(incident.current_tier),
    }


@login_required
@require_GET
def incident_list(request):
    qs = Incident.objects.all().order_by("-created_at")
    status = request.GET.get("status") or ""
    tier = request.GET.get("tier") or ""
    if status:
        qs = qs.filter(status=status)
    if tier:
        qs = qs.filter(current_tier=tier)
    ctx = {
        "incidents": list(qs[:200]),
        "status": status,
        "tier": tier,
        "statuses": Status.choices,
        "tiers": Tier.choices,
    }
    template = "incidents/_rows.html" if request.headers.get("HX-Request") else "incidents/list.html"
    return render(request, template, ctx)


@login_required
@require_GET
def incident_detail(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    return render(request, "incidents/detail.html", _detail_ctx(request, incident))


@login_required
@require_POST
def add_comment(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    body = (request.POST.get("body") or "").strip()
    if body:
        Comment.objects.create(incident=incident, author=request.user, body=body)
    return render(request, "incidents/_body.html", _detail_ctx(request, incident))


@login_required
@require_POST
def act(request, pk, action):
    incident = get_object_or_404(Incident, pk=pk)
    if not can_act_on(request.user, incident):
        return HttpResponseForbidden("You must hold this incident's tier (or higher) to act.")

    actor = str(request.user.pk)
    if action == "ack":
        services.acknowledge(incident.id, actor=actor)
    elif action == "escalate":
        escalation.send_outcome(incident, escalation.OUTCOME_ESCALATE, actor=actor)
        if settings.ESCALATION_LOCAL_MODE:
            services.escalate(incident.id, actor=actor)
    elif action == "resolve":
        escalation.send_outcome(incident, escalation.OUTCOME_RESOLVE, actor=actor)
        if settings.ESCALATION_LOCAL_MODE:
            services.resolve(incident.id, actor=actor)

    incident.refresh_from_db()
    return render(request, "incidents/_body.html", _detail_ctx(request, incident))


# --- On-call schedule (ADR-012) ---

def _aware(dt):
    if dt and timezone.is_naive(dt):
        return timezone.make_aware(dt)
    return dt


def _schedule_ctx():
    return {
        "on_call": {tier.value: services.current_on_call(tier.value) for tier in Tier},
        "shifts": OnCallShift.objects.select_related("user")[:50],
        "tiers": Tier.choices,
        "users": User.objects.order_by("username"),
        "now": timezone.now(),
    }


@login_required
@require_GET
def schedule(request):
    return render(request, "incidents/schedule.html", _schedule_ctx())


@login_required
@require_POST
def add_shift(request):
    tier = request.POST.get("tier")
    user_id = request.POST.get("user") or ""
    starts = _aware(parse_datetime(request.POST.get("starts_at") or ""))
    ends = _aware(parse_datetime(request.POST.get("ends_at") or ""))
    if (tier in Tier.values and user_id.isdigit() and starts and ends and ends > starts
            and User.objects.filter(pk=user_id).exists()):
        OnCallShift.objects.create(tier=tier, user_id=int(user_id), starts_at=starts, ends_at=ends)
    return render(request, "incidents/_schedule_body.html", _schedule_ctx())
