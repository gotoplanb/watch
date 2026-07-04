from rest_framework import serializers

from .models import Incident, Tier, Transition


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
