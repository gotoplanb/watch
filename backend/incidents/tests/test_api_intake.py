"""Unit tests for the intake webhook (ADR-002/008/009): secret auth, idempotent
create/dedupe, and validation — hermetic via APIClient."""
import pytest
from rest_framework.test import APIClient

from incidents.models import Incident

SECRET = "test-secret"  # config.settings_test INTAKE_WEBHOOK_SECRET


@pytest.fixture
def client():
    return APIClient()


def _post(client, body, secret=SECRET):
    return client.post("/api/intake/webhook", body, format="json",
                       HTTP_X_WATCH_WEBHOOK_SECRET=secret)


@pytest.mark.django_db
def test_webhook_creates_then_dedupes(client):
    body = {"source": "sumo", "title": "disk full", "source_event_id": "e1",
            "payload": {"host": "web-1"}}
    first = _post(client, body)
    assert first.status_code == 201 and first.json()["created"] is True
    again = _post(client, {**body, "title": "retry"})
    assert again.status_code == 200 and again.json()["created"] is False
    assert Incident.objects.count() == 1


def test_webhook_rejects_bad_secret(client):
    assert _post(client, {}, secret="wrong").status_code == 401


@pytest.mark.django_db
def test_webhook_rejects_invalid_body(client):
    # Missing required `title`.
    assert _post(client, {"source": "sumo"}).status_code == 400


@pytest.mark.django_db
def test_webhook_starts_execution_on_create(client, monkeypatch):
    from incidents import escalation
    calls = {}
    monkeypatch.setattr(escalation, "start_escalation",
                        lambda inc: calls.setdefault("arn", f"local:{inc.id}"))
    body = {"source": "sumo", "title": "x", "source_event_id": "e2", "payload": {}}
    assert _post(client, body).status_code == 201
    assert "arn" in calls
