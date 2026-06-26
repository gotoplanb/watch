from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .health import HealthView
from .views import IncidentViewSet, IntakeWebhookView

router = DefaultRouter()
router.register("incidents", IncidentViewSet, basename="incident")

urlpatterns = [
    path("", include(router.urls)),
    path("intake/webhook", IntakeWebhookView.as_view(), name="intake-webhook"),
    path("health", HealthView.as_view(), name="health"),
]
