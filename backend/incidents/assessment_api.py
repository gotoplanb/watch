import hmac

from django.conf import settings
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import assessments, automation
from .models import Assessment


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


class AssessmentViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Assessment.objects.select_related("incident", "assignee")
    serializer_class = AssessmentSerializer

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        assessment = self.get_object()
        return Response(list(assessment.history.values(
            "id", "kind", "actor", "body", "data", "occurred_at", "recorded_at"
        )))


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
