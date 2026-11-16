"""Planned maintenance windows (ADR-064).

A signal during planned work is expected noise, but not ignorable: customers can still be hurt more
than planned. So signals in scope still open or attach to assessments, each annotated with the
window, and automatic promotion is held while it lasts. Degradation inside a window parks the
assessment in monitoring with a recheck just after the window ends, when the normal rules apply.
A person can still promote at any time.
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from . import assessments
from .models import MaintenanceWindow, ServiceDependency

MAINTENANCE_ACTOR = "system:maintenance"
RECHECK_AFTER = timedelta(minutes=1)


def active_for(row, at=None):
    """The maintenance window covering this assessment now, if any."""
    at = at or timezone.now()
    suppliers = {row.supplier} - {""}
    if row.customer and row.service:
        suppliers |= set(ServiceDependency.objects.filter(
            active=True, customer=row.customer, service=row.service).values_list("supplier", flat=True))
    ours = Q(supplier="") & (Q(customer="") | Q(customer=row.customer)) & (Q(service="") | Q(service=row.service))
    covers = Q(supplier__in=suppliers) & (Q(customer="") | Q(customer=row.customer)) & (Q(service="") | Q(service=row.service))
    # A window scoped to nothing at all would silence everything; never match it.
    scoped = ~Q(supplier="", customer="", service="")
    return (MaintenanceWindow.objects.filter(scoped, starts_at__lte=at, ends_at__gt=at)
            .filter(covers | (ours & ~Q(customer="", service=""))).order_by("ends_at").first())


def annotate(row, window):
    """Note on the assessment, once per window, that its signals arrived during planned work."""
    if row.history.filter(kind="maintenance_window", data__window_id=window.pk).exists():
        return
    assessments.append(
        row, "maintenance_window",
        f"Signal during planned maintenance until {window.ends_at.isoformat()}: {window.reason}. "
        "Automatic promotion is held until it ends; a responder can still promote.",
        MAINTENANCE_ACTOR, {"window_id": window.pk, "ends_at": window.ends_at.isoformat()})


def hold_promotion(row, window, reason):
    """Degradation inside a window: monitor, and recheck right after the window ends."""
    assessments.dispose(row.pk, actor=MAINTENANCE_ACTOR, outcome="monitoring",
                        reason=f"Degradation during planned maintenance ({window.reason}); promotion "
                               f"held until {window.ends_at.isoformat()}. {reason}")
    row.next_recheck_at = window.ends_at + RECHECK_AFTER
    row.save(update_fields=["next_recheck_at"])
