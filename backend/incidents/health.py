"""
Dependency-checked health endpoint (ADR-005 / §4.4).

Honest degradation requires the SPA to probe a real liveness signal, not just an
ALB 200. This checks Postgres AND Valkey reachability so the SPA's read-only/stale
banner reflects actual backend health.
"""
from django.core.cache import cache
from django.db import connection
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request):
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

        ok = all(checks.values())
        return Response(
            {"status": "ok" if ok else "degraded", "checks": checks},
            status=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )
