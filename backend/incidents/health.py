"""
Liveness + public status endpoints (ADR-005 / §4.4 / ADR-011).

`/api/health` — dependency-checked liveness (Postgres + Valkey) for honest degradation.
`/api/status` — public, read-only posture (health + open-incident counts by tier) for
the React status-page SPA. CORS-open since it's served from a different origin
(S3/CloudFront in prod, a static server locally) and exposes only aggregate counts.
"""
import json
import time
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.models import Count
from django.http import StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
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


def status_posture() -> dict:
    """The public posture: health checks + open-incident counts by tier + resolved-24h. Reused by
    the /api/status snapshot and the /api/status/stream SSE feed (ADR-024)."""
    checks = dependency_checks()
    by_tier = {tier.value: 0 for tier in Tier}
    rows = (
        Incident.objects.filter(status=Status.OPEN)
        .values("current_tier")
        .annotate(n=Count("id"))
    )
    for row in rows:
        by_tier[row["current_tier"]] = row["n"]
    since = timezone.now() - timedelta(hours=24)
    resolved_24h = Incident.objects.filter(status=Status.RESOLVED, updated_at__gte=since).count()
    return {
        "status": "ok" if all(checks.values()) else "degraded",
        "checks": checks,
        "incidents": {
            "open": sum(by_tier.values()),
            "by_tier": by_tier,
            "resolved_24h": resolved_24h,
        },
        "generated_at": timezone.now().isoformat(),
    }


class StatusView(APIView):
    """Public read-only posture snapshot for the status-page SPA (aggregate counts only)."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request):
        data = status_posture()
        code = status.HTTP_200_OK if data["status"] == "ok" else status.HTTP_503_SERVICE_UNAVAILABLE
        resp = Response(data, status=code)
        # Configurable per environment (default open locally; a specific origin in prod).
        resp["Access-Control-Allow-Origin"] = settings.STATUS_PAGE_CORS_ORIGIN
        return resp


def status_stream(iterations: int, poll: float):
    """SSE generator (ADR-024): emit the current posture, then a posture-or-keepalive each poll
    cycle. Bounded by `iterations` so the connection recycles (the EventSource auto-reconnects) and
    tests terminate. Change-detection ignores the per-tick `generated_at`."""
    last = None
    for i in range(iterations):
        posture = status_posture()
        key = json.dumps({k: v for k, v in posture.items() if k != "generated_at"}, sort_keys=True)
        if key != last:
            yield f"event: status\ndata: {json.dumps(posture)}\n\n"
            last = key
        else:
            yield ": keepalive\n\n"
        if i < iterations - 1:
            time.sleep(poll)


@require_GET
def status_stream_view(request):
    """SSE feed of the status posture (ADR-024) — replaces the SPA's polling with one long-lived
    connection to the API origin. HTTP/streaming; point EventSource here (not CloudFront)."""
    poll = settings.STATUS_STREAM_POLL_SECONDS
    iterations = max(1, settings.STATUS_STREAM_MAX_SECONDS // poll) if poll else 1
    resp = StreamingHttpResponse(status_stream(iterations, poll), content_type="text/event-stream")
    resp["Access-Control-Allow-Origin"] = settings.STATUS_PAGE_CORS_ORIGIN
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"  # disable proxy/CloudFront buffering so events flush immediately
    return resp
