"""Copy Grafana IRM's on-call schedules into Watch, read-only (ADR-066 §7).

IRM is the system of record for who is on call, so that IRM can still page someone when Watch is
down. Watch keeps a copy in `OnCallShift` (source "irm") for assignment (ADR-012) and display.

Each sync reads every tier's final schedule from now to `IRM_SYNC_DAYS` out through the IRM app's
plugin API, then, per tier and in one transaction, replaces the copied shifts that haven't ended.
Shifts that already ended are left alone, so later edits in IRM don't rewrite history. IRM users are
matched to Watch users by email; a shift whose person has no Watch account is skipped and reported,
and that time is a gap in Watch.

If IRM can't be read, nothing is changed: Watch keeps its last copy. A successful sync reports to the
`irm-schedule` monitored source (ADR-062), so a copy that stops refreshing opens an assessment.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import heartbeats
from .models import MonitoredSource, OnCallShift, OperationsHeartbeat, Tier

logger = logging.getLogger(__name__)

SOURCE = "irm"
MONITORED_SOURCE = "irm-schedule"
TIMEOUT = 10


def schedule_ids() -> dict:
    """{"T1": "<IRM schedule id>", ...} from IRM_SCHEDULE_IDS ("T1:<id>,T2:<id>")."""
    ids = {}
    for part in settings.IRM_SCHEDULE_IDS.split(","):
        tier, _, schedule = part.strip().partition(":")
        if tier.strip() in Tier.values and schedule.strip():
            ids[tier.strip()] = schedule.strip()
    return ids


def enabled() -> bool:
    """The copy is on when IRM is configured; then shifts can't be edited in Watch."""
    return bool(settings.GRAFANA_IRM_URL and settings.GRAFANA_IRM_TOKEN and schedule_ids())


def fetch_events(schedule_id, start: datetime, days: int) -> list:
    """The schedule's final shifts (overrides applied) as IRM returns them."""
    url = (f"{settings.GRAFANA_IRM_URL.rstrip('/')}/api/plugins/grafana-irm-app/resources/"
           f"schedules/{schedule_id}/filter_events/")
    resp = requests.get(
        url, params={"type": "final", "date": start.date().isoformat(), "days": days},
        headers={"Authorization": f"Bearer {settings.GRAFANA_IRM_TOKEN}"}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("events", [])


@dataclass
class Plan:
    """What one tier's copy should hold: (user_id, start, end, IRM shift pk), plus what was skipped."""
    shifts: list = field(default_factory=list)
    unmatched: set = field(default_factory=set)


def _window(event, now):
    """(start, end) of an event that has someone on it and hasn't ended, else None."""
    if event.get("is_gap") or event.get("is_empty"):
        return None
    start, end = parse_datetime(event["start"]), parse_datetime(event["end"])
    if not (start and end) or end <= now:
        return None
    return start, end


def plan(events, users_by_email, now) -> Plan:
    """Turn IRM events into shifts. Gaps and empty slots carry no one; events that already ended are
    dropped; back-to-back events for the same person and rotation are joined into one shift."""
    result = Plan()
    for event in sorted(events, key=lambda e: e.get("start", "")):
        window = _window(event, now)
        if window is None:
            continue
        rotation = str((event.get("shift") or {}).get("pk") or "")
        for person in event.get("users", []):
            email = (person.get("email") or "").strip().lower()
            user_id = users_by_email.get(email)
            if user_id is None:
                result.unmatched.add(email or person.get("display_name") or "?")
            else:
                _add(result.shifts, (user_id, *window, rotation))
    return result


def _add(shifts, new):
    for i, (user_id, start, end, rotation) in enumerate(shifts):
        if (user_id, rotation) == (new[0], new[3]) and end == new[1]:
            shifts[i] = (user_id, start, new[2], rotation)
            return
    shifts.append(new)


def _users_by_email():
    users = {}
    for pk, email in User.objects.filter(is_active=True).exclude(email="").values_list("pk", "email"):
        users.setdefault(email.strip().lower(), pk)
    return users


def _replace(tier, tier_plan: Plan, now):
    """Swap a tier's copied shifts that haven't ended for the new plan."""
    with transaction.atomic():
        OnCallShift.objects.filter(tier=tier, source=SOURCE, ends_at__gt=now).delete()
        OnCallShift.objects.bulk_create([
            OnCallShift(tier=tier, user_id=user_id, starts_at=start, ends_at=end, source=SOURCE,
                        external_id=rotation)
            for user_id, start, end, rotation in tier_plan.shifts])


def sync(now=None) -> dict:
    """Copy every configured tier's schedule. All tiers are read before any is written, so a failed
    read leaves the whole copy as it was. Returns a summary; raises nothing."""
    now = now or timezone.now()
    ids = schedule_ids()
    try:
        events = {tier: fetch_events(schedule_id, now, settings.IRM_SYNC_DAYS)
                  for tier, schedule_id in ids.items()}
    except (requests.RequestException, ValueError) as exc:
        logger.warning("IRM schedule sync failed; keeping the last copy: %s", exc)
        return {"ok": False, "error": str(exc)[:300]}
    users = _users_by_email()
    summary = {"ok": True, "shifts": {}, "unmatched": []}
    unmatched = set()
    for tier, tier_events in events.items():
        tier_plan = plan(tier_events, users, now)
        _replace(tier, tier_plan, now)
        summary["shifts"][tier] = len(tier_plan.shifts)
        unmatched |= tier_plan.unmatched
    if unmatched:
        summary["unmatched"] = sorted(unmatched)
        logger.warning("IRM schedule: no Watch account for %s; their shifts are gaps in Watch",
                       ", ".join(summary["unmatched"]))
    MonitoredSource.objects.get_or_create(
        name=MONITORED_SOURCE,
        defaults={"max_silence_minutes": max(15, 3 * settings.IRM_SYNC_INTERVAL_SECONDS // 60)})
    heartbeats.seen(MONITORED_SOURCE, via="schedule sync")
    return summary


def sync_if_due(now=None):
    """Called by the operations sweep: sync when enabled and the interval has passed. The attempt
    is recorded first, so a slow or failing IRM is tried once per interval, not every tick."""
    if not enabled():
        return None
    now = now or timezone.now()
    with transaction.atomic():
        beat, created = OperationsHeartbeat.objects.select_for_update().get_or_create(
            name="irm-schedule-sync", defaults={"last_tick": now})
        if not created and now - beat.last_tick < timedelta(seconds=settings.IRM_SYNC_INTERVAL_SECONDS):
            return None
        beat.last_tick = now
        beat.save(update_fields=["last_tick"])
    summary = sync(now)
    OperationsHeartbeat.objects.filter(name="irm-schedule-sync").update(detail=summary)
    return summary
