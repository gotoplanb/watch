"""
Transition service — the one place that mutates incident lifecycle state and writes
the matching append-only audit record (ADR-007). Manual (API) and automatic
(Lambda timeout) paths both call through here so the Transition shape is identical
regardless of *how* it happened (spec §3).

Every transition is idempotent: "act if still applicable", never blind "act"
(ADR-001). Callers wrap these in select_for_update where concurrency matters.
"""
import hashlib
import hmac
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import apikeys, events, flags, notify, rca_ai
from .models import (
    Annotation,
    EventType,
    Incident,
    LinkKind,
    OnCallShift,
    Page,
    PageStatus,
    Problem,
    Rca,
    RecordLink,
    Status,
    Tier,
    TimelineEvent,
    Transition,
    next_tier,
)

logger = logging.getLogger(__name__)


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


def page_on_tier_entry(incident, tier) -> None:
    """Page the current on-call when an incident ENTERS a tier — a new incident at T1 or an escalate
    to T2/T3, never on ACK/RESOLVE (ADR-013). Deferred to after the transaction commits: paging is
    fire-and-forget, so a paging failure never rolls back or blocks the escalation."""
    transaction.on_commit(lambda: _page(incident, tier))


def paging_topic(kind: str, ident, seed: str = "") -> str:
    """The ntfy topic for a paging target (ADR-013). `kind` is 'user' or 'tier'. When
    NTFY_TOPIC_SECRET is set, an HMAC suffix makes each topic **independently** unguessable from the
    (public) source, and the secret itself never appears in the string. Empty secret → the plain
    topic (local default), so nothing breaks before it's configured. A per-user `seed` (ADR-030) is
    mixed in for the user topic so rotating the user's keyring rolls it; tier topics pass no seed
    (shared, not one person's to rotate) and are unaffected."""
    env = settings.PAGING_ENV
    base = f"watch-{env}-{kind}-{ident}"
    secret = settings.NTFY_TOPIC_SECRET
    if not secret:
        return base
    msg = f"{env}:{kind}:{ident}" + (f":{seed}" if seed else "")
    digest = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{base}-{digest[:12]}"


def _page(incident, tier) -> None:
    try:
        # Rollout gate (ADR-014): keyed on the incident so `sample:R` keeps a given incident
        # consistently in-or-out across all its tier entries.
        if not flags.active("paging_enabled", key=str(incident.id)):
            return
        shift = current_on_call(tier)
        user = shift.user if shift else None
        # Per-user topic, falling back to the tier topic when the rota has a gap (ADR-013).
        topic = paging_topic("user", user.id, seed=apikeys.seed_for(user)) if user else paging_topic("tier", tier)
        who = f"@{user.username}" if user else f"{tier} on-call (rota gap)"
        title = f"[{tier}] {incident.title}"[:110]
        message = f"{who} — incident at {tier}\n{incident.title}\nid {incident.id}"
        ok, err = notify.send(topic, title, message, priority="high", tags=["rotating_light"])
        Page.objects.create(
            incident=incident, tier=tier, topic=topic, target=user,
            status=PageStatus.SENT if ok else PageStatus.FAILED, error="" if ok else err,
        )
    except Exception:  # pragma: no cover - defensive: paging must never break the domain
        logger.warning("paging failed for incident %s", getattr(incident, "id", "?"), exc_info=True)


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
    page_on_tier_entry(incident, target.value)  # page the new tier's on-call (ADR-013)
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

def add_note(record, actor: str, body: str) -> TimelineEvent:
    """Human note (work note) on a record's timeline — incident/problem/rca (ADR-031)."""
    return TimelineEvent.objects.create(
        record=record, type=EventType.NOTE, actor=actor, body=body
    )


def post_system_event(record, body: str, data: dict | None = None) -> TimelineEvent:
    """Engine/automation narrative event — complements a Transition (which stays the audit record)."""
    return TimelineEvent.objects.create(
        record=record, type=EventType.SYSTEM, actor="system", body=body, data=data or {}
    )


def post_ai_event(record, body: str, actor: str = "argus", data: dict | None = None) -> TimelineEvent:
    """AI-assisted finding on the timeline (§8 / #17) — the hook the AI agent posts through, so its
    findings land in the record's history and feed the RCA."""
    return TimelineEvent.objects.create(
        record=record, type=EventType.AI, actor=actor, body=body, data=data or {}
    )


def annotate_event(target, *, author, body: str = "", tag: str = "note") -> Annotation:
    """Attach an annotation/tag to ANY timeline event — a Transition or a TimelineEvent. Orthogonal
    to the event; never mutates it, so an authoritative Transition stays intact."""
    return Annotation.objects.create(target=target, author=author, body=body, tag=tag)


def timeline(record):
    """Merged, time-ordered history for any record (ADR-031): its TimelineEvents, plus Transitions
    when the record has them (incidents only — Transitions stay incident-only). Each item carries its
    annotations prefetched. Items: {kind: transition|event, at, obj, target: 'kind:id'}."""
    items = []
    transitions = getattr(record, "transitions", None)
    if transitions is not None:  # incidents only
        items += [
            {"kind": "transition", "at": t.at, "obj": t, "target": f"transition:{t.id}"}
            for t in transitions.prefetch_related("annotations__author")
        ]
    items += [
        {"kind": "event", "at": e.occurred_at, "obj": e, "target": f"event:{e.id}"}
        for e in record.events.prefetch_related("annotations__author")
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


def seed_rca(*, title: str = "", incident=None, actor: str = "system"):
    """Create a stored RCA record (ADR-031). When seeded from an incident, its `document` starts as the
    assembled annotated timeline (rca_markdown) and is then hand-edited; otherwise it starts blank. The
    provenance lands as a system timeline event (the durable incident↔RCA link is a RecordLink later)."""
    from .models import Rca
    document = rca_markdown(incident) if incident is not None else ""
    if not title:
        title = f"RCA — {incident.title}" if incident is not None else "Untitled RCA"
    rca = Rca.objects.create(title=title, document=document)
    if incident is not None:
        post_system_event(rca, f"Seeded from incident {incident.number or incident.id} by {actor}")
    return rca


RCA_AI_FLAG = "rca_ai_draft"


def draft_rca(rca, *, actor: str = "system"):
    """AI-draft an RCA's document from its assembled timeline via the configured provider
    (ADR-033/034: stub | bedrock | conduct).

    The draft consumes whatever is in the RCA document — on a freshly seeded RCA that's the
    `rca_markdown` assembly; on a re-draft it's the current working copy. The model output
    *replaces* the document (a reviewable starting point the human then edits), and provenance
    (the provider + the model that actually ran) lands as a system timeline event. Flag-gating
    (`RCA_AI_FLAG`) is the caller's job — the UI hides the control and the view rejects when the
    flag is off (ADR-003). Propagates rca_ai.DraftError so the view can surface a friendly
    message instead of a 500.
    """
    result = rca_ai.draft(rca.document or "")
    rca.document = result.text
    rca.save(update_fields=["document", "updated_at"])
    post_system_event(rca, f"AI-drafted via {result.provider} ({result.model}) by {actor}")
    return rca


# --- Generic record links (ADR-031) — Jira-style issue-links across record types ---

# Human number prefix → model. Records without a number (e.g. Check probes) aren't resolvable
# by number and are linkable only programmatically for now.
_LINK_MODELS = {"INC": Incident, "PRB": Problem, "RCA": Rca}


def record_for_number(number: str):
    """Resolve a human record number (INC-/PRB-/RCA-) to its record, or None.

    Forgiving: case-insensitive, and the numeric part is normalized to the canonical 4-digit
    zero-padded form (numbering.py uses ``:04d``), so ``inc-7`` / ``INC-007`` / ``INC-0007`` all
    resolve to ``INC-0007``.
    """
    number = (number or "").strip().upper()
    prefix, _, num = number.partition("-")
    model = _LINK_MODELS.get(prefix)
    if model is None:
        return None
    num = num.strip()
    if num.isdigit():
        number = f"{prefix}-{int(num):04d}"
    return model.objects.filter(number=number).first()


def _label(record) -> str:
    return getattr(record, "number", None) or str(getattr(record, "id", record))


def link_records(from_record, to_record, *, kind: str, actor: str = "system"):
    """Create a directed link (from_record `kind` to_record) and narrate it on both timelines.
    Returns (link, created). Idempotent on the exact (from,to,kind) tuple; refuses self-links."""
    from django.contrib.contenttypes.models import ContentType

    if kind not in LinkKind.values:
        kind = LinkKind.RELATES_TO
    from_ct = ContentType.objects.get_for_model(from_record)
    to_ct = ContentType.objects.get_for_model(to_record)
    if from_ct == to_ct and str(from_record.pk) == str(to_record.pk):
        return None, False  # no self-links
    link, created = RecordLink.objects.get_or_create(
        from_content_type=from_ct, from_object_id=str(from_record.pk),
        to_content_type=to_ct, to_object_id=str(to_record.pk), kind=kind,
        defaults={"created_by": None},
    )
    if created:
        label = dict(LinkKind.choices)[kind]
        post_system_event(from_record, f"Linked — {label} {_label(to_record)} (by {actor})")
        post_system_event(to_record, f"Linked — {_label(from_record)} {label} this (by {actor})")
    return link, created


def links_for(record):
    """All links touching `record`, each as a display row: {id, kind, kind_label, direction, other,
    other_label}. `direction` is 'out' (record is the from side) or 'in' (record is the to side)."""
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(record)
    oid = str(record.pk)
    labels = dict(LinkKind.choices)
    rows = []
    qs = RecordLink.objects.filter(from_content_type=ct, from_object_id=oid).select_related(
        "to_content_type"
    ) | RecordLink.objects.filter(to_content_type=ct, to_object_id=oid).select_related(
        "from_content_type"
    )
    for link in qs:
        outgoing = link.from_content_type_id == ct.id and link.from_object_id == oid
        other = link.to_record if outgoing else link.from_record
        if other is None:  # pragma: no cover - dangling GFK (target deleted)
            continue
        rows.append({
            "id": link.id, "kind": link.kind, "kind_label": labels[link.kind],
            "direction": "out" if outgoing else "in", "other": other, "other_label": _label(other),
        })
    return rows


def unlink(link_id) -> bool:
    """Delete a link by id. Returns True if one was removed."""
    deleted, _ = RecordLink.objects.filter(pk=link_id).delete()
    return bool(deleted)
