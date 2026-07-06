"""
Server-rendered incident-management UI (ADR-011): Django templates + HTMX + Alpine +
Tailwind. The working surface for investigators (runs alongside ServiceNow). Mutating
actions reuse the same services/permissions as the API; HTMX swaps the incident body
partial so the page updates without a full reload.
"""
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from django.contrib import messages

from . import apikeys
from . import checks as checks_svc
from . import escalation, services, session_index
from .models import (
    AnnotationTag,
    CheckSource,
    CheckSubjectKind,
    Digest,
    EnvStatus,
    Incident,
    OnCallShift,
    Problem,
    ProblemStatus,
    SessionCheck,
    Status,
    Tier,
    WebhookDelivery,
    WebhookSubscription,
    next_tier,
)
from .permissions import can_act_on

# The swappable incident panel (ADR-011) returned by every mutating endpoint.
_BODY_PARTIAL = "incidents/_body.html"


def _resolve_target(incident, target):
    """Map a 'transition:<id>' / 'event:<id>' string to the object, scoped to THIS incident
    (so a user can only annotate events on the incident they're viewing)."""
    kind, _, sid = (target or "").partition(":")
    if not sid.isdigit():
        return None
    if kind == "transition":
        return incident.transitions.filter(pk=int(sid)).first()
    if kind == "event":
        return incident.events.filter(pk=int(sid)).first()
    return None


def _detail_ctx(request, incident):
    return {
        "incident": incident,
        "timeline": services.timeline(incident),
        "can_act": can_act_on(request.user, incident),
        "next_tier": next_tier(incident.current_tier),
        "annotation_tags": AnnotationTag.choices,
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
def add_note(request, pk):
    """Post a human note onto the incident timeline (a TimelineEvent of type `note`). Commentary,
    not a state change — available to any authenticated user (like the old add_comment)."""
    incident = get_object_or_404(Incident, pk=pk)
    body = (request.POST.get("body") or "").strip()
    if body:
        services.add_note(incident, actor=request.user.username, body=body)
    return render(request, _BODY_PARTIAL, _detail_ctx(request, incident))


@login_required
@require_POST
def annotate(request, pk):
    """Attach an annotation (note/tag) to ANY timeline event on this incident — a Transition or a
    TimelineEvent (ADR-021). Used to mark up the history for RCA ('this shouldn't have happened')."""
    incident = get_object_or_404(Incident, pk=pk)
    obj = _resolve_target(incident, request.POST.get("target"))
    body = (request.POST.get("body") or "").strip()
    tag = request.POST.get("tag") or AnnotationTag.NOTE
    if obj is not None and tag in AnnotationTag.values and (body or tag != AnnotationTag.NOTE):
        services.annotate_event(obj, author=request.user, body=body, tag=tag)
    return render(request, _BODY_PARTIAL, _detail_ctx(request, incident))


@login_required
@require_GET
def rca(request, pk):
    """Download the incident's full annotated timeline assembled as an RCA Markdown document
    (ADR-021) — the clean input to a root-cause writeup. AI-drafted RCA is a flagged follow-up."""
    incident = get_object_or_404(Incident, pk=pk)
    md = services.rca_markdown(incident)
    resp = HttpResponse(md, content_type="text/markdown; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="rca-{incident.id}.md"'
    return resp


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
    return render(request, _BODY_PARTIAL, _detail_ctx(request, incident))


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


# --- Session Checks (ADR-022): error-span lookup for a session/user ---

@login_required
@require_GET
def check_list(request):
    return render(request, "incidents/checks.html", {
        "checks": SessionCheck.objects.all()[:100],
        "kinds": CheckSubjectKind.choices,
    })


@login_required
@require_POST
def run_check(request):
    """Trigger a check from the UI (source=manual). subject = session correlation id or a user id."""
    kind = request.POST.get("subject_kind")
    subject = (request.POST.get("subject") or "").strip()
    if kind in CheckSubjectKind.values and subject:
        checks_svc.create_and_run(subject_kind=kind, subject_raw=subject, source=CheckSource.MANUAL)
    return redirect("ui:checks")


@login_required
@require_GET
def check_detail(request, pk):
    check = get_object_or_404(SessionCheck, pk=pk)
    return render(request, "incidents/check_detail.html",
                  {"check": check, "spans": check.error_spans.all()})


# --- Outbound event webhooks (ADR-023): register receivers + delivery log ---

@login_required
@require_GET
def webhook_list(request):
    return render(request, "incidents/webhooks.html", {
        "subscriptions": WebhookSubscription.objects.all()[:50],
        "deliveries": WebhookDelivery.objects.select_related("subscription")[:50],
    })


@login_required
@require_POST
def add_subscription(request):
    url = (request.POST.get("url") or "").strip()
    secret = (request.POST.get("secret") or "").strip()
    if url and secret:
        event_types = [e.strip() for e in (request.POST.get("event_types") or "").split(",") if e.strip()]
        WebhookSubscription.objects.create(
            url=url, secret=secret, event_types=event_types,
            description=(request.POST.get("description") or "").strip(),
        )
    return redirect("ui:webhooks")


@login_required
@require_GET
def settings_view(request):
    """Per-user settings — surfaces *this* user's own ntfy paging subscription (ADR-013). The topic
    carries the NTFY_TOPIC_SECRET suffix, so it's not derivable from the source; showing it only to
    the logged-in owner is the whole point. Also lists the tier fallback topic(s) for the user's tier
    group(s), since a rota gap pages the tier, not the person."""
    base = settings.NTFY_BASE_URL.rstrip("/")
    user_topic = services.paging_topic("user", request.user.id, seed=apikeys.seed_for(request.user))
    tiers = request.user.groups.filter(name__in=[t.value for t in Tier]).values_list("name", flat=True)
    tier_topics = [
        {"tier": t, "topic": services.paging_topic("tier", t), "url": f"{base}/{services.paging_topic('tier', t)}"}
        for t in tiers
    ]
    return render(request, "incidents/settings.html", {
        "api_key": apikeys.api_key_for(request.user),
        "api_key_set": bool(settings.API_KEY_SECRET),
        "ingest_base": request.build_absolute_uri("/api/environments").rstrip("/"),
        "ntfy_base": base,
        "user_topic": user_topic,
        "user_topic_url": f"{base}/{user_topic}",
        "tier_topics": tier_topics,
        "topic_secret_set": bool(settings.NTFY_TOPIC_SECRET),
    })


@login_required
@require_POST
def rotate_keys(request):
    """Rotate the current user's seed (ADR-030) — rolls their API key + ntfy topic together. Own
    keyring only; back to settings where the new values render."""
    apikeys.rotate(request.user)
    return redirect("ui:settings")


@login_required
@require_POST
def sign_out_everywhere(request):
    """Revoke all of the user's OTHER sessions (ADR-008), keeping the current device signed in. Own
    sessions only — the server-side session store makes this an instant kill, unlike a stateless JWT."""
    n = session_index.flush(request.user, keep=request.session.session_key or "")
    messages.success(request, f"Signed out {n} other session{'' if n == 1 else 's'}.")
    return redirect("ui:settings")


@login_required
@require_GET
def env_dashboard(request):
    """Detailed per-environment ops status + digests (ADR-028) — session-auth, ops-facing. The status
    payload is arbitrary JSON rendered by the schema-less `_json_node` partial; digests are markdown
    with a Copy-for-Slack button and a SPECIAL/ROUTINE (speci) badge."""
    envs = sorted(
        set(EnvStatus.objects.values_list("environment", flat=True))
        | set(Digest.objects.values_list("environment", flat=True))
    )
    if not envs:
        envs = ["prod", "nonprod"]
    env = request.GET.get("env") or envs[0]
    return render(request, "incidents/environments.html", {
        "environments": envs,
        "env": env,
        "status": EnvStatus.objects.filter(environment=env).first(),
        "digests": list(Digest.objects.filter(environment=env)[:50]),
    })


# --- Problems (ADR-031): thin ops record, generic timeline, no escalation engine ---
_PROBLEM_DETAIL = "ui:problem_detail"  # redirect target, reused across the write views


@login_required
@require_GET
def problem_list(request):
    return render(request, "incidents/problems.html", {
        "problems": Problem.objects.all()[:200],
        "statuses": ProblemStatus.choices,
    })


@login_required
@require_POST
def problem_create(request):
    title = (request.POST.get("title") or "").strip()
    if not title:
        return redirect("ui:problems")
    p = Problem.objects.create(title=title, description=(request.POST.get("description") or "").strip())
    return redirect(_PROBLEM_DETAIL, pk=p.id)


@login_required
@require_GET
def problem_detail(request, pk):
    problem = get_object_or_404(Problem, pk=pk)
    return render(request, "incidents/problem_detail.html", {
        "problem": problem,
        "timeline": services.timeline(problem),
        "statuses": ProblemStatus.choices,
        "users": User.objects.order_by("username"),
    })


@login_required
@require_POST
def problem_add_note(request, pk):
    problem = get_object_or_404(Problem, pk=pk)
    body = (request.POST.get("body") or "").strip()
    if body:
        services.add_note(problem, actor=request.user.username, body=body)
    return redirect(_PROBLEM_DETAIL, pk=pk)


@login_required
@require_POST
def problem_update(request, pk):
    """Status + assignee changes; a status change posts a system event to the shared timeline."""
    problem = get_object_or_404(Problem, pk=pk)
    fields = []
    status = request.POST.get("status")
    if status in ProblemStatus.values and status != problem.status:
        old, problem.status = problem.status, status
        fields.append("status")
        services.post_system_event(problem, f"Status {old} → {status} by {request.user.username}")
    assignee_id = (request.POST.get("assignee") or "").strip()
    if assignee_id.isdigit() and User.objects.filter(pk=assignee_id).exists():
        problem.assignee_id = int(assignee_id)
        fields.append("assignee")
    elif not assignee_id and problem.assignee_id:
        problem.assignee = None
        fields.append("assignee")
    if fields:
        problem.save(update_fields=fields + ["updated_at"])
    return redirect(_PROBLEM_DETAIL, pk=pk)
