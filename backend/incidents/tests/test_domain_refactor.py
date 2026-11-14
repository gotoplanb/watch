"""Pure helpers extracted from automation/diagnostics/reporting during the Sonar refactor."""
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace

import pytest

from incidents import automation, diagnostics, reporting


def _run(state, breached=None, error="", name="check"):
    result = {} if breached is None else {"breached": breached}
    return SimpleNamespace(state=state, result=result, error=error, rule=SimpleNamespace(name=name))


def test_summarize_all_within_bounds_is_high_confidence_no_issue():
    assert automation._summarize([_run("done", False, name="a")]) == (
        "no_issue", "high", "a: within configured bounds", False, False)


def test_summarize_breach_with_partial_failure():
    verdict, confidence, reason, breached, failed = automation._summarize(
        [_run("done", True, name="a"), _run("failed", error="boom", name="b")])
    assert (verdict, confidence, breached, failed) == ("degraded", "medium", True, True)
    assert reason == "a: threshold breached\nb: boom"


def test_summarize_all_failed_is_low_confidence_unknown():
    assert automation._summarize([_run("failed", error="x", name="a")])[:2] == ("unknown", "low")


def test_impact_branches():
    policy = SimpleNamespace(impact="major")
    assert automation._impact(policy, True, True) == "major"
    assert automation._impact(policy, False, True) == "unknown"
    assert automation._impact(policy, False, False) == "none"


@pytest.mark.parametrize("comparator,value,baseline,expected", [
    ("above", 11, None, True), ("above", 9, None, False),
    ("below", 9, None, True), ("deviation", 130, 100, True), ("deviation", 105, 100, False),
])
def test_breached_comparators(comparator, value, baseline, expected):
    rule = SimpleNamespace(comparator=comparator, threshold=10 if comparator != "deviation" else 20)
    assert diagnostics._breached(rule, value, baseline) is expected


@pytest.mark.parametrize("comparator,baseline", [("deviation", None), ("deviation", 0), ("bogus", 1)])
def test_breached_rejects_invalid_comparator_or_baseline(comparator, baseline):
    rule = SimpleNamespace(comparator=comparator, threshold=1)
    with pytest.raises(diagnostics.DiagnosticError, match="nonzero baseline"):
        diagnostics._breached(rule, 5, baseline)


def test_incident_clocks_counts_negative_and_missing_clocks():
    promoted = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
    common = {"promoted_at": promoted, "severity": "P2", "recurrence_key": ""}
    rows = [SimpleNamespace(incident_id=1, supplier="", **common),
            SimpleNamespace(incident_id=2, supplier="Acme", **common)]
    acks = {1: promoted - timedelta(seconds=5), 2: promoted + timedelta(seconds=60)}
    resolutions = {2: promoted + timedelta(seconds=120)}
    by_supplier, *_ = reporting._incident_counts(rows)
    ack, resolve, invalid = reporting._incident_clocks(rows, acks, resolutions, by_supplier)
    assert (ack, resolve, invalid) == ([60.0], [120.0], 1)
    assert by_supplier["Acme"]["durations"] == [120.0]
    assert by_supplier["Unassigned"] == {"incidents": 1, "durations": []}


def test_percent_handles_empty_total():
    assert reporting._percent(1, 0) is None
    assert reporting._percent(1, 3) == pytest.approx(33.3)
