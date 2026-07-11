"""
Check→incident bridge (T0) + T1 triage (ADR-036).

The bridge is deterministic — no AI: a check that finished with `errors_found` opens an
incident through the existing idempotent intake (ADR-009), flag-gated. The dedupe key derives
from the check SUBJECT (`check:{subject_kind}:{subject_hash}`), so a flapping subject re-firing
while its incident is OPEN is an ON-CONFLICT no-op, not an incident flood; a re-fire after
RESOLVED opens a fresh incident (existing partial-unique semantics, unchanged).

Triage then classifies the new incident — deterministic first (ADR-037): the routing matrix
(`triage-matrix.yaml` via routing_matrix) classifies span evidence by status code and origin;
the assistant (triage_ai, ADR-034-style seam) is the FALLBACK for evidence no rule matches,
returning ONLY (responsibility, fault_domain, verdict, confidence, rationale). Disposition is
always the matrix's (verdict × cell × mode) table: FALSE_POSITIVE auto-resolves in highway
mode; internal cells auto-escalate T1→T2 in race mode (escaped-defect / deploy-induced-fault
presumption); everything else is advisory and rides the SLA/escalation engine unchanged.

Failure posture: any assistant failure soft-fails — the incident stays open and untriaged, a
system event records why, and the deterministic engine remains the backstop (never the AI).
"""
import logging

from django.conf import settings

from . import escalation, events, flags, intake, routing_matrix, services, triage_ai
from .models import (
    LinkKind,
    SessionCheck,
    TriageDecision,
    TriageDisposition,
    TriageVerdict,
)

logger = logging.getLogger(__name__)

BRIDGE_FLAG = "check_incident_bridge"
BRIDGE_ACTOR = "system:check-bridge"


def check_dedupe_key(check: SessionCheck) -> str:
    """ADR-036 compatibility contract — changing this format re-floods flapping subjects."""
    return f"check:{check.subject_kind}:{check.subject_hash}"


def bridge_check(check: SessionCheck):
    """T0: turn a completed `errors_found` check into an (idempotent) incident, provenance-linked,
    then hand it to T1 triage. Returns the incident or None (flag off / nothing to bridge).
    An `indeterminate` check never bridges — never an incident from a non-answer (ADR-022/036)."""
    if not check.verdict.startswith("errors_found"):
        return None
    if not flags.is_enabled(BRIDGE_FLAG, default=False):
        return None

    incident, created = intake.create_incident_idempotent(
        source="check",
        payload={
            "check_id": str(check.id),
            "subject_kind": check.subject_kind,
            "subject_hash": check.subject_hash,
            "verdict": check.verdict,
        },
        title=f"Errors found for {check.subject_kind} {check.subject_hash[:12]}",
        dedupe_key=check_dedupe_key(check),
    )
    if created:
        # One Standard execution per incident, started at creation (ADR-001/007) — same as intake.
        incident.escalation_execution_arn = escalation.start_escalation(incident)
        incident.save(update_fields=["escalation_execution_arn", "updated_at"])
        services.link_records(incident, check, kind=LinkKind.CREATED_FROM, actor=BRIDGE_ACTOR)
        run_triage(incident, check)
    else:
        # A subsequent failing run of the same subject: corroborating evidence, not a new incident.
        services.link_records(check, incident, kind=LinkKind.RELATES_TO, actor=BRIDGE_ACTOR)
    return incident


def _evidence(incident, check: SessionCheck, spans) -> tuple[str, dict]:
    """Render what the assistant may consult — and snapshot it for the audit row."""
    lines = [
        f"# Triage evidence for {incident.number}",
        f"Incident: {incident.title} (source: {incident.source}, tier: {incident.current_tier})",
        f"Check: {check.subject_kind}:{check.subject_hash} — verdict {check.verdict}, "
        f"window {check.window_from} → {check.window_to}",
        "",
        "## Error spans",
    ]
    if spans:
        for s in spans:
            http = f"HTTP {s.http_status}" if s.http_status else "no HTTP status"
            lines.append(
                f"- {s.name or s.span_id} (service: {s.service or 'unknown'}, {s.status}, "
                f"{http}, trace {s.trace_id}, at {s.ts})"
            )
    else:
        lines.append("- (none recorded)")
    snapshot = {
        "check_id": str(check.id),
        "check_verdict": check.verdict,
        "spans": [
            {
                "trace_id": s.trace_id, "span_id": s.span_id, "name": s.name,
                "service": s.service, "status": s.status, "http_status": s.http_status,
                "kind": s.kind,
            }
            for s in spans
        ],
    }
    return "\n".join(lines), snapshot


def run_triage(incident, check: SessionCheck) -> TriageDecision | None:
    """T1 triage pass (ADR-037): the routing matrix classifies deterministically; the AI
    assistant is the fallback for unmatched evidence. Records the append-only TriageDecision,
    denormalizes onto the incident, then applies the matrix disposition. Soft-fails on any
    assistant error."""
    from . import modes

    spans = list(check.error_spans.all())
    evidence_markdown, snapshot = _evidence(incident, check, spans)
    cell = routing_matrix.classify(spans)
    if cell:
        responsibility, fault_domain = cell
        result = triage_ai.TriageResult(
            responsibility=responsibility,
            fault_domain=fault_domain,
            verdict=TriageVerdict.REAL,  # a matrix match IS a real error, even someone else's
            confidence=1.0,
            rationale=f"Deterministic matrix classification ({responsibility}/{fault_domain}) "
                      f"from span status/origin evidence.",
            provider="matrix",
            model="triage-matrix.yaml",
        )
    else:
        try:
            result = triage_ai.classify(evidence_markdown)
        except triage_ai.TriageError as exc:
            logger.warning("triage failed incident=%s: %s", incident.id, exc)
            services.post_system_event(
                incident,
                body=f"T1 triage failed ({exc}) — incident stays open; SLA engine is the backstop.",
                data={"error": str(exc)},
            )
            return None

    mode = modes.current_mode()
    disposition = routing_matrix.dispose(
        result.responsibility, result.fault_domain, result.verdict, mode
    )
    decision = TriageDecision.objects.create(
        incident=incident,
        actor=TriageDecision.ASSISTANT_ACTOR,
        responsibility=result.responsibility,
        fault_domain=result.fault_domain,
        verdict=result.verdict,
        confidence=result.confidence,
        rationale=result.rationale,
        evidence=snapshot,
        disposition=disposition,
        mode=mode,
        provider=result.provider,
        model=result.model,
    )
    incident.triage_responsibility = result.responsibility
    incident.triage_fault_domain = result.fault_domain
    incident.triage_verdict = result.verdict
    incident.save(
        update_fields=[
            "triage_responsibility", "triage_fault_domain", "triage_verdict", "updated_at"
        ]
    )
    services.post_ai_event(
        incident,
        body=(
            f"T1 triage: {result.verdict} ({result.responsibility}/{result.fault_domain}, "
            f"confidence {result.confidence:.2f}) via {result.provider} ({result.model}) — "
            f"{disposition} [{mode} mode]. {result.rationale}"
        ),
        actor=TriageDecision.ASSISTANT_ACTOR,
        data={"decision_id": str(decision.id), "disposition": disposition, "mode": mode},
    )
    events.emit("incident.triaged", {
        "incident_id": str(incident.id), "verdict": result.verdict,
        "responsibility": result.responsibility, "fault_domain": result.fault_domain,
        "disposition": str(disposition), "mode": str(mode),
    })

    # Both actions ride the normal engine paths (ADR-007): the token is consumed via
    # send_outcome so no zombie timer survives. At bridge time the tier token may not be
    # recorded yet in the cloud path — send_outcome no-ops on an empty token and the verdict
    # stays advisory there; the local path applies the service call directly.
    if disposition == TriageDisposition.AUTO_RESOLVE:
        escalation.send_outcome(
            incident, escalation.OUTCOME_RESOLVE, actor=TriageDecision.ASSISTANT_ACTOR
        )
        if settings.ESCALATION_LOCAL_MODE:
            services.resolve(
                incident.id,
                actor=TriageDecision.ASSISTANT_ACTOR,
                reason="false_positive (T1 triage)",
            )
    elif disposition == TriageDisposition.AUTO_ESCALATE:
        escalation.send_outcome(
            incident, escalation.OUTCOME_ESCALATE, actor=TriageDecision.ASSISTANT_ACTOR
        )
        if settings.ESCALATION_LOCAL_MODE:
            services.escalate(
                incident.id,
                actor=TriageDecision.ASSISTANT_ACTOR,
                reason="race-mode policy: internal fault during release window (ADR-037)",
            )
    return decision
