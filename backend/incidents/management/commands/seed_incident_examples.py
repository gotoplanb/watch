"""Import local exercise snapshots without firing webhooks, paging, AI, or AWS calls."""
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from incidents.models import (
    Annotation, AnnotationTag, EventType, Incident, LinkKind, Problem, ProblemStatus,
    Rca, RcaStatus, RecordLink, Status, Tier, TimelineEvent, Transition,
)


def _required_text(row, key, limit=None):
    value = row.get(key)
    if not isinstance(value, str) or not value.strip() or (limit and len(value) > limit):
        raise ValueError(f"{key} must be nonempty text" + (f" of at most {limit} characters" if limit else ""))
    return value


_SLUG = r"[a-z0-9-]+"
_ACTIONS = {"ack", "escalate", "resolve"}
_STEP_KINDS = {"note", "handoff"} | _ACTIONS


def _tier_or_higher(tier):
    return [f"T{n}" for n in range(tier, 4)]


def _validate_header(document):
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("Expected a version 1 scenario document")
    collection = _required_text(document, "collection", 50)
    if not re.fullmatch(_SLUG, collection):
        raise ValueError("collection must contain lowercase letters, digits, or hyphens")
    scenarios = document.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("scenarios must be a nonempty list")
    return collection, scenarios


def _validate_records(row, key):
    for field in ("title", "summary", "source_reference"):
        _required_text(row, field, 490 if field == "title" else None)
    if not isinstance(row.get("facts", {}), dict):
        raise ValueError(f"{key}: facts must be an object")
    for record, statuses, content in (("problem", ProblemStatus.values, "description"),
                                      ("rca", RcaStatus.values, "document")):
        detail = row[record]
        _required_text(detail, "title", 490)
        _required_text(detail, content)
        if detail["status"] not in statuses:
            raise ValueError(f"{key}: invalid {record} status")
    if row["rca"]["status"] == RcaStatus.FINAL:
        raise ValueError("Exercise RCAs cannot be marked final/approved")


class _StepState:
    """Replays the modeled lifecycle so each step is checked against the state it acts on."""

    def __init__(self):
        self.previous, self.tier, self.resolved, self.acked = -1, 1, False, False


def _validate_step_shape(key, step, users, state):
    minute = step["minute"]
    if type(minute) is not int or minute < state.previous or minute < 0:
        raise ValueError(f"{key}: step minutes must be nonnegative and chronological")
    state.previous = minute
    kind = step["kind"]
    if kind not in _STEP_KINDS:
        raise ValueError(f"{key}: unsupported step kind {kind}")
    _required_text(step, "body", 512 if kind in _ACTIONS else None)
    if step["actor"] not in users:
        raise ValueError(f"Missing demo user {step['actor']}; run make seed-dev first")
    return kind


def _validate_action(key, kind, user, state):
    allowed = user.is_superuser or user.groups.filter(name__in=_tier_or_higher(state.tier)).exists()
    if not allowed or state.resolved:
        raise ValueError(f"{key}: action is not applicable to its actor/state")
    if kind == "ack":
        if state.acked:
            raise ValueError(f"{key}: duplicate ACK at the same tier")
        state.acked = True
    if kind == "escalate":
        state.tier, state.acked = state.tier + 1, False
        if state.tier > 3:
            raise ValueError(f"{key}: cannot escalate beyond T3")
    if kind == "resolve":
        state.resolved = True


def _validate_annotations(key, step):
    for annotation in step.get("annotations", []):
        if annotation["tag"] not in AnnotationTag.values:
            raise ValueError(f"{key}: invalid annotation tag")
        _required_text(annotation, "body")


def _validate_steps(key, steps, users):
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{key}: steps must be a nonempty list")
    state = _StepState()
    for step in steps:
        kind = _validate_step_shape(key, step, users, state)
        if kind in _ACTIONS:
            _validate_action(key, kind, users[step["actor"]], state)
        _validate_annotations(key, step)
    return state.tier


def _validate_scenario(row, keys, users):
    key = _required_text(row, "key", 50)
    if not re.fullmatch(_SLUG, key) or key in keys:
        raise ValueError(f"Invalid or duplicate scenario key: {key}")
    keys.add(key)
    _validate_records(row, key)
    tier = _validate_steps(key, row["steps"], users)
    if row["assignee"] not in users:
        raise ValueError(f"{key}: unknown assignee")
    if not users[row["assignee"]].groups.filter(name__in=_tier_or_higher(tier)).exists():
        raise ValueError(f"{key}: assignee must hold the final tier or higher")


def _validate(document, users):
    collection, scenarios = _validate_header(document)
    keys = set()
    for row in scenarios:
        _validate_scenario(row, keys, users)
    return collection, scenarios


def _note(incident, step, actor, at):
    # A modeled human handoff is a note, never a claim that a live AI wrote it.
    kind = "exercise_handoff" if step["kind"] == "handoff" else "exercise_note"
    return TimelineEvent.objects.create(
        record=incident, type=EventType.NOTE, actor=actor.username,
        body=step["body"], occurred_at=at, data={"fixture": True, "kind": kind},
    )


def _next_acknowledged(kind, at, acknowledged):
    if kind == "ack":
        return at
    if kind == "escalate":
        return None
    return acknowledged


def _transition(incident, step, actor, at, tier, status):
    kind = step["kind"]
    next_tier = f"T{int(tier[1]) + 1}" if kind == "escalate" else tier
    next_status = Status.RESOLVED if kind == "resolve" else status
    target = Transition.objects.create(
        incident=incident, from_status=status, from_tier=tier,
        to_status=next_status, to_tier=next_tier, actor=str(actor.pk), reason=step["body"],
    )
    # These are imported exercise snapshots, not live engine transitions.
    Transition.objects.filter(pk=target.pk).update(at=at)
    return target, next_tier, next_status


def _timeline(incident, steps, users, start):
    tier, status, acknowledged = Tier.T1, Status.OPEN, None
    for step in steps:
        at = start + timedelta(minutes=step["minute"])
        kind, actor = step["kind"], users[step["actor"]]
        if kind in {"note", "handoff"}:
            target = _note(incident, step, actor, at)
        else:
            target, tier, status = _transition(incident, step, actor, at, tier, status)
            acknowledged = _next_acknowledged(kind, at, acknowledged)
        for annotation in step.get("annotations", []):
            Annotation.objects.create(target=target, author=actor, **annotation)
    Incident.objects.filter(pk=incident.pk).update(
        current_tier=tier, status=status, acknowledged_at=acknowledged,
        created_at=start, updated_at=at,
    )


class Command(BaseCommand):
    help = "Load local incident exercises and linked Problems/RCAs; reruns preserve existing examples."

    def add_arguments(self, parser):
        parser.add_argument("fixture", type=Path)

    def handle(self, *args, **options):
        if not settings.DEBUG or not settings.ESCALATION_LOCAL_MODE:
            raise CommandError("Example import requires DEBUG and ESCALATION_LOCAL_MODE enabled")
        try:
            document = json.loads(options["fixture"].read_text())
            users = {u.username: u for u in User.objects.prefetch_related("groups")}
            collection, scenarios = _validate(document, users)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CommandError(f"Invalid scenario file: {exc}") from exc

        created, skipped = 0, 0
        with transaction.atomic():
            for row in scenarios:
                identity = f"watch-exercise:{collection}:{row['key']}"
                pk = uuid.uuid5(uuid.NAMESPACE_URL, identity)
                if Incident.objects.filter(pk=pk).exists():
                    skipped += 1
                    continue
                last_minute = row["steps"][-1]["minute"]
                start = timezone.now() - timedelta(minutes=last_minute + 5)
                metadata = {
                    "fixture": True, "collection": collection, "scenario": row["key"],
                    "source_reference": row["source_reference"], "facts": row.get("facts", {}),
                    "notice": "Modeled exercise: response steps and recent timestamps are synthetic; not a historical audit or live telemetry.",
                }
                incident = Incident.objects.create(
                    pk=pk, source="local:exercise", title=f"[EXAMPLE] {row['title']}",
                    dedupe_key=identity, payload=metadata, assignee=users[row["assignee"]],
                )
                TimelineEvent.objects.create(
                    record=incident, type=EventType.SYSTEM, actor="system:fixture",
                    body=f"LOCAL EXERCISE — {row['summary']}\n{metadata['notice']}\nSource: {row['source_reference']}",
                    occurred_at=start, data=metadata,
                )
                _timeline(incident, row["steps"], users, start)
                incident.refresh_from_db()
                owner = users[row["assignee"]]
                problem = Problem.objects.create(
                    pk=uuid.uuid5(pk, "problem"), title=f"[EXAMPLE] {row['problem']['title']}",
                    description=row["problem"]["description"], status=row["problem"]["status"],
                    assignee=owner, data=metadata,
                )
                rca = Rca.objects.create(
                    pk=uuid.uuid5(pk, "rca"), title=f"[EXAMPLE] {row['rca']['title']}",
                    document=row["rca"]["document"], status=row["rca"]["status"],
                    assignee=owner, data=metadata,
                )
                for record in (problem, rca):
                    TimelineEvent.objects.create(
                        record=record, type=EventType.SYSTEM, actor="system:fixture",
                        body=f"Local exercise linked to {incident.number}. {metadata['notice']}", data=metadata,
                    )
                # A hypothesis is a related Problem, not a confirmed causal assertion.
                for source, target in ((incident, problem), (rca, incident), (rca, problem)):
                    RecordLink.objects.create(from_record=source, to_record=target,
                                              kind=LinkKind.RELATES_TO, created_by=owner)
                created += 1
                self.stdout.write(f"{incident.number} {incident.status}/{incident.current_tier}: {incident.title}\n"
                                  f"  /ui/incidents/{incident.pk}/ · {problem.number} · {rca.number}")
        self.stdout.write(self.style.SUCCESS(f"Created {created} examples; preserved {skipped} existing examples."))
