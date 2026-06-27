"""Unit tests for the incidents API (views/serializers/urls), hermetic via APIClient
in LOCAL_MODE. Covers authz (ADR-008), the optimistic expected_tier guard (ADR-007),
and the ack/escalate/resolve actions."""
import pytest
from django.contrib.auth.models import Group, User
from rest_framework.test import APIClient

from incidents.models import Incident, Status, Tier


@pytest.fixture
def client():
    return APIClient()


def _user(username, *tiers, **kw):
    user = User.objects.create(username=username, **kw)
    for tier in tiers:
        group, _ = Group.objects.get_or_create(name=tier)
        user.groups.add(group)
    return user


def _incident(tier=Tier.T1, **kw):
    data = dict(source="sumo", payload={}, title="t",
                dedupe_key=f"k-{tier}-{Incident.objects.count()}", current_tier=tier)
    data.update(kw)
    return Incident.objects.create(**data)


@pytest.mark.django_db
def test_list_requires_authentication(client):
    assert client.get("/api/incidents/").status_code == 403


@pytest.mark.django_db
def test_list_and_retrieve_authenticated(client):
    inc = _incident()
    client.force_authenticate(_user("viewer"))
    assert client.get("/api/incidents/").status_code == 200
    body = client.get(f"/api/incidents/{inc.id}/").json()
    assert body["id"] == str(inc.id) and body["transitions"] == []


@pytest.mark.django_db
def test_ack_then_escalate(client):
    inc = _incident(Tier.T1)
    client.force_authenticate(_user("t1", "T1"))
    ack = client.post(f"/api/incidents/{inc.id}/ack/", {"expected_tier": "T1"}, format="json")
    assert ack.status_code == 200 and ack.json()["acknowledged_at"] is not None
    esc = client.post(f"/api/incidents/{inc.id}/escalate/", {"expected_tier": "T1"}, format="json")
    assert esc.status_code == 200 and esc.json()["current_tier"] == "T2"


@pytest.mark.django_db
def test_resolve_action(client):
    inc = _incident(Tier.T1)
    client.force_authenticate(_user("t1b", "T1"))
    res = client.post(f"/api/incidents/{inc.id}/resolve/", {"expected_tier": "T1"}, format="json")
    assert res.status_code == 200 and res.json()["status"] == Status.RESOLVED


@pytest.mark.django_db
def test_lower_tier_forbidden(client):
    inc = _incident(Tier.T3)
    client.force_authenticate(_user("t1c", "T1"))
    assert client.post(f"/api/incidents/{inc.id}/ack/", {"expected_tier": "T3"},
                       format="json").status_code == 403


@pytest.mark.django_db
def test_stale_expected_tier_conflicts(client):
    inc = _incident(Tier.T2)
    client.force_authenticate(_user("t3", "T3"))  # authorized (senior), but stale tier
    assert client.post(f"/api/incidents/{inc.id}/ack/", {"expected_tier": "T1"},
                       format="json").status_code == 409


@pytest.mark.django_db
def test_action_without_expected_tier_is_allowed(client):
    inc = _incident(Tier.T1)
    client.force_authenticate(_user("t1d", "T1"))
    # expected_tier is optional; omitting it skips the optimistic check.
    assert client.post(f"/api/incidents/{inc.id}/ack/", {}, format="json").status_code == 200


@pytest.mark.django_db
def test_escalate_real_mode_branch_does_not_call_services(client, settings, monkeypatch):
    # With LOCAL_MODE off, the view only SendTaskSuccess (mocked) and leaves state to
    # the commit Lambda — so the tier is unchanged in the response here.
    settings.ESCALATION_LOCAL_MODE = False
    from incidents import escalation
    monkeypatch.setattr(escalation, "send_outcome", lambda *a, **k: None)
    inc = _incident(Tier.T1)
    client.force_authenticate(_user("t1e", "T1"))
    res = client.post(f"/api/incidents/{inc.id}/escalate/", {"expected_tier": "T1"}, format="json")
    assert res.status_code == 200 and res.json()["current_tier"] == "T1"
