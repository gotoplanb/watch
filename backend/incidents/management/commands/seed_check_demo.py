"""
Synthetic-data fuel for the check→incident→triage loop (ADR-036 §6 / ADR-037, manual use):

    python manage.py seed_check_demo                   # our 500s → matrix internal/software
                                                       #   (highway: stays open; race: auto-escalates T2)
    python manage.py seed_check_demo --client-noise    # 404s → matrix client/software, advisory
    python manage.py seed_check_demo --vendor          # outbound client-kind 502s → vendor/environment
    python manage.py seed_check_demo --false-positive  # 418s match no rule → AI fallback → stub
                                                       #   says false positive → auto-resolved (highway)

Emits OTLP error spans (tagged `session.id`, the attribute session_tagging stamps) straight to
the local Alloy/collector as a hand-built OTLP-JSON POST — no SDK wiring needed — then creates
and runs a real SessionCheck for that session, which exercises the whole slice against real
Tempo queries: check → T0 bridge → matrix/AI triage → dispose. Retries the check a few times to
ride out Tempo ingest lag. Local/dev only — needs TRACE_STORE_PROVIDER=tempo and Watchtower up.
"""
import secrets
import time
import uuid

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from incidents import checks
from incidents.models import CheckSubjectKind

OTLP_URL = "http://localhost:4318/v1/traces"


def _span(session_id: str, name: str, http_status: int, now_ns: int, kind: int = 2) -> dict:
    return {
        "traceId": secrets.token_hex(16),
        "spanId": secrets.token_hex(8),
        "name": name,
        "kind": kind,  # OTLP span kind: 2 = SERVER (ours), 3 = CLIENT (outbound → vendor, ADR-037)
        "startTimeUnixNano": str(now_ns - 50_000_000),
        "endTimeUnixNano": str(now_ns),
        "attributes": [
            {"key": "session.id", "value": {"stringValue": session_id}},
            {"key": "http.status_code", "value": {"intValue": str(http_status)}},
        ],
        "status": {"code": 2, "message": f"HTTP {http_status}"},  # OTLP status code ERROR
    }


class Command(BaseCommand):
    help = "Emit synthetic error spans and run a SessionCheck to drive the ADR-036 triage loop."

    def add_arguments(self, parser):
        parser.add_argument("--false-positive", action="store_true",
                            help="Emit 418s — no matrix rule matches, so the AI fallback runs; "
                                 "the stub reads no-5xx as a false positive (auto-resolves in "
                                 "highway mode).")
        parser.add_argument("--client-noise", action="store_true",
                            help="Emit 404s — matrix classifies client/software, advisory only.")
        parser.add_argument("--vendor", action="store_true",
                            help="Emit outbound (client-kind) 502s — matrix classifies "
                                 "vendor/environment.")
        parser.add_argument("--session", default=None, help="Session correlation id to tag/check.")
        parser.add_argument("--otlp-url", default=OTLP_URL)
        parser.add_argument("--retries", type=int, default=6,
                            help="Check attempts while waiting for Tempo ingest (2s apart).")

    def handle(self, *args, **opts):
        session_id = opts["session"] or uuid.uuid4().hex
        kind = 2
        if opts["false_positive"]:
            http_status = 418  # matches no matrix rule → exercises the AI fallback
        elif opts["client_noise"]:
            http_status = 404
        elif opts["vendor"]:
            http_status, kind = 502, 3  # outbound CLIENT span → third-party origin
        else:
            http_status = 500  # our unhandled exception → internal/software
        now_ns = time.time_ns()
        payload = {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": "watch-demo"}},
                ]},
                "scopeSpans": [{
                    "scope": {"name": "watch.seed_check_demo"},
                    "spans": [
                        _span(session_id, "GET /demo/orders", http_status, now_ns, kind),
                        _span(session_id, "GET /demo/cart", http_status, now_ns, kind),
                    ],
                }],
            }]
        }
        try:
            resp = requests.post(opts["otlp_url"], json=payload, timeout=5)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f"OTLP emit failed ({exc}) — is Watchtower/Alloy up on 4318?") from exc
        self.stdout.write(f"emitted 2 error spans (HTTP {http_status}) for session {session_id}")

        if settings.TRACE_STORE_PROVIDER != "tempo":
            raise CommandError("TRACE_STORE_PROVIDER must be 'tempo' for the live loop "
                               "(TRACE_STORE_PROVIDER=tempo TEMPO_QUERY_URL=http://localhost:3200)")

        check = None
        for attempt in range(1, opts["retries"] + 1):
            check = checks.create_and_run(
                subject_kind=CheckSubjectKind.SESSION, subject_raw=session_id, source="manual"
            )
            self.stdout.write(f"  check attempt {attempt}: {check.status} / {check.verdict}")
            if check.verdict.startswith("errors_found"):
                break
            time.sleep(2)  # Tempo ingest lag

        if not check or not check.verdict.startswith("errors_found"):
            raise CommandError("no error spans surfaced — Tempo may still be ingesting; re-run.")

        from incidents.models import Incident
        incident = Incident.objects.filter(dedupe_key=f"check:session:{session_id}").order_by(
            "-created_at").first()
        if incident is None:
            self.stdout.write(self.style.WARNING(
                "check found errors but no incident — is the check_incident_bridge flag on?"))
            return
        decision = incident.triage_decisions.first()
        if decision:
            summary = (f"{incident.number}: {incident.status}/{incident.current_tier} — triage "
                       f"{decision.verdict} ({decision.responsibility}/{decision.fault_domain}) → "
                       f"{decision.disposition} [{decision.mode}]")
        else:
            summary = f"{incident.number}: {incident.status} — no triage decision recorded"
        self.stdout.write(self.style.SUCCESS(summary))
