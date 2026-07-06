"""
Domain model for the v1 slice.

Lifecycle is two orthogonal fields (ADR-007): `status` x `current_tier`, plus
`acknowledged_at`. The old linear enum maps as:
    NEW        = (OPEN, T1, acknowledged_at=None)
    TRIAGED_T1 = (OPEN, T1, acknowledged_at=<set>)
RESOLVED is reachable from any tier.
"""
import secrets
import uuid

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

# Environment label (ADR-028) — a free but bounded string; prod/nonprod today, a new label just works.
ENV_LABEL = RegexValidator(r"^[a-z0-9-]+$", "environment must be lowercase letters, digits, or hyphens")


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

    # Annotations (ADR-021) can target a Transition too — mark up an authoritative state change
    # for RCA without mutating it. GenericRelation is read-only sugar over the Annotation GFK.
    annotations = GenericRelation("Annotation", related_query_name="transition")

    class Meta:
        ordering = ["at", "id"]

    def __str__(self):
        return f"{self.incident_id}: {self.from_tier}->{self.to_tier} by {self.actor}"


class EventType(models.TextChoices):
    NOTE = "note", "Note"        # human message (actor = username)
    SYSTEM = "system", "System"  # escalation-engine narrative
    AI = "ai", "AI triage"       # AI-assisted triage finding (§8, #17)


class TimelineEvent(models.Model):
    """A non-transition entry on an incident's timeline (ADR-021, replaces the flat Comment).
    `note` = human message; `system` = escalation-engine narrative (auto-escalation / SLA / paging);
    `ai` = AI-assisted triage finding. Merged with Transitions into the incident timeline and
    annotatable like any event. `data` holds structured detail (e.g. from/to tier, sla_seconds)."""

    id = models.BigAutoField(primary_key=True)
    incident = models.ForeignKey(Incident, related_name="events", on_delete=models.CASCADE)
    type = models.CharField(max_length=8, choices=EventType.choices, default=EventType.NOTE)
    actor = models.CharField(max_length=128, blank=True, default="")  # username / system:… / argus
    body = models.TextField(blank=True, default="")
    data = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    annotations = GenericRelation("Annotation", related_query_name="timeline_event")

    class Meta:
        ordering = ["occurred_at", "id"]

    def __str__(self):
        return f"{self.incident_id}: {self.type} by {self.actor or 'system'}"


class AnnotationTag(models.TextChoices):
    NOTE = "note", "Note"
    UNEXPECTED = "unexpected", "Unexpected"
    ROOT_CAUSE = "root-cause", "Root cause"
    CONTRIBUTING = "contributing", "Contributing"


class Annotation(models.Model):
    """A human note/tag attached to ANY timeline event — a Transition or a TimelineEvent (ADR-021).
    Orthogonal to the event's own authorship: an authoritative Transition can be marked up for RCA
    ("this shouldn't have happened" / "root cause here") without touching the escalation write-path.
    Targets its event via a GenericForeignKey."""

    id = models.BigAutoField(primary_key=True)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    target = GenericForeignKey("content_type", "object_id")

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    body = models.TextField(blank=True, default="")
    tag = models.CharField(max_length=16, choices=AnnotationTag.choices, default=AnnotationTag.NOTE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self):
        return f"annotation[{self.tag}] by {self.author.username if self.author else 'unknown'}"


class OnCallShift(models.Model):
    """On-call schedule (ADR-012). Defines *responsibility* — who is on-call for a tier
    during a window — distinct from Group membership (which defines *capability*/authz).
    `services.current_on_call(tier)` resolves the active shift; the engine auto-assigns
    the incident to it on tier entry. A gap leaves the incident unassigned (still
    actionable by any tier-or-above member)."""

    id = models.BigAutoField(primary_key=True)
    tier = models.CharField(max_length=8, choices=Tier.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="on_call_shifts", on_delete=models.CASCADE
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at", "-id"]
        indexes = [models.Index(fields=["tier", "starts_at", "ends_at"])]

    def __str__(self):
        return f"{self.tier} {self.user}: {self.starts_at:%b %d %H:%M}–{self.ends_at:%b %d %H:%M}"


class CheckSubjectKind(models.TextChoices):
    SESSION = "session", "Session"  # subject_hash = the non-secret session correlation id
    USER = "user", "User"           # subject_hash = HMAC(SESSION_USER_HMAC_KEY, user/customer id)


class CheckStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    DONE = "done", "Done"
    INDETERMINATE = "indeterminate", "Indeterminate"


class CheckSource(models.TextChoices):
    PARTNER = "partner", "Partner"
    E2E = "e2e", "E2E"
    MANUAL = "manual", "Manual"
    SELF_REPORT = "self_report", "Self-report"  # public status-page form (ADR-027)


class SessionCheck(models.Model):
    """On-demand error-span lookup for a session or user (ADR-022) — the inverse of an incident
    ("go look for problems", not "a human declared one"). Stores only hashes; no plaintext PII."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject_kind = models.CharField(max_length=8, choices=CheckSubjectKind.choices)
    subject_hash = models.CharField(max_length=128)  # session correlation id, or HMAC of a user id
    window_from = models.DateTimeField(null=True, blank=True)
    window_to = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=16, choices=CheckSource.choices, default=CheckSource.MANUAL)
    status = models.CharField(max_length=16, choices=CheckStatus.choices, default=CheckStatus.QUEUED)
    verdict = models.CharField(max_length=128, blank=True, default="")  # clean | errors_found:N | aged_out
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["subject_kind", "subject_hash"])]

    def __str__(self):
        return f"check {self.subject_kind}:{self.subject_hash[:12]} [{self.status}]"


class ErrorSpan(models.Model):
    """An error span a SessionCheck found in the trace backend (ADR-022)."""

    id = models.BigAutoField(primary_key=True)
    session_check = models.ForeignKey(SessionCheck, related_name="error_spans", on_delete=models.CASCADE)
    trace_id = models.CharField(max_length=64)
    span_id = models.CharField(max_length=32, blank=True, default="")
    name = models.CharField(max_length=256, blank=True, default="")
    service = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(max_length=32, blank=True, default="")
    http_status = models.IntegerField(null=True, blank=True)
    ts = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["ts", "id"]

    def __str__(self):
        return f"error span {self.name or self.span_id} ({self.trace_id[:12]})"


class WebhookSubscription(models.Model):
    """A registered receiver of Watch's outbound events (ADR-023). `event_types` empty = all."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    url = models.URLField(max_length=512)
    secret = models.CharField(max_length=128)  # per-receiver HMAC signing key
    event_types = models.JSONField(default=list, blank=True)  # [] = all events
    active = models.BooleanField(default=True)
    description = models.CharField(max_length=256, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def matches(self, event_type: str) -> bool:
        return not self.event_types or event_type in self.event_types

    def __str__(self):
        return f"webhook -> {self.url}"


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"


class WebhookDelivery(models.Model):
    """One delivery attempt of an event to a subscription (ADR-023) — the outbound audit trail,
    mirror of the inbound intake record."""

    id = models.BigAutoField(primary_key=True)
    subscription = models.ForeignKey(
        WebhookSubscription, related_name="deliveries", on_delete=models.CASCADE
    )
    event_type = models.CharField(max_length=64)
    event_id = models.UUIDField()
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING)
    status_code = models.IntegerField(null=True, blank=True)
    attempts = models.IntegerField(default=0)
    error = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["event_type"]), models.Index(fields=["status"])]

    def __str__(self):
        return f"delivery {self.event_type} -> {self.subscription_id} [{self.status}]"


class PageStatus(models.TextChoices):
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class Page(models.Model):
    """Audit of an escalation page attempt (ADR-013) — one row per attempt, best-effort. Records the
    resolved on-call target (or the tier-topic fallback on a rota gap) and whether ntfy accepted it."""

    id = models.BigAutoField(primary_key=True)
    incident = models.ForeignKey(Incident, related_name="pages", on_delete=models.CASCADE)
    tier = models.CharField(max_length=2, choices=Tier.choices)
    topic = models.CharField(max_length=200)
    target = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    status = models.CharField(max_length=8, choices=PageStatus.choices)
    error = models.CharField(max_length=320, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["incident", "-created_at"])]

    def __str__(self):
        return f"page {self.topic} [{self.status}]"


class EnvStatus(models.Model):
    """Per-environment ops status (ADR-028). `payload` is stored VERBATIM — no schema; the posted JSON
    itself defines the groupings the display renders. History kept; the newest row per env is 'current'.
    Watch is a dumb store here — it never reasons about the payload shape."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    environment = models.CharField(max_length=64, validators=[ENV_LABEL])
    payload = models.JSONField()
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["environment", "-created_at"])]

    def __str__(self):
        return f"status {self.environment} @ {self.created_at:%Y-%m-%d %H:%M}"


class Digest(models.Model):
    """Per-environment health digest (ADR-028) — markdown, pasted into Slack. `special` (the 'speci'
    flag) marks an ad-hoc digest published during an incident vs the routine scheduled ones."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    environment = models.CharField(max_length=64, validators=[ENV_LABEL])
    content = models.TextField()
    title = models.CharField(max_length=200, blank=True, default="")
    special = models.BooleanField(default=False)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["environment", "-created_at"])]

    def __str__(self):
        flag = " [special]" if self.special else ""
        return f"digest {self.environment}{flag} @ {self.created_at:%Y-%m-%d %H:%M}"


def _gen_seed() -> str:
    return secrets.token_hex(16)


class UserKeyring(models.Model):
    """Per-user rotation seed (ADR-030). A single random `secret` mixed into every per-user derived
    credential (ops API key, ntfy paging topic, …). Rotating it rolls all of a user's links at once —
    per-user revocation without a key-per-credential table. Created at user creation (signal)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="keyring")
    secret = models.CharField(max_length=64, default=_gen_seed)
    rotated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def rotate(self):
        self.secret = _gen_seed()
        self.rotated_at = timezone.now()
        self.save(update_fields=["secret", "rotated_at"])

    def __str__(self):
        return f"keyring[{self.user_id}]"


class UserSession(models.Model):
    """Reverse index of a user's active login sessions (ADR-008). Sessions live only in Valkey (cache
    backend), which has no user→sessions lookup — so we record each authenticated session key here to
    enable 'sign out everywhere' + admin force sign-out. One row per login; rows are pruned on flush
    (a leftover row for an already-expired session is harmless — deleting it is a no-op)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="session_index")
    session_key = models.CharField(max_length=40, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"session[{self.user_id}] {self.session_key[:8]}…"
