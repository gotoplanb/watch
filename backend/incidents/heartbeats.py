"""Noticing when a monitoring source goes quiet (ADR-062).

Uptime Robot, Sumo and status pages only speak when something changes, so a broken webhook or an
expired plan looks exactly like "all fine". A MonitoredSource proves it's alive by calling its
heartbeat URL on a schedule (Uptime Robot can poll it like any monitored URL; Sumo can hit it from
a scheduled search), or by sending any signal. Silence beyond its limit opens an assessment, and
the next sign of life records the recovery on it (ADR-059 correlation).
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import MonitoredSource

SOURCE = "watch"


def _key(source: MonitoredSource) -> str:
    return f"silence:{source.name}"


def _signal(source, state, signal):
    from . import automation  # automation imports this module

    return automation.receive_signal(
        title=f"{source.name} has gone quiet", source=SOURCE, signal=signal,
        correlation_key=_key(source), state=state,
        payload={"monitored_source": source.name, "max_silence_minutes": source.max_silence_minutes})


def seen(name: str, *, via: str = "signal"):
    """Record a sign of life from a source; if it had gone quiet, record the recovery."""
    now = timezone.now()
    with transaction.atomic():
        source = MonitoredSource.objects.select_for_update().filter(name=name, active=True).first()
        if source is None:
            return None
        was_silent = source.silent_since
        source.last_seen_at, source.silent_since = now, None
        source.save(update_fields=["last_seen_at", "silent_since"])
    if was_silent:
        quiet = int((now - was_silent).total_seconds() // 60)
        _signal(source, "resolved", f"{source.name} is reporting again ({via}) after about {quiet} "
                                    "minutes of silence. Check whether anything was missed meanwhile.")
    return source


def check_silence(pk, now=None) -> bool:
    """Open an assessment for a source that has been quiet past its limit (once per silence)."""
    now = now or timezone.now()
    source = MonitoredSource.objects.select_for_update().get(pk=pk)
    last = source.last_seen_at or source.created_at
    if not source.active or source.silent_since or now - last <= timedelta(minutes=source.max_silence_minutes):
        return False
    source.silent_since = now
    source.save(update_fields=["silent_since"])
    seen_text = f"last heard from at {source.last_seen_at.isoformat()}" if source.last_seen_at else "never heard from"
    transaction.on_commit(lambda: _signal(
        source, "active",
        f"No heartbeat or signal from {source.name} for over {source.max_silence_minutes} minutes "
        f"({seen_text}). Alerts from it may be going missing: check its plan, webhook and heartbeat."))
    return True


def overdue(now=None):
    """Active sources that may have gone quiet but aren't flagged yet (for the sweep)."""
    now = now or timezone.now()
    return [s.pk for s in MonitoredSource.objects.filter(active=True, silent_since__isnull=True)
            if now - (s.last_seen_at or s.created_at) > timedelta(minutes=s.max_silence_minutes)]
