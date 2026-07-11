"""Check→incident bridge + T1 triage tests (ADR-036), hermetic. The trace store is faked, the
bridge flag uses the in-memory provider, and escalation runs in local mode — so the full loop
(check → bridge → classify → dispose) runs with no Docker, AWS, or network."""
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
    TriageDecision,
    TriageDisposition,
    TriageVerdict,
)


class FakeTraceStore:
    def __init__(self, spans=None):
        self.spans = spans or []

    def find_error_spans(self, kind, subject_hash, wf, wt):
        return self.spans


SERVER_ERROR_SPAN = {"trace_id": "t1", "span_id": "s1", "name": "POST /orders",
                     "service": "api", "status": "ERROR", "http_status": 503}
CLIENT_ERROR_SPAN = {"trace_id": "t2", "span_id": "s2", "name": "GET /nope",
                     "service": "api", "status": "ERROR", "http_status": 404}


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


# --- dispose(): pure, deterministic — AI classifies, THIS decides ---

@pytest.mark.parametrize("verdict,mode,expected", [
    (TriageVerdict.FALSE_POSITIVE, OperatingMode.HIGHWAY, TriageDisposition.AUTO_RESOLVE),
    (TriageVerdict.FALSE_POSITIVE, OperatingMode.RACE, TriageDisposition.NO_ACTION),
    (TriageVerdict.REAL, OperatingMode.HIGHWAY, TriageDisposition.NO_ACTION),
    (TriageVerdict.UNDETERMINED, OperatingMode.HIGHWAY, TriageDisposition.NO_ACTION),
])
def test_dispose_only_false_positive_in_highway_acts(verdict, mode, expected):
    assert triage.dispose(verdict, mode) == expected


# --- the T0 bridge ---

@pytest.mark.django_db
def test_bridge_flag_off_opens_nothing():
    _bridge_flag(False)
    check = _run_check([SERVER_ERROR_SPAN])
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
def test_errors_found_bridges_with_provenance_and_triage():
    _bridge_flag(True)
    check = _run_check([SERVER_ERROR_SPAN])  # 5xx → stub classifies REAL
    incident = Incident.objects.get()
    assert incident.source == "check"
    assert incident.dedupe_key == f"check:session:{check.subject_hash}"
    assert incident.escalation_execution_arn  # execution started at creation (ADR-001/007)
    # provenance: incident created_from check
    link = RecordLink.objects.get(kind=LinkKind.CREATED_FROM)
    assert link.from_object_id == str(incident.id) and link.to_object_id == str(check.id)
    # REAL verdict: advisory only — incident rides the SLA engine, still open at T1
    incident.refresh_from_db()
    assert incident.status == Status.OPEN
    assert incident.triage_verdict == TriageVerdict.REAL
    assert incident.triage_responsibility == "internal"
    assert incident.triage_fault_domain == "software"
    decision = TriageDecision.objects.get()
    assert decision.actor == TriageDecision.ASSISTANT_ACTOR
    assert decision.disposition == TriageDisposition.NO_ACTION
    assert decision.mode == OperatingMode.HIGHWAY
    assert decision.evidence["spans"][0]["http_status"] == 503


@pytest.mark.django_db
def test_false_positive_auto_resolves_in_highway():
    _bridge_flag(True)
    _run_check([CLIENT_ERROR_SPAN])  # no 5xx → stub classifies FALSE_POSITIVE
    incident = Incident.objects.get()
    assert incident.status == Status.RESOLVED
    decision = TriageDecision.objects.get()
    assert decision.verdict == TriageVerdict.FALSE_POSITIVE
    assert decision.disposition == TriageDisposition.AUTO_RESOLVE
    resolved = incident.transitions.get(to_status=Status.RESOLVED)
    assert resolved.actor == TriageDecision.ASSISTANT_ACTOR
    assert "false_positive" in resolved.reason


@pytest.mark.django_db
def test_race_mode_defers_false_positive_to_humans():
    _bridge_flag(True)
    modes.open_race_window("t3a", reason="release")
    _run_check([CLIENT_ERROR_SPAN])
    incident = Incident.objects.get()
    assert incident.status == Status.OPEN  # verdict recorded, no auto-resolve in race mode
    decision = TriageDecision.objects.get()
    assert decision.mode == OperatingMode.RACE
    assert decision.disposition == TriageDisposition.NO_ACTION


@pytest.mark.django_db
def test_flapping_subject_dedupes_and_corroborates():
    _bridge_flag(True)
    _run_check([SERVER_ERROR_SPAN])          # opens the incident (REAL → stays open)
    second = _run_check([SERVER_ERROR_SPAN])  # same subject re-fires while OPEN
    assert Incident.objects.count() == 1      # ON CONFLICT no-op (ADR-009)
    # the second check attaches as corroborating evidence, not a new incident
    rel = RecordLink.objects.get(kind=LinkKind.RELATES_TO)
    assert rel.from_object_id == str(second.id)
    assert TriageDecision.objects.count() == 1  # triage runs on creation only


@pytest.mark.django_db
def test_refire_after_resolve_opens_fresh_incident():
    _bridge_flag(True)
    _run_check([CLIENT_ERROR_SPAN])  # auto-resolved as false positive
    _run_check([CLIENT_ERROR_SPAN])  # same subject after RESOLVED → new incident (ADR-009)
    assert Incident.objects.count() == 2
    assert Incident.objects.filter(status=Status.RESOLVED).count() == 2


@pytest.mark.django_db
def test_triage_error_soft_fails_and_sla_backstop_holds(monkeypatch):
    _bridge_flag(True)

    def boom(evidence):
        raise triage_ai.TriageError("provider down")

    monkeypatch.setattr(triage_ai, "classify", boom)
    _run_check([SERVER_ERROR_SPAN])
    incident = Incident.objects.get()
    assert incident.status == Status.OPEN          # untouched — SLA engine is the backstop
    assert incident.triage_verdict == ""           # nothing denormalized
    assert TriageDecision.objects.count() == 0
    assert incident.events.filter(body__startswith="T1 triage failed").exists()


@pytest.mark.django_db
def test_bridge_check_ignores_non_error_verdicts():
    _bridge_flag(True)
    check = SessionCheck.objects.create(
        subject_kind=CheckSubjectKind.SESSION, subject_hash="corr-9", verdict="clean"
    )
    assert triage.bridge_check(check) is None


@pytest.mark.django_db
def test_evidence_renders_spans_and_handles_none():
    _bridge_flag(True)
    check = SessionCheck.objects.create(
        subject_kind=CheckSubjectKind.SESSION, subject_hash="corr-7", verdict="errors_found:1"
    )
    ErrorSpan.objects.create(session_check=check, trace_id="t9", name="GET /x",
                             service="api", status="ERROR", http_status=500)
    incident = Incident.objects.create(source="check", title="t", dedupe_key="k1")
    markdown, snapshot = triage._evidence(incident, check)
    assert "HTTP 500" in markdown and snapshot["spans"][0]["trace_id"] == "t9"
    # and the empty-span rendering branch
    empty = SessionCheck.objects.create(
        subject_kind=CheckSubjectKind.SESSION, subject_hash="corr-8", verdict="errors_found:1"
    )
    markdown2, snapshot2 = triage._evidence(incident, empty)
    assert "(none recorded)" in markdown2 and snapshot2["spans"] == []
