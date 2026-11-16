from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import operations_api
from .api_common import MeView
from .health import HealthView, StatusView, status_stream_view
from .assessment_api import (
    AlertmanagerView, AssessmentIntakeView, AssessmentViewSet, EmailIntakeView, HeartbeatListView,
    HeartbeatView, SignalIntakeView, StatuspageView, UptimeRobotView,
)
from .views import (
    DigestIngestView,
    DigestListView,
    EnvStatusIngestView,
    EnvStatusReadView,
    IncidentViewSet,
    IntakeWebhookView,
    ReportCheckView,
    ReportIncidentView,
    SessionCheckWebhookView,
    WebhookEchoView,
)

router = DefaultRouter()
router.register("incidents", IncidentViewSet, basename="incident")
router.register("assessments", AssessmentViewSet, basename="assessment")

urlpatterns = [
    path("", include(router.urls)),
    path("me", MeView.as_view(), name="me"),
    # The assessment workspace beyond the assessment itself (ADR-056).
    path("communications/<uuid:pk>/", operations_api.communication_detail, name="api-communication"),
    path("communications/<uuid:pk>/<str:name>/", operations_api.communication_action,
         name="api-communication-action"),
    path("engagements/<uuid:pk>/respond/", operations_api.engagement_respond,
         name="api-engagement-respond"),
    path("remediations/<uuid:pk>/", operations_api.remediation_update, name="api-remediation"),
    path("diagnostic-rules/", operations_api.diagnostic_rules, name="api-diagnostic-rules"),
    path("subject-areas/", operations_api.subject_areas, name="api-subject-areas"),
    path("outbox/", operations_api.outbox, name="api-outbox"),
    path("reports/", operations_api.reports, name="api-reports"),
    path("maintenance/", operations_api.maintenance_windows, name="api-maintenance"),
    path("intake/webhook", IntakeWebhookView.as_view(), name="intake-webhook"),
    path("assessments/intake", AssessmentIntakeView.as_view(), name="assessment-intake"),
    path("signals/intake", SignalIntakeView.as_view(), name="signal-intake"),
    path("email/inbound", EmailIntakeView.as_view(), name="email-inbound"),
    # Vendor adapters (ADR-065): payloads that can't be templated into the signal shape.
    path("signals/uptimerobot", UptimeRobotView.as_view(), name="signal-uptimerobot"),
    path("signals/alertmanager", AlertmanagerView.as_view(), name="signal-alertmanager"),
    path("signals/statuspage/<str:token>", StatuspageView.as_view(), name="signal-statuspage"),
    path("heartbeats/", HeartbeatListView.as_view(), name="heartbeats"),
    path("heartbeats/<str:token>", HeartbeatView.as_view(), name="heartbeat"),
    path("checks/webhook", SessionCheckWebhookView.as_view(), name="checks-webhook"),
    path("report/incident", ReportIncidentView.as_view(), name="report-incident"),
    path("report/check", ReportCheckView.as_view(), name="report-check"),
    # Per-environment ops status + digests (ADR-028): secret-gated ingest + session-auth reads.
    path("environments/<str:env>/status", EnvStatusIngestView.as_view(), name="env-status-ingest"),
    path("environments/<str:env>/status/latest", EnvStatusReadView.as_view(), name="env-status-latest"),
    path("environments/<str:env>/digest", DigestIngestView.as_view(), name="env-digest-ingest"),
    path("environments/<str:env>/digests", DigestListView.as_view(), name="env-digests"),
    path("webhook-echo", WebhookEchoView.as_view(), name="webhook-echo"),
    path("health", HealthView.as_view(), name="health"),
    path("status", StatusView.as_view(), name="status"),
    path("status/stream", status_stream_view, name="status-stream"),
]
