import hmac
import uuid

from django.conf import settings
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import assessments, automation, operations_api
from .assessment_forms import SignalForm
from .api_common import NumberLookupMixin, filtered
from .models import Assessment
from .permissions import user_tier_rank


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
        secret = request.headers.get("X-Watch-Webhook-Secret", "")
        if not settings.INTAKE_WEBHOOK_SECRET or not hmac.compare_digest(secret, settings.INTAKE_WEBHOOK_SECRET):
            return Response({"detail": "Invalid webhook secret."}, status=status.HTTP_401_UNAUTHORIZED)
        serializer = SignalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assessment, created = assessments.receive(**serializer.validated_data)
        if created:
            automation.schedule(assessment)
        return Response({**AssessmentSerializer(assessment).data, "created": created},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class SignalIntakeView(AssessmentIntakeView):
    def post(self, request):
        secret = request.headers.get("X-Watch-Webhook-Secret", "")
        if not settings.INTAKE_WEBHOOK_SECRET or not hmac.compare_digest(secret, settings.INTAKE_WEBHOOK_SECRET):
            return Response({"detail": "Invalid webhook secret."}, status=status.HTTP_401_UNAUTHORIZED)
        serializer = SignalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rows = automation.receive_signal(**serializer.validated_data)
        return Response({"assessments": [{**AssessmentSerializer(row).data, "created": created} for row, created in rows]},
                        status=201 if any(created for _, created in rows) else 200)
