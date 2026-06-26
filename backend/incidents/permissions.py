"""
Authorization (ADR-008).

A user may ACK / ESCALATE / RESOLVE an incident iff they hold the incident's
`current_tier` role OR any higher tier (senior override). Role-based, not
per-assignee. Tiers are Django Groups named "T1" / "T2" / "T3".

This is the guard in front of the SendTaskSuccess path (ADR-007); combined with
the expected-tier optimistic-concurrency check in views, a stale action is still
rejected.
"""
from rest_framework.permissions import BasePermission

from .models import TIER_ORDER, Tier


def user_tier_rank(user) -> int:
    """Highest tier the user holds, as an index into TIER_ORDER; -1 if none."""
    if not user or not user.is_authenticated:
        return -1
    if user.is_superuser:
        return len(TIER_ORDER) - 1
    held = set(user.groups.values_list("name", flat=True))
    ranks = [i for i, t in enumerate(TIER_ORDER) if t.value in held]
    return max(ranks) if ranks else -1


def can_act_on(user, incident) -> bool:
    """True iff the user's tier is at or above the incident's current tier."""
    required = TIER_ORDER.index(Tier(incident.current_tier))
    return user_tier_rank(user) >= required


class CanActOnIncident(BasePermission):
    message = "You must hold the incident's current tier role or higher to act on it."

    def has_object_permission(self, request, view, obj):
        return can_act_on(request.user, obj)
