"""
API (spec §4.3).

Read endpoints for incidents; state changes go through the ack/escalate/resolve
action endpoints, each guarded by tier-or-above authz (ADR-008) and an optimistic
expected-tier check (ADR-007).

Escalate/Resolve flow:
  1. authorize + optimistic expected-tier check
  2. tell the engine: escalation.send_outcome(ESCALATE|RESOLVE) — consumes the token
  3. apply the decision + write the audit record via the idempotent services layer

In production the tier-entering Lambda is what writes the transition after the
token is consumed; here the app also applies it so the local loop works without the
Lambda. Because services.* are idempotent ("act if still applicable", ADR-001),
both calling it is safe — the second is a no-op.
"""
import hmac

from django.conf import settings
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import escalation, services
from .intake import create_incident_idempotent
from .models import Incident, Status
from .permissions import CanActOnIncident
from .serializers import ActionSerializer, IncidentSerializer, IntakeSerializer


class IncidentViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Incident.objects.all().order_by("-created_at")
    serializer_class = IncidentSerializer

    def _validated(self, request):
        s = ActionSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return s.validated_data

    def _check_expected_tier(self, incident, data):
        """Optimistic concurrency (ADR-007): reject if the incident moved tier."""
        expected = data.get("expected_tier")
        if expected and expected != incident.current_tier:
            return Response(
                {"detail": f"Incident is at {incident.current_tier}, not {expected}."},
                status=status.HTTP_409_CONFLICT,
            )
        return None

    @action(detail=True, methods=["post"], permission_classes=[CanActOnIncident])
    def ack(self, request, pk=None):
        incident = self.get_object()  # runs object-level authz
        data = self._validated(request)
        if (conflict := self._check_expected_tier(incident, data)) is not None:
            return conflict
        # ACK is Postgres-only and never consumes the token (ADR-007).
        incident = services.acknowledge(incident.id, actor=str(request.user.pk),
                                        reason=data["reason"])
        return Response(IncidentSerializer(incident).data)

    @action(detail=True, methods=["post"], permission_classes=[CanActOnIncident])
    def escalate(self, request, pk=None):
        incident = self.get_object()
        data = self._validated(request)
        if (conflict := self._check_expected_tier(incident, data)) is not None:
            return conflict
        escalation.send_outcome(incident, escalation.OUTCOME_ESCALATE)
        if settings.ESCALATION_LOCAL_MODE:
            incident = services.escalate(incident.id, actor=str(request.user.pk),
                                         reason=data["reason"])
        return Response(IncidentSerializer(incident).data)

    @action(detail=True, methods=["post"], permission_classes=[CanActOnIncident])
    def resolve(self, request, pk=None):
        incident = self.get_object()
        data = self._validated(request)
        if (conflict := self._check_expected_tier(incident, data)) is not None:
            return conflict
        escalation.send_outcome(incident, escalation.OUTCOME_RESOLVE)
        if settings.ESCALATION_LOCAL_MODE:
            incident = services.resolve(incident.id, actor=str(request.user.pk),
                                        reason=data["reason"])
        return Response(IncidentSerializer(incident).data)


class IntakeWebhookView(APIView):
    """
    Intake (ADR-002 / ADR-009). Machine-to-machine; authenticated by a shared secret
    header, NOT a user session (ADR-008). In production this path is fronted by API
    Gateway + SQS; this endpoint is the consumer's create logic, also used by the
    local `consume_intake` command.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        secret = request.headers.get("X-Watch-Webhook-Secret", "")
        if not settings.INTAKE_WEBHOOK_SECRET or not hmac.compare_digest(
            secret, settings.INTAKE_WEBHOOK_SECRET
        ):
            return Response({"detail": "Invalid webhook secret."},
                            status=status.HTTP_401_UNAUTHORIZED)

        s = IntakeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        incident, created = create_incident_idempotent(
            source=d["source"],
            payload=d["payload"],
            title=d["title"],
            source_event_id=d["source_event_id"] or None,
        )
        if created:
            # One Standard execution per incident, started at creation (ADR-001/007).
            incident.escalation_execution_arn = escalation.start_escalation(incident)
            incident.save(update_fields=["escalation_execution_arn", "updated_at"])

        return Response(
            {"id": str(incident.id), "created": created, "status": incident.status},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
