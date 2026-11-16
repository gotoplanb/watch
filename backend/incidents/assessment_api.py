import hmac
import uuid

from django.conf import settings
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ParseError
from rest_framework.parsers import BaseParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import assessments, automation, email_intake, operations_api
from .assessment_forms import SignalForm
from .api_common import NumberLookupMixin, filtered
from .models import Assessment
from .permissions import user_tier_rank


def _secret_denied(request):
    """The intake shared-secret check (ADR-008: machine-to-machine, not a user session)."""
    secret = request.headers.get("X-Watch-Webhook-Secret", "")
    if settings.INTAKE_WEBHOOK_SECRET and hmac.compare_digest(secret, settings.INTAKE_WEBHOOK_SECRET):
        return None
    return Response({"detail": "Invalid webhook secret."}, status=status.HTTP_401_UNAUTHORIZED)


class SignalSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=512)
    source = serializers.CharField(max_length=128)
    signal = serializers.CharField(max_length=10000)
    customer = serializers.CharField(max_length=128, required=False, default="", allow_blank=True)
    service = serializers.CharField(max_length=128, required=False, default="", allow_blank=True)
    supplier = serializers.CharField(max_length=128, required=False, default="", allow_blank=True)
    source_event_id = serializers.CharField(max_length=256, required=False, allow_blank=True)
    signal_at = serializers.DateTimeField(required=False)
    payload = serializers.JSONField(required=False, default=dict)
    # ADR-059: the condition's stable id at the source, and whether it is active or has recovered.
    correlation_key = serializers.CharField(max_length=256, required=False, default="", allow_blank=True)
    state = serializers.ChoiceField(choices=["active", "resolved"], required=False, default="active")

    def validate_payload(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Use a JSON object for the source payload.")
        return value


class AssessmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Assessment
        fields = ["id", "number", "title", "source", "signal", "customer", "service", "supplier",
                  "status", "verdict", "confidence", "conclusion", "signal_at", "created_at",
                  "closed_at", "promoted_at", "incident", "assignee", "severity", "impact", "automation_state", "automation_policy"]
        read_only_fields = fields


class AssessmentViewSet(NumberLookupMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Assessment.objects.select_related("incident", "assignee").order_by("-created_at")
    serializer_class = AssessmentSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action == "list":
            qs = filtered(qs, self.request.query_params, ("status",))
        return qs

    def create(self, request):
        """A responder opens an assessment by hand, as /ui/assessments/new/ does (owned by them)."""
        if user_tier_rank(request.user) < 0:
            return Response({"detail": "A responder role is required to create assessments."},
                            status=status.HTTP_403_FORBIDDEN)
        data = {"source": "operator", "source_event_id": str(uuid.uuid4()), **request.data}
        form = SignalForm(data)
        if not form.is_valid():
            return Response({"detail": "Check the fields.", "errors": form.errors.get_json_data()},
                            status=status.HTTP_400_BAD_REQUEST)
        fields = {**form.cleaned_data, "source_event_id": str(form.cleaned_data["source_event_id"])}
        assessment, created = assessments.receive(**fields, actor=request.user.username,
                                                  assignee=request.user)
        if created:
            automation.schedule(assessment)
        return Response({**AssessmentSerializer(assessment).data, "created": created},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        return Response(operations_api.events_json(self.get_object().history.order_by("pk")))

    @action(detail=True, methods=["post"], url_path=r"actions/(?P<name>[a-z_]+)")
    def actions(self, request, pk=None, name=None):
        """Any workspace action: comment, evidence, conclusion, monitor, no_issue, promote, email,
        operation, attachment, query, engage, curate, rca, remediation, context, claim, retry_*."""
        return operations_api.run_action(request, self.get_object(), name)

    @action(detail=True, methods=["get"])
    def communications(self, request, pk=None):
        rows = self.get_object().communications.prefetch_related("revisions").order_by("created_at")
        return Response([operations_api.communication_json(c) for c in rows])

    @action(detail=True, methods=["get"])
    def engagements(self, request, pk=None):
        rows = self.get_object().engagements.select_related(
            "target", "area", "requested_by", "acknowledged_by").order_by("paged_at")
        return Response([operations_api.engagement_json(e) for e in rows])

    @action(detail=True, methods=["get"])
    def remediations(self, request, pk=None):
        rows = self.get_object().remediations.select_related("owner").order_by("due_date")
        return Response([operations_api.remediation_json(t) for t in rows])

    @action(detail=True, methods=["get"])
    def rca(self, request, pk=None):
        return Response(operations_api.rca_json(self.get_object()))


class AssessmentIntakeView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if (denied := _secret_denied(request)) is not None:
            return denied
        serializer = SignalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        data.pop("state")  # correlation is the routing path's job (signals/intake); this opens one
        assessment, created = assessments.receive(**data)
        if created:
            automation.schedule(assessment)
        return Response({**AssessmentSerializer(assessment).data, "created": created},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class SignalIntakeView(AssessmentIntakeView):
    def post(self, request):
        if (denied := _secret_denied(request)) is not None:
            return denied
        serializer = SignalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rows = automation.receive_signal(**serializer.validated_data)
        if any(created for _, created in rows):
            outcome = "created"
        elif rows:
            outcome = "correlated"
        else:
            outcome = "nothing open"  # a recovery for a condition no assessment is working
        return Response({"outcome": outcome,
                         "assessments": [{**AssessmentSerializer(row).data, "created": created} for row, created in rows]},
                        status=201 if outcome == "created" else 200)


class RawEmailParser(BaseParser):
    """`message/rfc822` bodies arrive as bytes. Read straight from the stream, so the email's own
    size cap applies instead of DATA_UPLOAD_MAX_MEMORY_SIZE (attachments can reach 8 × 5 MB)."""
    media_type = "message/rfc822"
    max_bytes = 45 * 1024 * 1024

    def parse(self, stream, media_type=None, parser_context=None):
        raw = stream.read(self.max_bytes + 1) if stream else b""
        if len(raw) > self.max_bytes:
            raise ParseError("Email is larger than 45 MB.")
        return raw


class EmailIntakeView(APIView):
    """Inbound email (ADR-058): the raw message as `message/rfc822`, behind the intake secret. The SES
    receipt Lambda posts here in the cloud; `wt email-in` does locally."""
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = [RawEmailParser]

    def post(self, request):
        if (denied := _secret_denied(request)) is not None:
            return denied
        if not isinstance(request.data, bytes) or not request.data.strip():
            return Response({"detail": "Send the raw message as message/rfc822."},
                            status=status.HTTP_400_BAD_REQUEST)
        result = email_intake.receive(request.data)
        created = any(c for _, c in result.assessments)
        return Response({
            "outcome": result.outcome, "reason": result.reason,
            "assessments": [{**AssessmentSerializer(row).data, "created": c} for row, c in result.assessments],
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
