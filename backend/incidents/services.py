"""
Transition service — the one place that mutates incident lifecycle state and writes
the matching append-only audit record (ADR-007). Manual (API) and automatic
(Lambda timeout) paths both call through here so the Transition shape is identical
regardless of *how* it happened (spec §3).

Every transition is idempotent: "act if still applicable", never blind "act"
(ADR-001). Callers wrap these in select_for_update where concurrency matters.
"""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import events
from .models import (
    Annotation,
    EventType,
    Incident,
    OnCallShift,
    Status,
    TimelineEvent,
    Transition,
    next_tier,
)


def current_on_call(tier, at=None):
    """The active on-call shift for a tier (ADR-012), or None on a rota gap. Most
    recently-started shift wins if windows overlap."""
    at = at or timezone.now()
    return (
        OnCallShift.objects.filter(tier=tier, starts_at__lte=at, ends_at__gt=at)
        .select_related("user")
        .order_by("-starts_at")
        .first()
    )


def on_call_user(tier, at=None):
    shift = current_on_call(tier, at)
    return shift.user if shift else None


def _record(incident, *, to_status, to_tier, actor, reason, from_status, from_tier):
    Transition.objects.create(
        incident=incident,
        from_status=from_status,
        from_tier=from_tier,
        to_status=to_status,
        to_tier=to_tier,
        actor=actor,
        reason=reason,
    )


@transaction.atomic
def acknowledge(incident_id, actor: str, reason: str = "") -> Incident:
    """ACK: Postgres-only, does NOT consume the task token; the SLA clock keeps
    running (ADR-007). Idempotent — re-acking is a no-op."""
    incident = Incident.objects.select_for_update().get(pk=incident_id)
    if incident.status == Status.OPEN and incident.acknowledged_at is None:
        incident.acknowledged_at = timezone.now()
        incident.save(update_fields=["acknowledged_at", "updated_at"])
        _record(
            incident,
            from_status=incident.status,
            from_tier=incident.current_tier,
            to_status=incident.status,
            to_tier=incident.current_tier,
            actor=actor,
            reason=reason or "acknowledged",
        )
    return incident


@transaction.atomic
def escalate(incident_id, actor: str, reason: str = "") -> Incident:
    """Advance one tier. Manual (actor=user) and auto (actor=system) share this path.
    Idempotent via select_for_update + applicability check."""
    incident = Incident.objects.select_for_update().get(pk=incident_id)
    if incident.status != Status.OPEN:
        return incident
    target = next_tier(incident.current_tier)
    if target is None:
        # At T3 with nowhere to escalate — the ASL routes this to a Fail state, which
        # surfaces as a failed execution -> alarm (ADR-001). Nothing to do app-side.
        return incident

    from_tier = incident.current_tier
    incident.current_tier = target.value
    incident.acknowledged_at = None  # new tier, not yet acknowledged
    incident.current_task_token = ""  # T-prev token consumed; record_token sets the next
    incident.assignee = on_call_user(target.value)  # auto-route to the new tier's on-call
    incident.save(
        update_fields=[
            "current_tier", "acknowledged_at", "current_task_token", "assignee", "updated_at"
        ]
    )
    _record(
        incident,
        from_status=incident.status,
        from_tier=from_tier,
        to_status=incident.status,
        to_tier=target.value,
        actor=actor,
        reason=reason or "escalated",
    )
    # Narrative system event alongside the structured Transition (ADR-021): captures the paging
    # target, which the Transition can't hold. Complements — the Transition stays the audit record.
    auto = actor == Transition.SYSTEM_ACTOR
    assignee = incident.assignee
    paged = f"paged {assignee.username} (on-call {target.value})" if assignee else f"no on-call for {target.value}"
    verb = "Auto-escalated (SLA breach)" if auto else "Escalated"
    post_system_event(
        incident,
        body=f"{verb} {from_tier}→{target.value}; {paged}.",
        data={
            "from_tier": from_tier,
            "to_tier": target.value,
            "actor": actor,
            "auto": auto,
            "assignee": assignee.username if assignee else None,
        },
    )
    events.emit("incident.escalated", {
        "incident_id": str(incident.id), "title": incident.title,
        "from_tier": from_tier, "to_tier": target.value, "actor": actor, "auto": auto,
    })
    return incident


@transaction.atomic
def record_tier_token(incident_id, tier: str, token: str, sla_seconds: int | None = None) -> Incident:
    """Called by the record_token Lambda when Step Functions enters a tier's
    waitForTaskToken state (ADR-007). Persists the outstanding token + SLA deadline so
    the API can later SendTaskSuccess. Idempotent — safe to retry. Writes no Transition;
    the commit Lambda (services.escalate/resolve) owns the audit record."""
    incident = Incident.objects.select_for_update().get(pk=incident_id)
    if incident.status != Status.OPEN:
        return incident  # resolved between dispatch and entry — leave it terminal
    if sla_seconds is None:
        sla_seconds = settings.TIER_SLA_SECONDS.get(tier, 0)
    incident.current_tier = tier
    incident.current_task_token = token
    incident.sla_deadline_at = timezone.now() + timedelta(seconds=sla_seconds)
    incident.assignee = on_call_user(tier)  # auto-route to this tier's on-call (ADR-012)
    incident.save(
        update_fields=[
            "current_tier", "current_task_token", "sla_deadline_at", "assignee", "updated_at"
        ]
    )
    return incident


@transaction.atomic
def resolve(incident_id, actor: str, reason: str = "") -> Incident:
    """RESOLVE: terminal. Consuming the token (via the caller / send_outcome) ends the
    execution in Succeed, so no zombie timer can later auto-escalate (ADR-007)."""
    incident = Incident.objects.select_for_update().get(pk=incident_id)
    if incident.status == Status.RESOLVED:
        return incident
    from_status = incident.status
    incident.status = Status.RESOLVED
    incident.current_task_token = ""  # consumed
    incident.save(update_fields=["status", "current_task_token", "updated_at"])
    _record(
        incident,
        from_status=from_status,
        from_tier=incident.current_tier,
        to_status=Status.RESOLVED,
        to_tier=incident.current_tier,
        actor=actor,
        reason=reason or "resolved",
    )
    events.emit("incident.resolved", {
        "incident_id": str(incident.id), "title": incident.title,
        "tier": incident.current_tier, "actor": actor,
    })
    return incident


# --- Timeline events, annotations, and RCA assembly (ADR-021) ---

def add_note(incident, actor: str, body: str) -> TimelineEvent:
    """Human note on the incident timeline."""
    return TimelineEvent.objects.create(
        incident=incident, type=EventType.NOTE, actor=actor, body=body
    )


def post_system_event(incident, body: str, data: dict | None = None) -> TimelineEvent:
    """Escalation-engine narrative event — complements a Transition (which stays the audit record)."""
    return TimelineEvent.objects.create(
        incident=incident, type=EventType.SYSTEM, actor="system", body=body, data=data or {}
    )


def post_ai_event(incident, body: str, actor: str = "argus", data: dict | None = None) -> TimelineEvent:
    """AI-assisted triage finding on the timeline (§8 / #17) — the hook the AI agent posts through,
    so its findings land in the incident history and feed the RCA."""
    return TimelineEvent.objects.create(
        incident=incident, type=EventType.AI, actor=actor, body=body, data=data or {}
    )


def annotate_event(target, *, author, body: str = "", tag: str = "note") -> Annotation:
    """Attach an annotation/tag to ANY timeline event — a Transition or a TimelineEvent. Orthogonal
    to the event; never mutates it, so an authoritative Transition stays intact."""
    return Annotation.objects.create(target=target, author=author, body=body, tag=tag)


def timeline(incident):
    """Merged, time-ordered incident history: Transitions + TimelineEvents, each with its
    annotations prefetched. Items: {kind: transition|event, at, obj, target: 'kind:id'}."""
    items = [
        {"kind": "transition", "at": t.at, "obj": t, "target": f"transition:{t.id}"}
        for t in incident.transitions.prefetch_related("annotations__author")
    ]
    items += [
        {"kind": "event", "at": e.occurred_at, "obj": e, "target": f"event:{e.id}"}
        for e in incident.events.prefetch_related("annotations__author")
    ]
    items.sort(key=lambda i: i["at"])
    return items


_RCA_FLAG_TAGS = {"unexpected", "root-cause", "contributing"}


def _rca_line(item) -> str:
    """One Markdown timeline row (event + its annotations) for the RCA assembly."""
    obj = item["obj"]
    ts = item["at"].strftime("%Y-%m-%d %H:%M:%S %Z").strip() if item["at"] else "?"
    if item["kind"] == "transition":
        if obj.to_status == Status.RESOLVED and obj.from_status != Status.RESOLVED:
            what = f"resolved (at {obj.to_tier})"
        elif obj.from_tier != obj.to_tier:
            what = f"escalated {obj.from_tier}→{obj.to_tier}"
        else:
            what = obj.reason or "updated"
        head = f"**transition** — {what} · actor `{obj.actor}`"
    else:
        head = f"**{obj.type}** · `{obj.actor or 'system'}` — {obj.body}"
    lines = [f"- `{ts}` {head}"]
    for a in obj.annotations.all():
        who = a.author.username if a.author else "unknown"
        note = f" — {a.body}" if a.body else ""
        lines.append(f"    - _[{a.tag}]_ {who}{note}")
    return "\n".join(lines)


def rca_markdown(incident) -> str:
    """Assemble the full annotated timeline into a structured RCA Markdown document — the clean,
    reviewable input to a root-cause writeup. The AI-drafted RCA (flagged) consumes the same text."""
    items = timeline(incident)
    ack = (
        f"{incident.acknowledged_at:%Y-%m-%d %H:%M:%S %Z}".strip()
        if incident.acknowledged_at
        else "—"
    )
    out = [
        f"# RCA — {incident.title}",
        "",
        f"- **Incident:** `{incident.id}`",
        f"- **Source:** {incident.source}",
        f"- **Status / Tier:** {incident.status} · {incident.current_tier}",
        f"- **Opened:** {incident.created_at:%Y-%m-%d %H:%M:%S %Z}".strip(),
        f"- **Acknowledged:** {ack}",
        "",
        "## Timeline",
        "",
    ]
    out += [_rca_line(i) for i in items] if items else ["_(no events)_"]
    out += ["", "## Flagged for RCA", ""]
    flagged = [
        (item, a)
        for item in items
        for a in item["obj"].annotations.all()
        if a.tag in _RCA_FLAG_TAGS
    ]
    if flagged:
        for item, a in flagged:
            who = a.author.username if a.author else "unknown"
            ts = item["at"].strftime("%Y-%m-%d %H:%M:%S") if item["at"] else "?"
            out.append(f"- **{a.tag}** @ `{ts}` ({who}): {a.body or '—'}")
    else:
        out.append("_(none flagged)_")
    out += ["", "## Root cause", "", "_(to be completed)_", "", "## Follow-ups", "", "_(to be completed)_", ""]
    return "\n".join(out)
