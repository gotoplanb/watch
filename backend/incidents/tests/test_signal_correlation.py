"""Signal correlation (ADR-059): a re-fire or recovery of a condition already being assessed attaches
to that assessment; a recovery rechecks with our own telemetry and never resolves an incident."""
import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from incidents import assessments, automation
from incidents.models import Assessment, AssessmentPolicy, DiagnosticRule, ServiceDependency

pytestmark = pytest.mark.django_db
SECRET = "signal-secret"


def signal(state="active", event="e1", key="monitor-778", **scope):
    data = {"title": "Square API down", "source": "uptimerobot", "signal": f"{state} {event}",
            "source_event_id": event, "correlation_key": key, "state": state,
            "customer": "A", "service": "checkout"}
    return automation.receive_signal(**{**data, **scope})


def kinds(row):
    return list(row.history.order_by("pk").values_list("kind", flat=True))


def test_refire_attaches_instead_of_opening_another():
    [(row, created)] = signal()
    assert created
    assert row.correlation_key == "monitor-778"
    [(again, created)] = signal(event="e2")
    assert (again.pk, created) == (row.pk, False)
    assert kinds(row)[-1] == "signal_repeated"
    assert Assessment.objects.count() == 1


def test_resolved_with_nothing_open_opens_nothing():
    assert signal(state="resolved") == []
    assert not Assessment.objects.exists()


def test_resolved_without_policy_records_evidence_only():
    [(row, _)] = signal()
    signal(state="resolved", event="e2")
    signal(state="resolved", event="e2")  # retried delivery
    row.refresh_from_db()
    assert kinds(row).count("signal_resolved") == 1
    assert "recheck_requested" not in kinds(row)
    assert row.status == "new"
    event = row.history.get(kind="signal_resolved")
    assert (event.actor, event.data["state"]) == (automation.SIGNALS_ACTOR, "resolved")


def test_unkeyed_signals_correlate_only_by_scope():
    signal(key="", customer="", service="")
    signal(key="", event="e2", customer="", service="")
    assert Assessment.objects.count() == 2  # unscoped: nothing shared to attach to
    [(row, _)] = signal(key="")
    [routed] = signal(key="", event="e3")  # same customer/service: ADR-060 attaches
    assert (routed[0].pk, routed[1], routed.how) == (row.pk, False, "attached")


@pytest.fixture
def healthy_policy():
    rule = DiagnosticRule.objects.create(
        name="Checkout payment errors", provider="fixture", query="q", comparator="above",
        threshold=2, minimum_samples=100, fixture_result={"value": 0.2, "samples": 800})
    policy = AssessmentPolicy.objects.create(name="Checkout", active=True, customer="A",
                                             service="checkout", auto_close_no_issue=True)
    policy.rules.add(rule)
    return policy


def test_recovery_rechecks_and_closes_only_when_our_data_agrees(healthy_policy, settings,
                                                                django_capture_on_commit_callbacks):
    settings.DEBUG = True
    [(row, _)] = signal(payload={"fixture": True})
    assessments.dispose(row.pk, actor="t1a", outcome="monitoring", reason="Square unstable")
    Assessment.objects.filter(pk=row.pk).update(automation_state="complete")
    signal(state="resolved", event="e2")
    row.refresh_from_db()
    assert row.automation_state == "queued"
    assert kinds(row)[-2:] == ["signal_resolved", "recheck_requested"]
    with django_capture_on_commit_callbacks(execute=True):
        automation.run(row.pk)
    row.refresh_from_db()
    assert (row.verdict, row.status) == ("no_issue", "no_issue")


def test_recovery_does_not_requeue_running_automation(healthy_policy):
    [(row, _)] = signal()
    Assessment.objects.filter(pk=row.pk).update(automation_state="running")
    signal(state="resolved", event="e2")
    assert "recheck_requested" not in kinds(row)


def test_recovery_never_resolves_a_promoted_incident(django_capture_on_commit_callbacks):
    [(row, _)] = signal()
    assessments.add_evidence(row.pk, actor="t1a", summary="errors", result="6% for 15m")
    assessments.conclude(row.pk, actor="t1a", verdict="degraded", confidence="high", reason="r")
    with django_capture_on_commit_callbacks(execute=True):
        incident, _ = assessments.promote(row.pk, actor="t1a", reason="impact")
    signal(state="resolved", event="e2")
    incident.refresh_from_db()
    assert incident.status == "OPEN"
    note = row.history.get(kind="signal_resolved").body
    assert f"{incident.number} stays open until a responder resolves it" in note
    incident.status = "RESOLVED"
    incident.save(update_fields=["status"])
    assert signal(state="resolved", event="e3") == []  # nothing left working this condition


def test_supplier_recovery_reaches_every_fanned_out_scope():
    for customer in ("A", "B"):
        ServiceDependency.objects.create(customer=customer, service="checkout", supplier="Square",
                                         active=True)
    opened = signal(customer="", service="", supplier="Square")
    assert len(opened) == 2
    recovered = signal(state="resolved", event="e2", customer="", service="", supplier="Square")
    assert sorted(r.customer for r, _ in recovered) == ["A", "B"]


# --- API -----------------------------------------------------------------------------------------

@pytest.fixture
def intake(settings):
    settings.INTAKE_WEBHOOK_SECRET = SECRET
    client = APIClient()
    client.credentials(HTTP_X_WATCH_WEBHOOK_SECRET=SECRET)
    return client


def _post(client, path, **fields):
    body = {"title": "Square API down", "source": "uptimerobot", "signal": "s",
            "correlation_key": "monitor-778", **fields}
    return client.post(path, body, format="json")


def test_signal_api_reports_the_outcome(intake):
    opened = _post(intake, "/api/signals/intake", source_event_id="e1")
    assert (opened.status_code, opened.json()["outcome"]) == (201, "created")
    again = _post(intake, "/api/signals/intake", source_event_id="e2")
    assert (again.status_code, again.json()["outcome"]) == (200, "correlated")
    Assessment.objects.update(status="no_issue")
    orphan = _post(intake, "/api/signals/intake", source_event_id="e3", state="resolved")
    assert (orphan.json()["outcome"], orphan.json()["assessments"]) == ("nothing open", [])
    assert _post(intake, "/api/signals/intake", state="ended").status_code == 400


def test_single_assessment_intake_keeps_the_key_but_does_not_correlate(intake):
    first = _post(intake, "/api/assessments/intake", source_event_id="e1", state="resolved")
    assert first.status_code == 201
    assert Assessment.objects.get().correlation_key == "monitor-778"
    assert User.objects.count() == 0
