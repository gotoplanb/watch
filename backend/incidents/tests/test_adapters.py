"""Vendor adapters (ADR-065, #69), with payloads shaped like each vendor's documented webhook."""
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from incidents import adapters
from incidents.models import Assessment, EvidenceCapture, MaintenanceWindow, StatusPage

pytestmark = pytest.mark.django_db
SECRET = "adapter-secret"


@pytest.fixture
def client(settings):
    settings.INTAKE_WEBHOOK_SECRET = SECRET
    return APIClient()


def robot(alert_type="1", **extra):
    return {"monitorID": "778", "monitorURL": "https://api.square.example/health",
            "monitorFriendlyName": "Square API", "alertType": alert_type,
            "alertTypeFriendlyName": {"1": "Down", "2": "Up", "3": "SSL expiry"}[alert_type],
            "alertDetails": "HTTP 503 - Service Unavailable", "alertDateTime": "1794835205", **extra}


# --- Uptime Robot --------------------------------------------------------------------------------

def test_uptimerobot_down_then_up_is_one_assessment(client):
    url = "/api/signals/uptimerobot?supplier=&customer=A&service=checkout"
    down = client.post(url, robot("1"), format="json", HTTP_X_WATCH_WEBHOOK_SECRET=SECRET)
    assert (down.status_code, down.json()["outcome"]) == (201, "created")
    row = Assessment.objects.get()
    assert (row.title, row.correlation_key, row.customer) == ("Square API is Down", "monitor-778", "A")
    up = client.post(url, robot("2", alertDateTime="1794835305"), format="json",
                     HTTP_X_WATCH_WEBHOOK_SECRET=SECRET)
    assert up.json()["outcome"] == "correlated"
    assert row.history.filter(kind="signal_resolved").exists()


def test_uptimerobot_form_encoded_and_bearer_auth(client):
    reply = client.post("/api/signals/uptimerobot?customer=A&service=checkout", robot("3"),
                        HTTP_AUTHORIZATION=f"Bearer {SECRET}")
    assert reply.status_code == 201


def test_uptimerobot_refusals(client):
    assert client.post("/api/signals/uptimerobot", robot(), format="json").status_code == 401
    unfilled = client.post("/api/signals/uptimerobot", robot(monitorID="*monitorID*"), format="json",
                           HTTP_X_WATCH_WEBHOOK_SECRET=SECRET)
    assert unfilled.status_code == 400
    assert "Send Test" in unfilled.json()["detail"]
    bad_type = robot(alertType="9")
    with pytest.raises(adapters.AdapterError, match="alertType"):
        adapters.uptimerobot(bad_type, {})


# --- Alertmanager --------------------------------------------------------------------------------

def alert(status="firing", fingerprint="abc123"):
    return {"status": status, "fingerprint": fingerprint, "startsAt": "2026-11-16T15:00:00Z",
            "labels": {"alertname": "CheckoutErrors", "customer": "A", "service": "checkout"},
            "annotations": {"summary": "5xx above 2%", "description": "checkout 5xx at 4%"}}


def test_alertmanager_one_signal_per_alert_and_resolution(client):
    body = {"version": "4", "status": "firing", "alerts": [alert(), alert(fingerprint="def456")]}
    reply = client.post("/api/signals/alertmanager", body, format="json",
                        HTTP_AUTHORIZATION=f"Bearer {SECRET}")
    assert reply.status_code == 201
    row = Assessment.objects.get()  # same customer/service: the second alert attaches (ADR-060)
    assert row.title == "CheckoutErrors: 5xx above 2%"
    assert row.history.filter(kind="related_signal").exists()
    resolved = client.post("/api/signals/alertmanager", {"alerts": [alert("resolved")]},
                           format="json", HTTP_AUTHORIZATION=f"Bearer {SECRET}")
    assert resolved.json()["outcome"] == "correlated"


def test_alertmanager_refusals(client):
    assert client.post("/api/signals/alertmanager", {"alerts": []}, format="json",
                       HTTP_AUTHORIZATION=f"Bearer {SECRET}").status_code == 400
    with pytest.raises(adapters.AdapterError, match="fingerprint or an alertname"):
        adapters.alertmanager({"alerts": [{"labels": {}}]})
    titled = adapters.alertmanager({"alerts": [{"fingerprint": "x", "labels": {"alertname": "Up"}}]})
    assert titled[0]["title"] == "Up"


# --- Statuspage ----------------------------------------------------------------------------------

@pytest.fixture
def square():
    return StatusPage.objects.create(supplier="Square", host="status.square.example")


def incident(status="investigating", update="u1", **extra):
    return {"meta": {}, "page": {"id": "p"}, "incident": {
        "id": "inc9", "name": "Elevated payment errors", "status": status, "shortlink": "https://stspg.io/x",
        "scheduled_for": None, "scheduled_until": None,
        "incident_updates": [{"id": "u0", "body": "older", "created_at": "2026-11-16T14:00:00Z"},
                             {"id": update, "body": "We're investigating.", "created_at": "2026-11-16T15:00:00Z"}],
        **extra}}


def test_statuspage_incident_cites_its_page_and_resolves(client, square):
    url = f"/api/signals/statuspage/{square.webhook_token}"
    opened = client.post(url, incident(), format="json")
    assert opened.status_code == 201
    row = Assessment.objects.get()
    assert (row.supplier, row.correlation_key) == ("Square", "statuspage-incident-inc9")
    assert row.signal == "[investigating] We're investigating."
    assert EvidenceCapture.objects.get().url == "https://status.square.example/incidents/inc9"
    done = client.post(url, incident("resolved", update="u2"), format="json")
    assert done.json()["outcome"] == "correlated"


def test_statuspage_component_update(client, square):
    body = {"component_update": {"id": "cu1", "component_id": "c1", "old_status": "operational",
                                 "new_status": "major_outage"},
            "component": {"id": "c1", "name": "Payments API"}}
    reply = client.post(f"/api/signals/statuspage/{square.webhook_token}", body, format="json")
    assert Assessment.objects.get().title == "Square: Payments API is major outage"
    assert reply.status_code == 201
    back = {**body, "component_update": {**body["component_update"], "id": "cu2", "new_status": "operational"}}
    assert client.post(f"/api/signals/statuspage/{square.webhook_token}", back,
                       format="json").json()["outcome"] == "correlated"


def test_statuspage_scheduled_maintenance_becomes_a_window(client, square):
    start = timezone.now() + timedelta(hours=1)
    planned = incident("scheduled", scheduled_for=start.isoformat(),
                       scheduled_until=(start + timedelta(hours=2)).isoformat(), name="Database upgrade")
    url = f"/api/signals/statuspage/{square.webhook_token}"
    assert client.post(url, planned, format="json").json()["outcome"] == "maintenance window"
    window = MaintenanceWindow.objects.get()
    assert (window.supplier, window.reason, window.external_id) == ("Square", "Database upgrade",
                                                                    "statuspage:inc9")
    planned["incident"]["status"] = "completed"
    client.post(url, planned, format="json")  # finished early: same window, ended now
    window.refresh_from_db()
    assert MaintenanceWindow.objects.count() == 1
    assert window.ends_at == window.starts_at  # completed before it was due to start
    assert not Assessment.objects.exists()


def test_statuspage_refusals(client, square):
    assert client.post("/api/signals/statuspage/nope", incident(), format="json").status_code == 404
    url = f"/api/signals/statuspage/{square.webhook_token}"
    assert client.post(url, {"meta": {}}, format="json").status_code == 400
    no_id = incident(id="")
    with pytest.raises(adapters.AdapterError, match="no id"):
        adapters.statuspage(no_id, square)
    bad_schedule = incident(scheduled_for="2026-11-16T10:00:00Z", scheduled_until="soon")
    with pytest.raises(adapters.AdapterError, match="scheduled_for before"):
        adapters.statuspage(bad_schedule, square)
    with pytest.raises(adapters.AdapterError, match="component_id"):
        adapters.statuspage({"component_update": {"new_status": "operational"}}, square)


def test_existing_status_pages_get_tokens():
    page = StatusPage.objects.create(supplier="GitHub", host="githubstatus.com")
    assert len(page.webhook_token) >= 24
