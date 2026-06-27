"""Unit tests for the public /api/status posture endpoint (ADR-011)."""
import pytest
from rest_framework.test import APIClient

from incidents import services
from incidents.models import Incident, Tier


@pytest.fixture
def client():
    return APIClient()


def _incident(tier=Tier.T1, **kw):
    data = dict(source="sumo", payload={}, title="t",
                dedupe_key=f"st-{tier}-{Incident.objects.count()}", current_tier=tier)
    data.update(kw)
    return Incident.objects.create(**data)


@pytest.mark.django_db
def test_status_ok_shape_and_cors(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert set(body["incidents"]) == {"open", "by_tier", "resolved_24h"}
    assert resp["Access-Control-Allow-Origin"] == "*"


@pytest.mark.django_db
def test_status_counts_open_by_tier(client):
    _incident(Tier.T1)
    _incident(Tier.T1)
    _incident(Tier.T2)
    resolved = _incident(Tier.T1)
    services.resolve(resolved.id, actor="1")  # excluded from open counts
    body = client.get("/api/status").json()
    assert body["incidents"]["by_tier"] == {"T1": 2, "T2": 1, "T3": 0}
    assert body["incidents"]["open"] == 3
    assert body["incidents"]["resolved_24h"] == 1


@pytest.mark.django_db
def test_status_degraded_when_dependency_down(client, monkeypatch):
    from incidents import health
    monkeypatch.setattr(health, "dependency_checks",
                        lambda: {"postgres": True, "valkey": False})
    resp = client.get("/api/status")
    assert resp.status_code == 503 and resp.json()["status"] == "degraded"
