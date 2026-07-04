from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .health import HealthView, StatusView
from .views import (
    IncidentViewSet,
    IntakeWebhookView,
    SessionCheckWebhookView,
    WebhookEchoView,
)

router = DefaultRouter()
router.register("incidents", IncidentViewSet, basename="incident")

urlpatterns = [
    path("", include(router.urls)),
    path("intake/webhook", IntakeWebhookView.as_view(), name="intake-webhook"),
    path("checks/webhook", SessionCheckWebhookView.as_view(), name="checks-webhook"),
    path("webhook-echo", WebhookEchoView.as_view(), name="webhook-echo"),
    path("health", HealthView.as_view(), name="health"),
    path("status", StatusView.as_view(), name="status"),
]
