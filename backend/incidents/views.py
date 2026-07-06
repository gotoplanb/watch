"""
API (spec §4.3).

Read endpoints for incidents; state changes go through the ack/escalate/resolve
action endpoints, each guarded by tier-or-above authz (ADR-008) and an optimistic
expected-tier check (ADR-007).

Escalate/Resolve flow:
  1. authorize + optimistic expected-tier check
  2. tell the engine: escalation.send_outcome(ESCALATE|RESOLVE) — consumes the token
  3. apply the decision + write the audit record via the idempotent services layer

In production the tier-entering Lambda is what writes the transition after the
token is consumed; here the app also applies it so the local loop works without the
Lambda. Because services.* are idempotent ("act if still applicable", ADR-001),
both calling it is safe — the second is a no-op.
"""
import hashlib
import hmac
import re

from django.conf import settings
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from . import checks, escalation, services
from .intake import create_incident_idempotent
from .models import CheckSource, CheckSubjectKind, Digest, EnvStatus, Incident, Status
from .permissions import CanActOnIncident
from .serializers import (
    ActionSerializer,
    DigestIngestSerializer,
    DigestReadSerializer,
    EnvStatusReadSerializer,
    IncidentSerializer,
    IntakeSerializer,
    PublicCheckSerializer,
    PublicIncidentSerializer,
    SessionCheckSerializer,
)

_ENV_RE = re.compile(r"^[a-z0-9-]+$")  # ADR-028 environment label


class IncidentViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Incident.objects.all().order_by("-created_at")
    serializer_class = IncidentSerializer

    def _validated(self, request):
        s = ActionSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return s.validated_data

    def _check_expected_tier(self, incident, data):
        """Optimistic concurrency (ADR-007): reject if the incident moved tier."""
        expected = data.get("expected_tier")
        if expected and expected != incident.current_tier:
            return Response(
                {"detail": f"Incident is at {incident.current_tier}, not {expected}."},
                status=status.HTTP_409_CONFLICT,
            )
        return None

    @action(detail=True, methods=["post"], permission_classes=[CanActOnIncident])
    def ack(self, request, pk=None):
        incident = self.get_object()  # runs object-level authz
        data = self._validated(request)
        if (conflict := self._check_expected_tier(incident, data)) is not None:
            return conflict
        # ACK is Postgres-only and never consumes the token (ADR-007).
        incident = services.acknowledge(incident.id, actor=str(request.user.pk),
                                        reason=data["reason"])
        return Response(IncidentSerializer(incident).data)

    @action(detail=True, methods=["post"], permission_classes=[CanActOnIncident])
    def escalate(self, request, pk=None):
        incident = self.get_object()
        data = self._validated(request)
        if (conflict := self._check_expected_tier(incident, data)) is not None:
            return conflict
        escalation.send_outcome(incident, escalation.OUTCOME_ESCALATE, actor=str(request.user.pk))
        if settings.ESCALATION_LOCAL_MODE:
            incident = services.escalate(incident.id, actor=str(request.user.pk),
                                         reason=data["reason"])
        return Response(IncidentSerializer(incident).data)

    @action(detail=True, methods=["post"], permission_classes=[CanActOnIncident])
    def resolve(self, request, pk=None):
        incident = self.get_object()
        data = self._validated(request)
        if (conflict := self._check_expected_tier(incident, data)) is not None:
            return conflict
        escalation.send_outcome(incident, escalation.OUTCOME_RESOLVE, actor=str(request.user.pk))
        if settings.ESCALATION_LOCAL_MODE:
            incident = services.resolve(incident.id, actor=str(request.user.pk),
                                        reason=data["reason"])
        return Response(IncidentSerializer(incident).data)


class IntakeWebhookView(APIView):
    """
    Intake (ADR-002 / ADR-009). Machine-to-machine; authenticated by a shared secret
    header, NOT a user session (ADR-008). In production this path is fronted by API
    Gateway + SQS; this endpoint is the consumer's create logic, also used by the
    local `consume_intake` command.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        secret = request.headers.get("X-Watch-Webhook-Secret", "")
        if not settings.INTAKE_WEBHOOK_SECRET or not hmac.compare_digest(
            secret, settings.INTAKE_WEBHOOK_SECRET
        ):
            return Response({"detail": "Invalid webhook secret."},
                            status=status.HTTP_401_UNAUTHORIZED)

        s = IntakeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        incident, created = create_incident_idempotent(
            source=d["source"],
            payload=d["payload"],
            title=d["title"],
            source_event_id=d["source_event_id"] or None,
        )
        if created:
            # One Standard execution per incident, started at creation (ADR-001/007).
            incident.escalation_execution_arn = escalation.start_escalation(incident)
            incident.save(update_fields=["escalation_execution_arn", "updated_at"])

        return Response(
            {"id": str(incident.id), "created": created, "status": incident.status},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SessionCheckWebhookView(APIView):
    """Inbound Session Check (ADR-022). M2M shared-secret auth (like intake, ADR-008), separate from
    human sessions. Creates a SessionCheck and, in local mode, runs it synchronously; the cloud path
    enqueues to SQS and a worker calls the same `checks` service."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        secret = request.headers.get("X-Watch-Webhook-Secret", "")
        if not settings.CHECKS_WEBHOOK_SECRET or not hmac.compare_digest(
            secret, settings.CHECKS_WEBHOOK_SECRET
        ):
            return Response({"detail": "Invalid webhook secret."},
                            status=status.HTTP_401_UNAUTHORIZED)

        s = SessionCheckSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        check = checks.create_and_run(
            subject_kind=d["subject_kind"],
            subject_raw=d["subject"],
            window_from=d["window_from"],
            window_to=d["window_to"],
            source=d["source"],
        )
        return Response(
            {
                "id": str(check.id),
                "status": check.status,
                "verdict": check.verdict,
                "error_spans": check.error_spans.count(),
            },
            status=status.HTTP_201_CREATED,
        )


class _PublicReportThrottle(SimpleRateThrottle):
    """Per-IP limiter for the anonymous report endpoints (ADR-027). Fixed scope (not
    ScopedRateThrottle, which pulls its scope off the view's `throttle_scope`); rate from
    DEFAULT_THROTTLE_RATES['public_report']. Preflights return no key, so they aren't counted."""

    scope = "public_report"

    def get_cache_key(self, request, view):
        if request.method == "OPTIONS":
            return None  # don't spend the budget on CORS preflights
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class _PublicCORSMixin:
    """Minimal CORS for the anonymous status-page write endpoints — mirrors the GET status view's
    single-origin allowance (ADR-011 / ADR-027) and answers the JSON-POST preflight. No credentials;
    DRF's built-in `options()` handles the preflight and this stamps the headers onto it."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        origin = settings.STATUS_PAGE_CORS_ORIGIN
        response["Access-Control-Allow-Origin"] = origin
        response["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Content-Type"
        if origin != "*":
            response["Vary"] = "Origin"
        return response


class ReportIncidentView(_PublicCORSMixin, APIView):
    """Public, unauthenticated incident report from the status-page form (ADR-027). Unlike the M2M
    intake webhook (ADR-008), there is NO shared secret — the abuse surface is bounded by a per-IP
    throttle and the serializer's length caps. Creates a real incident (source=status-page) and,
    like intake, starts one escalation execution."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [_PublicReportThrottle]

    def post(self, request):
        s = PublicIncidentSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        incident, created = create_incident_idempotent(
            source="status-page",
            payload={"detail": d["detail"], "reporter": "public"},
            title=d["title"],
            source_event_id=None,
        )
        if created:
            incident.escalation_execution_arn = escalation.start_escalation(incident)
            incident.save(update_fields=["escalation_execution_arn", "updated_at"])
        return Response(
            {"id": str(incident.id), "created": created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ReportCheckView(_PublicCORSMixin, APIView):
    """Public Session Check submission from the status-page form (ADR-027). A visitor reports their
    own non-secret session correlation id; we enqueue a check (source=self_report). The verdict is
    for the on-call, not the anonymous submitter, so we return only an id/status acknowledgement."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [_PublicReportThrottle]

    def post(self, request):
        s = PublicCheckSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        check = checks.create_and_run(
            subject_kind=CheckSubjectKind.SESSION,
            subject_raw=s.validated_data["session"].lower(),
            source=CheckSource.SELF_REPORT,
        )
        return Response(
            {"id": str(check.id), "status": check.status},
            status=status.HTTP_201_CREATED,
        )


class WebhookEchoView(APIView):
    """Loopback receiver for Watch's OWN outbound webhooks (ADR-023) — a self-contained, signature-
    verifying target so the outbound path is testable locally + on staging (the E2E dogfood). Verifies
    X-Watch-Signature against WEBHOOK_ECHO_SECRET; 200 if valid, 401 if not. Not a partner endpoint."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        secret = settings.WEBHOOK_ECHO_SECRET
        signature = request.headers.get("X-Watch-Signature", "")
        expected = (
            "sha256=" + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
            if secret else ""
        )
        if not secret or not hmac.compare_digest(signature, expected):
            return Response({"detail": "bad signature"}, status=status.HTTP_401_UNAUTHORIZED)
        return Response({"received": request.headers.get("X-Watch-Event", "")})


# --- Per-environment ops status + digests (ADR-028) -------------------------------------------------
def _ops_secret_ok(request) -> bool:
    """M2M auth for the ops ingest (mirrors the intake webhook, ADR-008) — a shared secret in
    X-Watch-Ops-Secret, never a human session."""
    secret = request.headers.get("X-Watch-Ops-Secret", "")
    return bool(settings.OPS_INGEST_SECRET) and hmac.compare_digest(secret, settings.OPS_INGEST_SECRET)


class _OpsIngestView(APIView):
    """Base for the secret-gated ops ingest POSTs. env comes from the URL; the body is per-subclass."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def _guard(self, request, env):
        if not _ops_secret_ok(request):
            return Response({"detail": "Invalid ops secret."}, status=status.HTTP_401_UNAUTHORIZED)
        if not _ENV_RE.match(env or ""):
            return Response({"detail": "Invalid environment label."}, status=status.HTTP_400_BAD_REQUEST)
        return None


class EnvStatusIngestView(_OpsIngestView):
    """Ops status ingest (ADR-028). The request body is arbitrary JSON, stored VERBATIM as the status
    payload — no schema; the posted JSON itself defines the display groupings."""

    def post(self, request, env):
        blocked = self._guard(request, env)
        if blocked:
            return blocked
        payload = request.data
        if not isinstance(payload, (dict, list)) or payload in ({}, []):
            return Response({"detail": "Body must be a non-empty JSON object or array."},
                            status=status.HTTP_400_BAD_REQUEST)
        s = EnvStatus.objects.create(environment=env, payload=payload)
        return Response({"id": str(s.id), "environment": env, "created_at": s.created_at},
                        status=status.HTTP_201_CREATED)


class DigestIngestView(_OpsIngestView):
    """Per-env digest ingest (ADR-028). `special` = the 'speci' ad-hoc incident digest flag."""

    def post(self, request, env):
        blocked = self._guard(request, env)
        if blocked:
            return blocked
        s = DigestIngestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = Digest.objects.create(environment=env, **s.validated_data)
        return Response({"id": str(d.id), "environment": env, "special": d.special, "created_at": d.created_at},
                        status=status.HTTP_201_CREATED)


class EnvStatusReadView(APIView):
    """Latest ops status for an env — session-auth (DRF default). The /ui renders from the ORM; this
    is the JSON read for API consumers / tooling verification."""

    def get(self, request, env):
        s = EnvStatus.objects.filter(environment=env).first()  # Meta ordering: newest first
        if not s:
            return Response({"detail": "No status for this environment."}, status=status.HTTP_404_NOT_FOUND)
        return Response(EnvStatusReadSerializer(s).data)


class DigestListView(APIView):
    """Per-env digest history — session-auth; optional ?special=true|false filter."""

    def get(self, request, env):
        qs = Digest.objects.filter(environment=env)
        special = request.query_params.get("special")
        if special in ("true", "1"):
            qs = qs.filter(special=True)
        elif special in ("false", "0"):
            qs = qs.filter(special=False)
        return Response(DigestReadSerializer(qs[:50], many=True).data)
