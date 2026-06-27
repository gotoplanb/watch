"""Unit tests for the server-rendered incident UI (ADR-011): rendering, HTMX partials,
auth/authz, comments — hermetic via the Django test client in LOCAL_MODE."""
import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from incidents.models import Comment, Incident, Status, Tier


@pytest.fixture
def client():
    return Client()


def _user(username, *tiers):
    user = User.objects.create(username=username)
    for tier in tiers:
        user.groups.add(Group.objects.get_or_create(name=tier)[0])
    return user


def _incident(tier=Tier.T1, **kw):
    data = dict(source="sumo", payload={}, title="disk full",
                dedupe_key=f"ui-{tier}-{Incident.objects.count()}", current_tier=tier)
    data.update(kw)
    return Incident.objects.create(**data)


@pytest.mark.django_db
def test_list_redirects_anonymous_to_login(client):
    resp = client.get("/ui/incidents/")
    assert resp.status_code == 302 and "/api-auth/login/" in resp["Location"]


@pytest.mark.django_db
def test_list_renders_for_authenticated(client):
    _incident()
    client.force_login(_user("viewer"))
    resp = client.get("/ui/incidents/")
    assert resp.status_code == 200 and b"disk full" in resp.content


@pytest.mark.django_db
def test_list_htmx_returns_rows_partial_with_filters(client):
    _incident()
    client.force_login(_user("viewer2"))
    resp = client.get("/ui/incidents/?status=OPEN&tier=T1", HTTP_HX_REQUEST="true")
    assert resp.status_code == 200
    assert b'id="incident-rows"' in resp.content and b"<html" not in resp.content


@pytest.mark.django_db
def test_detail_renders_body(client):
    inc = _incident()
    client.force_login(_user("viewer3"))
    resp = client.get(f"/ui/incidents/{inc.id}/")
    assert resp.status_code == 200 and b'id="incident-body"' in resp.content


@pytest.mark.django_db
def test_add_comment_appends_to_timeline(client):
    inc = _incident()
    client.force_login(_user("commenter"))
    resp = client.post(f"/ui/incidents/{inc.id}/comment/", {"body": "looking into it"})
    assert resp.status_code == 200 and b"looking into it" in resp.content
    assert Comment.objects.filter(incident=inc).count() == 1


@pytest.mark.django_db
def test_empty_comment_is_noop(client):
    inc = _incident()
    client.force_login(_user("commenter2"))
    resp = client.post(f"/ui/incidents/{inc.id}/comment/", {"body": "   "})
    assert resp.status_code == 200 and Comment.objects.filter(incident=inc).count() == 0


@pytest.mark.django_db
def test_ack_escalate_resolve_authorized(client):
    inc = _incident(Tier.T1)
    client.force_login(_user("t3", "T3"))  # senior — can act at any tier
    assert client.post(f"/ui/incidents/{inc.id}/ack/").status_code == 200
    inc.refresh_from_db()
    assert inc.acknowledged_at is not None
    client.post(f"/ui/incidents/{inc.id}/escalate/")
    inc.refresh_from_db()
    assert inc.current_tier == Tier.T2
    client.post(f"/ui/incidents/{inc.id}/resolve/")
    inc.refresh_from_db()
    assert inc.status == Status.RESOLVED


@pytest.mark.django_db
def test_action_forbidden_for_lower_tier(client):
    inc = _incident(Tier.T3)
    client.force_login(_user("t1", "T1"))
    assert client.post(f"/ui/incidents/{inc.id}/ack/").status_code == 403


@pytest.mark.django_db
def test_escalate_real_mode_leaves_state_to_lambda(client, settings, monkeypatch):
    settings.ESCALATION_LOCAL_MODE = False
    from incidents import escalation
    monkeypatch.setattr(escalation, "send_outcome", lambda *a, **k: None)
    inc = _incident(Tier.T1)
    client.force_login(_user("t1b", "T1"))
    resp = client.post(f"/ui/incidents/{inc.id}/escalate/")
    inc.refresh_from_db()
    assert resp.status_code == 200 and inc.current_tier == Tier.T1  # unchanged; commit Lambda owns it


@pytest.mark.django_db
def test_comment_str():
    inc = _incident()
    user = _user("author1")
    assert "comment by author1" in str(Comment.objects.create(incident=inc, author=user, body="x"))
    assert "unknown" in str(Comment.objects.create(incident=inc, author=None, body="y"))
