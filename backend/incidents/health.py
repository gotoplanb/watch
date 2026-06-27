"""
Liveness + public status endpoints (ADR-005 / §4.4 / ADR-011).

`/api/health` — dependency-checked liveness (Postgres + Valkey) for honest degradation.
`/api/status` — public, read-only posture (health + open-incident counts by tier) for
the React status-page SPA. CORS-open since it's served from a different origin
(S3/CloudFront in prod, a static server locally) and exposes only aggregate counts.
"""
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.models import Count
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Incident, Status, Tier


def dependency_checks() -> dict:
    """Probe the real dependencies (Postgres + Valkey) so degradation is honest."""
    checks = {}
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        checks["postgres"] = True
    except Exception:
        checks["postgres"] = False
    try:
        cache.set("health:probe", "1", timeout=5)
        checks["valkey"] = cache.get("health:probe") == "1"
    except Exception:
        checks["valkey"] = False
    return checks


class HealthView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request):
        checks = dependency_checks()
        ok = all(checks.values())
        return Response(
            {"status": "ok" if ok else "degraded", "checks": checks},
            status=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class StatusView(APIView):
    """Public read-only posture for the status-page SPA (aggregate counts only)."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request):
        checks = dependency_checks()
        ok = all(checks.values())

        by_tier = {tier.value: 0 for tier in Tier}
        rows = (
            Incident.objects.filter(status=Status.OPEN)
            .values("current_tier")
            .annotate(n=Count("id"))
        )
        for row in rows:
            by_tier[row["current_tier"]] = row["n"]

        since = timezone.now() - timedelta(hours=24)
        resolved_24h = Incident.objects.filter(
            status=Status.RESOLVED, updated_at__gte=since
        ).count()

        data = {
            "status": "ok" if ok else "degraded",
            "checks": checks,
            "incidents": {
                "open": sum(by_tier.values()),
                "by_tier": by_tier,
                "resolved_24h": resolved_24h,
            },
            "generated_at": timezone.now().isoformat(),
        }
        resp = Response(data, status=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE)
        # Configurable per environment (default open locally; a specific origin in prod).
        resp["Access-Control-Allow-Origin"] = settings.STATUS_PAGE_CORS_ORIGIN
        return resp
