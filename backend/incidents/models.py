"""
Domain model for the v1 slice.

Lifecycle is two orthogonal fields (ADR-007): `status` x `current_tier`, plus
`acknowledged_at`. The old linear enum maps as:
    NEW        = (OPEN, T1, acknowledged_at=None)
    TRIAGED_T1 = (OPEN, T1, acknowledged_at=<set>)
RESOLVED is reachable from any tier.
"""
import uuid

from django.conf import settings
from django.db import models


class Status(models.TextChoices):
    OPEN = "OPEN", "Open"
    RESOLVED = "RESOLVED", "Resolved"


class Tier(models.TextChoices):
    T1 = "T1", "Tier 1"
    T2 = "T2", "Tier 2"
    T3 = "T3", "Tier 3"


# Ordering used by both escalation (next tier) and authz (tier-or-above, ADR-008).
TIER_ORDER = [Tier.T1, Tier.T2, Tier.T3]


def next_tier(tier: str):
    """The tier above `tier`, or None if already at the top (T3)."""
    idx = TIER_ORDER.index(Tier(tier))
    return TIER_ORDER[idx + 1] if idx + 1 < len(TIER_ORDER) else None


class Incident(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Intake
    source = models.CharField(max_length=128)
    payload = models.JSONField(default=dict)
    title = models.CharField(max_length=512)
    # Idempotency key (ADR-009): source event id, else sha256(normalize(payload)).
    dedupe_key = models.CharField(max_length=128)

    # Lifecycle (ADR-007)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    current_tier = models.CharField(max_length=8, choices=Tier.choices, default=Tier.T1)
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    # Escalation engine handles (ADR-007). Exactly one outstanding task token per
    # incident — the current tier's waitForTaskToken — so a single field suffices.
    sla_deadline_at = models.DateTimeField(null=True, blank=True)
    escalation_execution_arn = models.CharField(max_length=256, blank=True, default="")
    current_task_token = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # ADR-009: dedupe is scoped to the open incident. Retries while OPEN
            # collide and no-op; a re-fire after RESOLVED can open a new incident.
            models.UniqueConstraint(
                fields=["dedupe_key"],
                condition=models.Q(status=Status.OPEN),
                name="uniq_open_dedupe_key",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "current_tier"], name="incidents_status_tier_idx"),
        ]

    def __str__(self):
        return f"{self.id} [{self.status}/{self.current_tier}] {self.title}"


class Transition(models.Model):
    """
    Append-only audit record (spec §3). Manual and automatic escalations write the
    SAME shape — the trail is agnostic to *how* a transition happened.
    `actor` is a user id (str) or the sentinel `system:auto-escalation`.
    """

    SYSTEM_ACTOR = "system:auto-escalation"

    id = models.BigAutoField(primary_key=True)
    incident = models.ForeignKey(Incident, related_name="transitions", on_delete=models.CASCADE)

    from_status = models.CharField(max_length=16, choices=Status.choices)
    from_tier = models.CharField(max_length=8, choices=Tier.choices)
    to_status = models.CharField(max_length=16, choices=Status.choices)
    to_tier = models.CharField(max_length=8, choices=Tier.choices)

    actor = models.CharField(max_length=128)
    reason = models.CharField(max_length=512, blank=True, default="")
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "id"]

    def __str__(self):
        return f"{self.incident_id}: {self.from_tier}->{self.to_tier} by {self.actor}"


class Comment(models.Model):
    """Investigator note on an incident (internal — this tool runs alongside
    ServiceNow as the working surface). Renders in the incident timeline next to
    Transitions, ordered by time."""

    id = models.BigAutoField(primary_key=True)
    incident = models.ForeignKey(Incident, related_name="comments", on_delete=models.CASCADE)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        who = self.author.username if self.author else "unknown"
        return f"{self.incident_id}: comment by {who}"
