"""Check→incident bridge + T1 triage tests (ADR-036/037), hermetic. The trace store is faked,
the bridge flag uses the in-memory provider, and escalation runs in local mode — so the full
loop (check → bridge → matrix/AI classify → dispose) runs with no Docker, AWS, or network.
Classification/disposition rule coverage lives in test_routing_matrix.py; these tests cover
the loop's behavior end to end."""
import pytest

from incidents import checks as checks_svc
from incidents import flags, modes, trace_store, triage, triage_ai
from incidents.models import (
    CheckSubjectKind,
    ErrorSpan,
    Incident,
    LinkKind,
    OperatingMode,
    RecordLink,
    SessionCheck,
    Status,
    Tier,
    TriageDecision,
    TriageDisposition,
    TriageVerdict,
)


class FakeTraceStore:
    def __init__(self, spans=None):
        self.spans = spans or []

    def find_error_spans(self, kind, subject_hash, wf, wt):
        return self.spans


CODE_FAULT_SPAN = {"trace_id": "t1", "span_id": "s1", "name": "POST /orders",
                   "service": "api", "status": "ERROR", "http_status": 500, "kind": "server"}
VENDOR_SPAN = {"trace_id": "t2", "span_id": "s2", "name": "GET api.vendor.com/x",
               "service": "api", "status": "ERROR", "http_status": 502, "kind": "client"}
CLIENT_NOISE_SPAN = {"trace_id": "t3", "span_id": "s3", "name": "GET /nope",
                     "service": "api", "status": "ERROR", "http_status": 404, "kind": "server"}
# 418 matches no matrix rule — the AI fallback's cue; the stub reads no-5xx as a false positive
UNMATCHED_SPAN = {"trace_id": "t4", "span_id": "s4", "name": "GET /teapot",
                  "service": "api", "status": "ERROR", "http_status": 418, "kind": "server"}


@pytest.fixture(autouse=True)
def _reset_providers():
    yield
    trace_store.set_provider_for_tests(None)
    flags.set_provider_for_tests(None)


def _bridge_flag(on: bool):
    flags.set_provider_for_tests(flags.InMemoryProvider({triage.BRIDGE_FLAG: on}))


def _run_check(spans):
    trace_store.set_provider_for_tests(FakeTraceStore(spans))
    return checks_svc.create_and_run(
        subject_kind=CheckSubjectKind.SESSION, subject_raw="corr-1", source="e2e"
    )


# --- the T0 bridge ---

@pytest.mark.django_db
def test_bridge_flag_off_opens_nothing():
    _bridge_flag(False)
    check = _run_check([CODE_FAULT_SPAN])
    assert check.verdict == "errors_found:1"
    assert Incident.objects.count() == 0


@pytest.mark.django_db
def test_clean_check_never_bridges():
    _bridge_flag(True)
    check = _run_check([])
    assert check.verdict == "clean"
    assert Incident.objects.count() == 0


@pytest.mark.django_db
def test_indeterminate_check_never_bridges():
    # never an incident from a non-answer (ADR-022/036)
    _bridge_flag(True)
    check = checks_svc.create_check(subject_kind=CheckSubjectKind.SESSION, subject_raw="")
    checks_svc.run_session_check(check)
    check.refresh_from_db()
    assert check.verdict == "no_subject"
    assert Incident.objects.count() == 0


@pytest.mark.django_db
def test_bridge_check_ignores_non_error_verdicts():
    _bridge_flag(True)
    check = SessionCheck.objects.create(
        subject_kind=CheckSubjectKind.SESSION, subject_hash="corr-9", verdict="clean"
    )
    assert triage.bridge_check(check) is None


# --- matrix-first triage (ADR-037) ---

@pytest.mark.django_db
def test_matrix_classifies_internal_software_advisory_in_highway():
    _bridge_flag(True)
    check = _run_check([CODE_FAULT_SPAN])  # our 500 → internal/software, REAL, deterministic
    incident = Incident.objects.get()
    assert incident.source == "check"
    assert incident.dedupe_key == f"check:session:{check.subject_hash}"
    assert incident.escalation_execution_arn  # execution started at creation (ADR-001/007)
    link = RecordLink.objects.get(kind=LinkKind.CREATED_FROM)
    assert link.from_object_id == str(incident.id) and link.to_object_id == str(check.id)
    assert incident.status == Status.OPEN and incident.current_tier == Tier.T1
    assert incident.triage_verdict == TriageVerdict.REAL
    assert incident.triage_responsibility == "internal"
    assert incident.triage_fault_domain == "software"
    decision = TriageDecision.objects.get()
    assert decision.actor == TriageDecision.ASSISTANT_ACTOR
    assert decision.provider == "matrix" and decision.confidence == 1.0  # no AI call needed
    assert decision.disposition == TriageDisposition.NO_ACTION
    assert decision.mode == OperatingMode.HIGHWAY
    assert decision.evidence["spans"][0]["kind"] == "server"


@pytest.mark.django_db
def test_matrix_classifies_vendor_from_outbound_spans():
    _bridge_flag(True)
    _run_check([VENDOR_SPAN])  # outbound client-kind 502 → vendor/environment
    incident = Incident.objects.get()
    assert incident.status == Status.OPEN
    assert incident.triage_responsibility == "vendor"
    assert incident.triage_fault_domain == "environment"


@pytest.mark.django_db
def test_client_noise_is_advisory_not_false_positive():
    # ADR-037: 4xx noise is REAL (someone's requests are failing) but advisory — the former
    # stub heuristic that auto-resolved it as a false positive is retired.
    _bridge_flag(True)
    _run_check([CLIENT_NOISE_SPAN])
    incident = Incident.objects.get()
    assert incident.status == Status.OPEN
    assert incident.triage_responsibility == "client"
    assert incident.triage_verdict == TriageVerdict.REAL


@pytest.mark.django_db
def test_race_mode_auto_escalates_internal_fault_to_t2():
    # Dave's scenario: during a release window, an internal fault is a presumed escaped defect.
    _bridge_flag(True)
    modes.open_race_window("t3a", reason="release")
    _run_check([CODE_FAULT_SPAN])
    incident = Incident.objects.get()
    assert incident.status == Status.OPEN
    assert incident.current_tier == Tier.T2  # escalated by policy, not by SLA breach
    decision = TriageDecision.objects.get()
    assert decision.disposition == TriageDisposition.AUTO_ESCALATE
    assert decision.mode == OperatingMode.RACE
    escalated = incident.transitions.get(to_tier=Tier.T2)
    assert escalated.actor == TriageDecision.ASSISTANT_ACTOR
    assert "race-mode policy" in escalated.reason


# --- the AI fallback (unmatched evidence) ---

@pytest.mark.django_db
def test_unmatched_evidence_falls_back_to_ai_and_auto_resolves_fp_in_highway():
    _bridge_flag(True)
    _run_check([UNMATCHED_SPAN])  # 418 → no rule → stub says false positive
    incident = Incident.objects.get()
    assert incident.status == Status.RESOLVED
    decision = TriageDecision.objects.get()
    assert decision.provider == "stub"  # the AI seam ran, not the matrix
    assert decision.verdict == TriageVerdict.FALSE_POSITIVE
    assert decision.disposition == TriageDisposition.AUTO_RESOLVE
    resolved = incident.transitions.get(to_status=Status.RESOLVED)
    assert resolved.actor == TriageDecision.ASSISTANT_ACTOR
    assert "false_positive" in resolved.reason


@pytest.mark.django_db
def test_race_mode_defers_false_positive_to_humans():
    _bridge_flag(True)
    modes.open_race_window("t3a", reason="release")
    _run_check([UNMATCHED_SPAN])
    incident = Incident.objects.get()
    assert incident.status == Status.OPEN  # verdict recorded, a human confirms in race mode
    decision = TriageDecision.objects.get()
    assert decision.mode == OperatingMode.RACE
    assert decision.disposition == TriageDisposition.NO_ACTION


@pytest.mark.django_db
def test_triage_error_soft_fails_and_sla_backstop_holds(monkeypatch):
    _bridge_flag(True)

    def boom(evidence):
        raise triage_ai.TriageError("provider down")

    monkeypatch.setattr(triage_ai, "classify", boom)
    _run_check([UNMATCHED_SPAN])  # unmatched → fallback → provider down
    incident = Incident.objects.get()
    assert incident.status == Status.OPEN          # untouched — SLA engine is the backstop
    assert incident.triage_verdict == ""           # nothing denormalized
    assert TriageDecision.objects.count() == 0
    assert incident.events.filter(body__startswith="T1 triage failed").exists()


# --- dedupe semantics across the loop ---

@pytest.mark.django_db
def test_flapping_subject_dedupes_and_corroborates():
    _bridge_flag(True)
    _run_check([CODE_FAULT_SPAN])          # opens the incident (REAL → stays open)
    second = _run_check([CODE_FAULT_SPAN])  # same subject re-fires while OPEN
    assert Incident.objects.count() == 1    # ON CONFLICT no-op (ADR-009)
    rel = RecordLink.objects.get(kind=LinkKind.RELATES_TO)
    assert rel.from_object_id == str(second.id)
    assert TriageDecision.objects.count() == 1  # triage runs on creation only


@pytest.mark.django_db
def test_refire_after_resolve_opens_fresh_incident():
    _bridge_flag(True)
    _run_check([UNMATCHED_SPAN])  # auto-resolved as false positive (highway)
    _run_check([UNMATCHED_SPAN])  # same subject after RESOLVED → new incident (ADR-009)
    assert Incident.objects.count() == 2
    assert Incident.objects.filter(status=Status.RESOLVED).count() == 2


# --- evidence rendering ---

@pytest.mark.django_db
def test_evidence_renders_spans_and_handles_none():
    check = SessionCheck.objects.create(
        subject_kind=CheckSubjectKind.SESSION, subject_hash="corr-7", verdict="errors_found:1"
    )
    ErrorSpan.objects.create(session_check=check, trace_id="t9", name="GET /x",
                             service="api", status="ERROR", http_status=500, kind="server")
    incident = Incident.objects.create(source="check", title="t", dedupe_key="k1")
    spans = list(check.error_spans.all())
    markdown, snapshot = triage._evidence(incident, check, spans)
    assert "HTTP 500" in markdown and snapshot["spans"][0]["trace_id"] == "t9"
    markdown2, snapshot2 = triage._evidence(incident, check, [])
    assert "(none recorded)" in markdown2 and snapshot2["spans"] == []
