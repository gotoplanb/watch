"""Unit tests for the dependency-checked health endpoint (ADR-005)."""
import pytest
from rest_framework.test import APIClient


@pytest.fixture
def client():
    return APIClient()


@pytest.mark.django_db
def test_health_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {"postgres": True, "valkey": True}}


@pytest.mark.django_db
def test_health_degraded_when_postgres_down(client, monkeypatch):
    from incidents import health

    class _BrokenConn:
        def cursor(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(health, "connection", _BrokenConn())
    resp = client.get("/api/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["checks"]["postgres"] is False


@pytest.mark.django_db
def test_health_degraded_when_valkey_down(client, monkeypatch):
    from incidents import health

    class _BrokenCache:
        def set(self, *a, **k):
            raise RuntimeError("cache down")

        def get(self, *a, **k):
            return None

    monkeypatch.setattr(health, "cache", _BrokenCache())
    resp = client.get("/api/health")
    assert resp.status_code == 503 and resp.json()["checks"]["valkey"] is False
