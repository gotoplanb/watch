from rest_framework import serializers

from .models import Digest, EnvStatus, Incident, Tier, Transition


class DigestIngestSerializer(serializers.Serializer):
    """Ingest body for a per-env health digest (ADR-028). `special` is the 'speci' flag — an ad-hoc
    incident digest vs a routine scheduled one. The status ingest has NO serializer: its body is
    arbitrary JSON stored verbatim."""

    content = serializers.CharField()
    title = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    special = serializers.BooleanField(required=False, default=False)


class EnvStatusReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = EnvStatus
        fields = ["id", "environment", "payload", "created_at"]


class DigestReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Digest
        fields = ["id", "environment", "title", "content", "special", "created_at"]


class TransitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transition
        fields = [
            "id", "from_status", "from_tier", "to_status", "to_tier",
            "actor", "reason", "at",
        ]


class IncidentSerializer(serializers.ModelSerializer):
    transitions = TransitionSerializer(many=True, read_only=True)

    class Meta:
        model = Incident
        fields = [
            "id", "source", "title", "status", "current_tier", "acknowledged_at",
            "assignee", "sla_deadline_at", "escalation_execution_arn",
            "created_at", "updated_at", "transitions",
        ]
        read_only_fields = fields  # state changes go through the action endpoints only


class ActionSerializer(serializers.Serializer):
    """Input for ack/escalate/resolve. `expected_tier` enforces optimistic
    concurrency (ADR-007): the client states which tier it believes it is acting on,
    and the server rejects if the incident has since moved."""

    expected_tier = serializers.ChoiceField(choices=Tier.choices, required=False)
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class IntakeSerializer(serializers.Serializer):
    source = serializers.CharField()
    title = serializers.CharField()
    source_event_id = serializers.CharField(required=False, allow_blank=True, default="")
    payload = serializers.JSONField(required=False, default=dict)


class PublicIncidentSerializer(serializers.Serializer):
    """Public, unauthenticated incident report from the status-page form (ADR-027). Tight bounds keep
    the anonymous write surface small; the body is stored as the incident payload, not trusted."""

    title = serializers.CharField(max_length=200, trim_whitespace=True)
    detail = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class PublicCheckSerializer(serializers.Serializer):
    """Public Session Check submission (ADR-027): a visitor reports their own session correlation id.
    The id is a non-secret uuid4 hex (32 chars) surfaced by the app; we validate the shape so the
    anonymous path can only enqueue well-formed checks."""

    session = serializers.RegexField(
        r"^[0-9a-fA-F]{32}$",
        error_messages={"invalid": "Enter the 32-character session id shown in the app."},
    )


class SessionCheckSerializer(serializers.Serializer):
    """Inbound Session Check request (ADR-022). `subject` is the non-secret session correlation id
    (kind=session) or the plaintext user/customer id (kind=user, HMAC'd server-side)."""

    subject_kind = serializers.ChoiceField(choices=[("session", "session"), ("user", "user")])
    subject = serializers.CharField()
    window_from = serializers.DateTimeField(required=False, allow_null=True, default=None)
    window_to = serializers.DateTimeField(required=False, allow_null=True, default=None)
    source = serializers.ChoiceField(
        choices=[("partner", "partner"), ("e2e", "e2e"), ("manual", "manual")],
        required=False,
        default="partner",
    )
