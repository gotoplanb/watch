"""
Operating mode service (ADR-035) — highway / race as audited domain state, not a flag.

`current_mode()` is the single read path: the latest open OperatingModeWindow's mode, or
HIGHWAY when none is open (highway is the ambient state and needs no row). Race windows are
declared with an actor + reason and closed with an explicit all-clear, matching the operations
manual's "declared start and all-clear". Decision functions receive mode as a parameter
(never read it themselves) so race-mode behavior later is a code change, not plumbing.
"""
from django.utils import timezone

from . import events
from .models import OperatingMode, OperatingModeWindow


def current_mode() -> str:
    """The active operating mode — the latest open window's mode, defaulting to highway."""
    window = OperatingModeWindow.objects.filter(ended_at__isnull=True).first()
    return window.mode if window else OperatingMode.HIGHWAY


def open_race_window(actor: str, reason: str = "") -> OperatingModeWindow:
    """Declare race mode (idempotent — an already-open race window is returned, not duplicated)."""
    window = OperatingModeWindow.objects.filter(
        mode=OperatingMode.RACE, ended_at__isnull=True
    ).first()
    if window:
        return window
    window = OperatingModeWindow.objects.create(
        mode=OperatingMode.RACE, actor=actor, reason=reason
    )
    events.emit("mode.race.opened", {
        "window_id": str(window.id), "actor": actor, "reason": reason,
        "started_at": window.started_at.isoformat(),
    })
    return window


def close_race_window(actor: str) -> OperatingModeWindow | None:
    """Declare the all-clear: close the open race window (no-op when none is open)."""
    window = OperatingModeWindow.objects.filter(
        mode=OperatingMode.RACE, ended_at__isnull=True
    ).first()
    if window is None:
        return None
    window.ended_at = timezone.now()
    window.save(update_fields=["ended_at"])
    events.emit("mode.race.closed", {
        "window_id": str(window.id), "actor": actor, "ended_at": window.ended_at.isoformat(),
    })
    return window
