from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .health import HealthView, StatusView, status_stream_view
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

urlpatterns = [
    path("", include(router.urls)),
    path("intake/webhook", IntakeWebhookView.as_view(), name="intake-webhook"),
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
