"""Routing-matrix tests (ADR-037): the SHIPPED triage-matrix.yaml is what these tests load —
they validate the real policy artifact, every classification rule, mixed-evidence precedence,
and the full disposition table (including the race column's internal-escalates principle)."""
from dataclasses import dataclass

import pytest

from incidents import routing_matrix
from incidents.models import OperatingMode, TriageDisposition, TriageVerdict


@dataclass
class Span:
    http_status: int | None = None
    kind: str = "server"


@pytest.fixture(autouse=True)
def _reset_matrix():
    routing_matrix.set_matrix_for_tests(None)
    yield
    routing_matrix.set_matrix_for_tests(None)


# --- the shipped document loads and is internally complete ---

def test_shipped_matrix_loads_and_validates():
    data = routing_matrix.matrix()
    assert data["classification"] and data["precedence"] and data["disposition"]


# --- classification rules (Mapping A) ---

@pytest.mark.parametrize("span,expected", [
    (Span(500), ("internal", "software")),           # our unhandled exception
    (Span(502), ("internal", "environment")),        # our infra path
    (Span(503), ("internal", "environment")),
    (Span(504), ("internal", "environment")),
    (Span(None), ("internal", "environment")),       # no status: DNS/conn/timeout
    (Span(404), ("client", "software")),             # their wrong requests
    (Span(400), ("client", "software")),
    (Span(401), ("client", "environment")),          # creds/config
    (Span(429), ("client", "environment")),          # quota
    (Span(502, kind="client"), ("vendor", "environment")),  # outbound: their 5xx
    (Span(404, kind="client"), ("vendor", "software")),     # outbound: contract mismatch
])
def test_classification_rules(span, expected):
    assert routing_matrix.classify([span]) == expected


def test_unmatched_evidence_defers_to_ai():
    assert routing_matrix.classify([Span(418)]) is None  # no rule → AI fallback's cue
    assert routing_matrix.classify([]) is None


def test_mixed_evidence_aggregates_to_worst_cell_by_precedence():
    spans = [Span(404), Span(503), Span(500)]  # client noise + infra + code
    assert routing_matrix.classify(spans) == ("internal", "software")
    spans = [Span(404), Span(502, kind="client")]  # client noise + vendor outage
    assert routing_matrix.classify(spans) == ("vendor", "environment")


# --- disposition table (Mapping B) ---

@pytest.mark.parametrize("cell,mode,expected", [
    # race tightens the INTERNAL cells only (escaped defect / deploy-induced fault)
    (("internal", "software"), OperatingMode.RACE, TriageDisposition.AUTO_ESCALATE),
    (("internal", "environment"), OperatingMode.RACE, TriageDisposition.AUTO_ESCALATE),
    (("internal", "software"), OperatingMode.HIGHWAY, TriageDisposition.NO_ACTION),
    (("vendor", "environment"), OperatingMode.RACE, TriageDisposition.NO_ACTION),
    (("vendor", "software"), OperatingMode.RACE, TriageDisposition.NO_ACTION),
    (("client", "software"), OperatingMode.RACE, TriageDisposition.NO_ACTION),
    (("client", "environment"), OperatingMode.HIGHWAY, TriageDisposition.NO_ACTION),
])
def test_dispose_real_by_cell_and_mode(cell, mode, expected):
    assert routing_matrix.dispose(*cell, TriageVerdict.REAL, mode) == expected


@pytest.mark.parametrize("mode,expected", [
    (OperatingMode.HIGHWAY, TriageDisposition.AUTO_RESOLVE),
    (OperatingMode.RACE, TriageDisposition.NO_ACTION),  # a human confirms during a release
])
def test_dispose_false_positive_wildcard(mode, expected):
    assert routing_matrix.dispose("internal", "environment", TriageVerdict.FALSE_POSITIVE, mode) == expected


def test_dispose_undetermined_never_acts():
    for mode in OperatingMode.values:
        assert routing_matrix.dispose(
            "client", "software", TriageVerdict.UNDETERMINED, mode
        ) == TriageDisposition.NO_ACTION


# --- validation rejects malformed matrices ---

def _valid_stub_matrix():
    cells = [f"{r}/{d}" for r in ("client", "internal", "vendor") for d in ("environment", "software")]
    return {
        "classification": [{"match": {"statuses": [500]}, "cell": "internal/software"}],
        "precedence": list(cells),
        "disposition": {
            "real": {c: {"highway": "no_action", "race": "no_action"} for c in cells},
            "false_positive": {"*": {"highway": "auto_resolve", "race": "no_action"}},
            "undetermined": {"*": {"highway": "no_action", "race": "no_action"}},
        },
    }


@pytest.mark.parametrize("mutate,message", [
    (lambda m: m["classification"].append({"match": {"statuses": [1]}, "cell": "us/hardware"}),
     "unknown cell"),
    (lambda m: m["classification"].append({"cell": "internal/software"}), "no match conditions"),
    (lambda m: m["precedence"].pop(), "every responsibility/fault_domain cell"),
    (lambda m: m["disposition"].pop("undetermined"), "must cover exactly the verdicts"),
    (lambda m: m["disposition"]["real"].pop("client/software"), "must cover every cell"),
    (lambda m: m["disposition"]["real"]["client/software"].pop("race"), "every operating mode"),
    (lambda m: m["disposition"]["real"]["client/software"].update(race="page_everyone"),
     "unknown action"),
])
def test_validation_rejects(mutate, message):
    data = _valid_stub_matrix()
    mutate(data)
    with pytest.raises(routing_matrix.MatrixError, match=message):
        routing_matrix._validate(data)


def test_load_missing_file_raises(settings):
    settings.TRIAGE_MATRIX_PATH = "/nonexistent/matrix.yaml"
    with pytest.raises(routing_matrix.MatrixError, match="cannot load"):
        routing_matrix.matrix()


def test_validate_rejects_non_mapping_root():
    with pytest.raises(routing_matrix.MatrixError, match="root must be a mapping"):
        routing_matrix._validate(["not", "a", "dict"])
