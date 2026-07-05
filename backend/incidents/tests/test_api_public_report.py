"""Public, unauthenticated status-page report surface (ADR-027): the two anonymous write endpoints.
No shared secret; bounded by a per-IP throttle + serializer caps. Hermetic via APIClient."""
import pytest
from rest_framework.test import APIClient

from incidents import checks, trace_store
from incidents.models import CheckSource, CheckStatus, Incident, SessionCheck

INCIDENT_URL = "/api/report/incident"
CHECK_URL = "/api/report/check"
SESSION_ID = "0123456789abcdef0123456789abcdef"


class FakeTraceStore:
    def find_error_spans(self, *a, **k):
        return []


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture(autouse=True)
def _clean(settings):
    # Fresh trace store + throttle bucket per test (LocMemCache persists across the process).
    from django.core.cache import cache
    cache.clear()
    trace_store.set_provider_for_tests(FakeTraceStore())
    yield
    trace_store.set_provider_for_tests(None)
    cache.clear()


@pytest.mark.django_db
def test_report_incident_creates_no_secret(client, monkeypatch):
    from incidents import escalation
    monkeypatch.setattr(escalation, "start_escalation", lambda inc: f"local:{inc.id}")
    res = client.post(INCIDENT_URL, {"title": "checkout is down", "detail": "500 on pay"}, format="json")
    assert res.status_code == 201 and res.json()["created"] is True
    inc = Incident.objects.get()
    assert inc.source == "status-page" and inc.title == "checkout is down"
    assert inc.payload["detail"] == "500 on pay" and inc.payload["reporter"] == "public"
    assert inc.escalation_execution_arn == f"local:{inc.id}"


@pytest.mark.django_db
def test_report_incident_dedupes_repeat(client, monkeypatch):
    from incidents import escalation
    monkeypatch.setattr(escalation, "start_escalation", lambda inc: "local")
    body = {"title": "same", "detail": "identical body"}
    assert client.post(INCIDENT_URL, body, format="json").status_code == 201
    again = client.post(INCIDENT_URL, body, format="json")
    assert again.status_code == 200 and again.json()["created"] is False
    assert Incident.objects.count() == 1


@pytest.mark.django_db
def test_report_incident_rejects_blank_title(client):
    assert client.post(INCIDENT_URL, {"detail": "no title"}, format="json").status_code == 400


@pytest.mark.django_db
def test_report_incident_caps_title_length(client):
    res = client.post(INCIDENT_URL, {"title": "x" * 201}, format="json")
    assert res.status_code == 400


@pytest.mark.django_db
def test_report_check_creates_self_report(client):
    res = client.post(CHECK_URL, {"session": SESSION_ID.upper()}, format="json")
    assert res.status_code == 201
    check = SessionCheck.objects.get()
    assert check.source == CheckSource.SELF_REPORT
    assert res.json()["status"] == CheckStatus.DONE  # local mode ran it (clean, no spans)
    # verdict is NOT leaked to the anonymous submitter
    assert set(res.json().keys()) == {"id", "status"}


@pytest.mark.django_db
def test_report_check_normalizes_case_to_lowercase(client, monkeypatch):
    captured = {}
    real = checks.create_and_run

    def spy(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(checks, "create_and_run", spy)
    client.post(CHECK_URL, {"session": SESSION_ID.upper()}, format="json")
    assert captured["subject_raw"] == SESSION_ID  # lowercased


@pytest.mark.django_db
def test_report_check_rejects_malformed_session(client):
    for bad in ["short", "g" * 32, SESSION_ID + "00"]:
        assert client.post(CHECK_URL, {"session": bad}, format="json").status_code == 400
    assert SessionCheck.objects.count() == 0


@pytest.mark.django_db
def test_preflight_is_allowed_and_carries_cors_headers(client):
    res = client.options(INCIDENT_URL)
    assert res.status_code == 200
    assert res["Access-Control-Allow-Origin"] == "*"  # settings_test default
    assert "POST" in res["Access-Control-Allow-Methods"]
    assert "Content-Type" in res["Access-Control-Allow-Headers"]


@pytest.mark.django_db
def test_throttle_clamps_burst(client, monkeypatch):
    from incidents import escalation, views
    monkeypatch.setattr(escalation, "start_escalation", lambda inc: "local")
    monkeypatch.setattr(views._PublicReportThrottle, "THROTTLE_RATES", {"public_report": "2/min"})
    codes = [client.post(INCIDENT_URL, {"title": "spam", "detail": f"n{i}"}, format="json").status_code for i in range(3)]
    assert codes[:2] == [201, 201] and codes[2] == 429


@pytest.mark.django_db
def test_preflight_not_throttled(client, monkeypatch):
    from incidents import views
    monkeypatch.setattr(views._PublicReportThrottle, "THROTTLE_RATES", {"public_report": "1/min"})
    # A burst of preflights never trips the limiter; the following POST still succeeds.
    for _ in range(5):
        assert client.options(INCIDENT_URL).status_code == 200
    assert client.post(CHECK_URL, {"session": SESSION_ID}, format="json").status_code == 201
