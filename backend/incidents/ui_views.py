"""
Server-rendered incident-management UI (ADR-011): Django templates + HTMX + Alpine +
Tailwind. The working surface for investigators (runs alongside ServiceNow). Mutating
actions reuse the same services/permissions as the API; HTMX swaps the incident body
partial so the page updates without a full reload.
"""
from django.conf import settings
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from django.contrib import messages

from . import apikeys
from . import checks as checks_svc
from . import envview, escalation, flags, rca_ai, services, session_index
from .models import (
    AnnotationTag,
    CheckSource,
    CheckSubjectKind,
    Digest,
    EnvStatus,
    Incident,
    LinkKind,
    OnCallShift,
    Problem,
    ProblemStatus,
    Rca,
    RcaStatus,
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


def _detail_ctx(request, incident, awaiting=None):
    return {
        "incident": incident,
        # Set when a cloud-mode escalate/resolve has been issued but the commit Lambda hasn't
        # landed yet (ADR-007): the swapped-in body carries a poller that refreshes until the
        # transition commits. Without it the async response shows stale pre-commit state and the
        # page sits dead until a manual reload (the handoff card's poller only exists once the card
        # itself is reserved, which is also post-commit). The value is the state we're waiting to
        # LEAVE — "<tier>:<status>" — so the poll self-terminates the moment it changes.
        "awaiting": awaiting,
        # Newest-first (ADR-040): the incoming responder reads the handoff brief, then history.
        # RCA assembly (rca_markdown) stays chronological — this reversal is display-only.
        "timeline": list(reversed(services.timeline(incident))),
        "can_act": can_act_on(request.user, incident),
        "next_tier": next_tier(incident.current_tier),
        "annotation_tags": AnnotationTag.choices,
        "links": services.links_for(incident),
        "link_kinds": LinkKind.choices,
        "record_number": incident.number,
    }


@login_required
@require_GET
def incident_list(request):
    qs = Incident.objects.all().order_by("-created_at")
    status = request.GET.get("status") or ""
    tier = request.GET.get("tier") or ""
    q = (request.GET.get("q") or "").strip()
    if status:
        qs = qs.filter(status=status)
    if tier:
        qs = qs.filter(current_tier=tier)
    if q:
        # one box, both human keys: free text matches the title, INC-… matches the number
        qs = qs.filter(Q(title__icontains=q) | Q(number__icontains=q))
    ctx = {
        "incidents": list(qs[:200]),
        "status": status,
        "tier": tier,
        "q": q,
        "statuses": Status.choices,
        "tiers": Tier.choices,
    }
    template = "incidents/_results.html" if request.headers.get("HX-Request") else "incidents/list.html"
    return render(request, template, ctx)


@login_required
@require_GET
def incident_detail(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    return render(request, "incidents/detail.html", _detail_ctx(request, incident))


@login_required
@require_GET
def incident_body(request, pk):
    """The incident body on its own — what a pending handoff card polls while the model writes it
    (ADR-042). Same partial every mutation swaps in, so the poll that lands the brief also refreshes
    everything else; the swapped-in body has no poller, which is how the polling stops."""
    incident = get_object_or_404(Incident, pk=pk)
    # `?await=<tier>:<status>` = an escalate/resolve poller waiting for its commit (see _detail_ctx).
    # Keep polling only while the incident is still in that state; once it moves, drop the poller.
    await_state = request.GET.get("await") or ""
    awaiting = await_state if await_state == f"{incident.current_tier}:{incident.status}" else None
    return render(request, _BODY_PARTIAL, _detail_ctx(request, incident, awaiting=awaiting))


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
    # The human's stated reason — why they escalated (ADR-041) or what actually fixed it (ADR-042).
    # Optional by design in both cases: we want the signal, not a toll gate. It rides the same paths
    # as the actor (cloud: SendTaskSuccess → commit Lambda; local: straight to services) and lands on
    # the Transition, where the next tier's brief and the RCA both read it.
    reason = (request.POST.get("reason") or "").strip()
    if action == "ack":
        services.acknowledge(incident.id, actor=actor)
    elif action == "escalate":
        escalation.send_outcome(incident, escalation.OUTCOME_ESCALATE, actor=actor, reason=reason)
        if settings.ESCALATION_LOCAL_MODE:
            services.escalate(incident.id, actor=actor, reason=reason)
    elif action == "resolve":
        escalation.send_outcome(incident, escalation.OUTCOME_RESOLVE, actor=actor, reason=reason)
        if settings.ESCALATION_LOCAL_MODE:
            services.resolve(incident.id, actor=actor, reason=reason)

    incident.refresh_from_db()
    # In cloud mode escalate/resolve only issue SendTaskSuccess here; the commit Lambda writes the
    # new state a beat later (ADR-007), so what we're about to render is still pre-commit. Hand the
    # body a poller (keyed to the state we're leaving) so it refreshes itself the moment the commit
    # lands — instead of sitting on stale state until the user reloads. Local mode commits inline
    # above, so there is nothing to wait for.
    awaiting = None
    if action in ("escalate", "resolve") and not settings.ESCALATION_LOCAL_MODE:
        awaiting = f"{incident.current_tier}:{incident.status}"
    return render(request, _BODY_PARTIAL, _detail_ctx(request, incident, awaiting=awaiting))


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


_SETTINGS = "ui:settings"  # redirect target, reused across the settings write views


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
        "mcp_key": apikeys.mcp_key_for(request.user),
        "mcp_url": f"{settings.MCP_PUBLIC_BASE_URL.rstrip('/')}/mcp",
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
    return redirect(_SETTINGS)


@login_required
@require_POST
def sign_out_everywhere(request):
    """Revoke all of the user's OTHER sessions (ADR-008), keeping the current device signed in. Own
    sessions only — the server-side session store makes this an instant kill, unlike a stateless JWT."""
    n = session_index.flush(request.user, keep=request.session.session_key or "")
    messages.success(request, f"Signed out {n} other session{'' if n == 1 else 's'}.")
    return redirect(_SETTINGS)


@login_required
@require_POST
def change_password(request):
    """Self-service password change (ADR-008). Django's PasswordChangeForm enforces the current
    password + AUTH_PASSWORD_VALIDATORS; update_session_auth_hash keeps THIS session signed in
    (the password-hash change would otherwise invalidate the session cookie)."""
    form = PasswordChangeForm(user=request.user, data=request.POST)
    if form.is_valid():
        form.save()
        update_session_auth_hash(request, request.user)  # don't log the user out of their own device
        messages.success(request, "Password changed.")
    else:
        errs = "; ".join(msg for field in form.errors.values() for msg in field)
        messages.error(request, f"Could not change password: {errs}")
    return redirect(_SETTINGS)


def _history_index(request, param, count):
    """Position in a newest-first history: 0 is now, higher is older. Clamped, so a stale bookmark or
    a hand-typed index lands somewhere real instead of 404ing."""
    try:
        i = int(request.GET.get(param, 0))
    except ValueError:
        i = 0
    return max(0, min(i, max(0, count - 1)))


def _pager(request, param, index, count):
    """Older/Newer links for one history, preserving every other query param — so paging the digest
    doesn't yank the status snapshot (or the environment) out from under you."""
    def at(i):
        q = request.GET.copy()
        q[param] = i
        return f"?{q.urlencode()}"

    return {
        "index": index, "count": count,
        "older": at(index + 1) if index + 1 < count else "",
        "newer": at(index - 1) if index > 0 else "",
    }


@login_required
@require_GET
def env_dashboard(request):
    """Per-environment ops status + digests (ADR-028/043) — session-auth, ops-facing.

    The store is schema-less; the *screen* is not. `envview.summarize` turns the posted payload into
    a worst-first list of subsystem rows with a verdict and a staleness read, and both histories
    (snapshots and digests) page independently — the stored history is what "what did it look like an
    hour ago" needs, and it was previously written but never shown."""
    envs = sorted(
        set(EnvStatus.objects.values_list("environment", flat=True))
        | set(Digest.objects.values_list("environment", flat=True))
    )
    if not envs:
        envs = ["prod", "nonprod"]
    env = request.GET.get("env") or envs[0]

    statuses = list(EnvStatus.objects.filter(environment=env)[:100])  # newest first (Meta ordering)
    s_index = _history_index(request, "s", len(statuses))
    status = statuses[s_index] if statuses else None

    special_only = request.GET.get("special") == "1"
    digest_qs = Digest.objects.filter(environment=env)
    if special_only:
        digest_qs = digest_qs.filter(special=True)
    digests = list(digest_qs[:100])
    d_index = _history_index(request, "d", len(digests))
    digest = digests[d_index] if digests else None

    return render(request, "incidents/environments.html", {
        "environments": envs,
        "env": env,
        "status": status,
        "view": envview.summarize(status, historical=s_index > 0),
        "status_pager": _pager(request, "s", s_index, len(statuses)),
        "digest": digest,
        "digest_pager": _pager(request, "d", d_index, len(digests)),
        "special_only": special_only,
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
        "links": services.links_for(problem),
        "link_kinds": LinkKind.choices,
        "record_number": problem.number,
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


# --- RCA records (ADR-031): a stored root-cause writeup, document seeded by assembly then edited ---

_RCA_DETAIL = "ui:rca_detail"  # redirect target, reused across the write views


@login_required
@require_GET
def rca_list(request):
    return render(request, "incidents/rcas.html", {
        "rcas": Rca.objects.all()[:200],
        "incidents": Incident.objects.all()[:200],  # optional seed source in the create form
    })


@login_required
@require_POST
def rca_create(request):
    title = (request.POST.get("title") or "").strip()
    incident = None
    incident_id = (request.POST.get("incident") or "").strip()
    if incident_id:
        incident = Incident.objects.filter(pk=incident_id).first()
    if not title and incident is None:
        return redirect("ui:rcas")  # nothing to go on — need a title or a seed incident
    rca = services.seed_rca(title=title, incident=incident, actor=request.user.username)
    return redirect(_RCA_DETAIL, pk=rca.id)


@login_required
@require_GET
def rca_detail(request, pk):
    rca = get_object_or_404(Rca, pk=pk)
    return render(request, "incidents/rca_detail.html", {
        "rca": rca,
        "timeline": services.timeline(rca),
        "statuses": RcaStatus.choices,
        "users": User.objects.order_by("username"),
        "links": services.links_for(rca),
        "link_kinds": LinkKind.choices,
        "record_number": rca.number,
        "ai_draft_enabled": flags.is_enabled(services.RCA_AI_FLAG),
        "ai_provider": settings.RCA_AI_PROVIDER,
    })


@login_required
@require_POST
def rca_save_document(request, pk):
    rca = get_object_or_404(Rca, pk=pk)
    rca.document = request.POST.get("document") or ""
    rca.save(update_fields=["document", "updated_at"])
    return redirect(_RCA_DETAIL, pk=pk)


@login_required
@require_POST
def rca_ai_draft(request, pk):
    """Replace the RCA document with a Bedrock-drafted narrative (ADR-021/031/033). Flag-gated:
    404-adjacent 403 when off so the endpoint can't be driven while the control is hidden."""
    if not flags.is_enabled(services.RCA_AI_FLAG):
        return HttpResponseForbidden("AI RCA drafting is not enabled")
    rca = get_object_or_404(Rca, pk=pk)
    try:
        services.draft_rca(rca, actor=request.user.username)
        messages.success(request, "AI draft generated — review and edit before finalising.")
    except rca_ai.DraftError as exc:
        messages.error(request, f"AI draft failed: {exc}")
    return redirect(_RCA_DETAIL, pk=pk)


@login_required
@require_POST
def rca_add_note(request, pk):
    rca = get_object_or_404(Rca, pk=pk)
    body = (request.POST.get("body") or "").strip()
    if body:
        services.add_note(rca, actor=request.user.username, body=body)
    return redirect(_RCA_DETAIL, pk=pk)


@login_required
@require_POST
def rca_update(request, pk):
    """Status + assignee changes; a status change posts a system event to the shared timeline."""
    rca = get_object_or_404(Rca, pk=pk)
    fields = []
    status = request.POST.get("status")
    if status in RcaStatus.values and status != rca.status:
        old, rca.status = rca.status, status
        fields.append("status")
        services.post_system_event(rca, f"Status {old} → {status} by {request.user.username}")
    assignee_id = (request.POST.get("assignee") or "").strip()
    if assignee_id.isdigit() and User.objects.filter(pk=assignee_id).exists():
        rca.assignee_id = int(assignee_id)
        fields.append("assignee")
    elif not assignee_id and rca.assignee_id:
        rca.assignee = None
        fields.append("assignee")
    if fields:
        rca.save(update_fields=fields + ["updated_at"])
    return redirect(_RCA_DETAIL, pk=pk)


# --- Generic record links (ADR-031) — shared add/remove across incident/problem/rca details ---

def _record_detail_redirect(record):
    """Redirect back to a record's /ui detail page after a link add/remove."""
    if isinstance(record, Problem):
        return redirect(_PROBLEM_DETAIL, pk=record.id)
    if isinstance(record, Rca):
        return redirect(_RCA_DETAIL, pk=record.id)
    return redirect("ui:incident_detail", pk=record.id)


@login_required
@require_POST
def link_add(request):
    """Link two records by their human numbers (INC-/PRB-/RCA-). Redirects back to the source record
    with a success/error message so a bad or missing number isn't a silent no-op."""
    to_number = request.POST.get("to_number", "").strip()
    src = services.record_for_number(request.POST.get("from_number", ""))
    dst = services.record_for_number(to_number)
    if src is None:
        return redirect("ui:incident_list")
    if dst is None:
        messages.error(
            request,
            f"No record found for “{to_number or '—'}”. Enter a record number like INC-0007 "
            "(shown next to each incident/problem/RCA).",
        )
        return _record_detail_redirect(src)
    link, created = services.link_records(src, dst, kind=request.POST.get("kind", ""), actor=request.user.username)
    if created:
        messages.success(request, f"Linked {src.number} → {dst.number}.")
    elif link is None:
        messages.error(request, "A record can’t be linked to itself.")
    else:
        messages.info(request, f"{src.number} is already linked to {dst.number}.")
    return _record_detail_redirect(src)


@login_required
@require_POST
def link_remove(request, link_id):
    services.unlink(link_id)
    src = services.record_for_number(request.POST.get("from_number", ""))
    if src is not None:
        return _record_detail_redirect(src)
    return redirect("ui:incident_list")
