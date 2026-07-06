"""Unit tests for the server-rendered incident UI (ADR-011/021): rendering, HTMX partials,
auth/authz, timeline notes + annotations + RCA — hermetic via the Django test client in LOCAL_MODE."""
import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from incidents import services
from incidents.models import Annotation, AnnotationTag, Incident, Status, TimelineEvent, Tier


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
def test_settings_requires_login(client):
    resp = client.get("/ui/settings/")
    assert resp.status_code == 302 and "/api-auth/login/" in resp["Location"]


@pytest.mark.django_db
def test_settings_shows_own_api_key(client, settings):
    from incidents import apikeys
    settings.API_KEY_SECRET = "unit-key-secret"
    user = _user("keyholder")
    client.force_login(user)
    body = client.get("/ui/settings/").content.decode()
    assert apikeys.api_key_for(user) in body and "Authorization: Bearer" in body


@pytest.mark.django_db
def test_rotate_keys_rolls_the_shown_key(client, settings):
    from incidents import apikeys
    settings.API_KEY_SECRET = "unit-key-secret"
    user = _user("rotator")
    client.force_login(user)
    before = apikeys.api_key_for(user)
    assert client.post("/ui/settings/rotate-keys/").status_code == 302  # back to settings
    after = apikeys.api_key_for(user)
    assert after != before
    body = client.get("/ui/settings/").content.decode()
    assert after in body and before not in body


@pytest.mark.django_db
def test_rotate_keys_requires_login(client):
    resp = client.post("/ui/settings/rotate-keys/")
    assert resp.status_code == 302 and "/api-auth/login/" in resp["Location"]


@pytest.mark.django_db
def test_settings_shows_own_paging_topic(client, settings):
    settings.PAGING_ENV = "test"
    settings.NTFY_TOPIC_SECRET = "s3kret"
    user = _user("t2a", "T2")
    client.force_login(user)
    resp = client.get("/ui/settings/")
    assert resp.status_code == 200
    body = resp.content.decode()
    from incidents import apikeys
    own = services.paging_topic("user", user.id, seed=apikeys.seed_for(user))
    assert own in body and own.startswith(f"watch-test-user-{user.id}-")
    # tier fallback topic for the user's tier is shown; the raw secret never is
    assert services.paging_topic("tier", "T2") in body
    assert "s3kret" not in body


@pytest.mark.django_db
def test_settings_does_not_leak_other_users_topics(client, settings):
    settings.NTFY_TOPIC_SECRET = "s3kret"
    other = _user("t3a", "T3")
    me = _user("t1a", "T1")
    client.force_login(me)
    body = client.get("/ui/settings/").content.decode()
    from incidents import apikeys
    assert services.paging_topic("user", me.id, seed=apikeys.seed_for(me)) in body
    assert services.paging_topic("user", other.id, seed=apikeys.seed_for(other)) not in body
    assert services.paging_topic("tier", "T3") not in body  # not my tier


@pytest.mark.django_db
def test_settings_warns_when_secret_unset(client, settings):
    settings.NTFY_TOPIC_SECRET = ""
    client.force_login(_user("plain"))
    body = client.get("/ui/settings/").content.decode()
    assert "not salted" in body.lower()


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
def test_add_note_appends_to_timeline(client):
    inc = _incident()
    client.force_login(_user("commenter"))
    resp = client.post(f"/ui/incidents/{inc.id}/note/", {"body": "looking into it"})
    assert resp.status_code == 200 and b"looking into it" in resp.content
    ev = TimelineEvent.objects.get(incident=inc)
    assert ev.type == "note" and ev.actor == "commenter"


@pytest.mark.django_db
def test_empty_note_is_noop(client):
    inc = _incident()
    client.force_login(_user("commenter2"))
    resp = client.post(f"/ui/incidents/{inc.id}/note/", {"body": "   "})
    assert resp.status_code == 200 and TimelineEvent.objects.filter(incident=inc).count() == 0


@pytest.mark.django_db
def test_annotate_transition_and_event_with_tag(client):
    inc = _incident(Tier.T1)
    client.force_login(_user("t3", "T3"))
    client.post(f"/ui/incidents/{inc.id}/escalate/")  # creates a Transition + a system event
    t = inc.transitions.first()
    ev = services.add_note(inc, actor="t3", body="note")
    # annotate the authoritative Transition, and the note event
    r1 = client.post(f"/ui/incidents/{inc.id}/annotate/",
                     {"target": f"transition:{t.id}", "tag": "unexpected", "body": "should not have fired"})
    r2 = client.post(f"/ui/incidents/{inc.id}/annotate/",
                     {"target": f"event:{ev.id}", "tag": "root-cause", "body": "here"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert t.annotations.count() == 1 and t.annotations.first().tag == "unexpected"
    assert ev.annotations.count() == 1 and ev.annotations.first().tag == "root-cause"
    assert b"should not have fired" in r2.content


@pytest.mark.django_db
def test_annotate_invalid_target_is_noop(client):
    inc = _incident()
    client.force_login(_user("annot"))
    for bad in ["transition:999999", "event:nope", "bogus:1", ""]:
        resp = client.post(f"/ui/incidents/{inc.id}/annotate/", {"target": bad, "tag": "note", "body": "x"})
        assert resp.status_code == 200
    assert Annotation.objects.count() == 0


@pytest.mark.django_db
def test_annotate_rejects_bad_tag(client):
    inc = _incident(Tier.T1)
    client.force_login(_user("t3b", "T3"))
    ev = services.add_note(inc, actor="t3b", body="n")
    client.post(f"/ui/incidents/{inc.id}/annotate/", {"target": f"event:{ev.id}", "tag": "nonsense", "body": "x"})
    assert ev.annotations.count() == 0


@pytest.mark.django_db
def test_rca_export_downloads_markdown(client):
    inc = _incident(Tier.T1, title="disk full")
    client.force_login(_user("t3c", "T3"))
    client.post(f"/ui/incidents/{inc.id}/escalate/")
    ev = services.add_note(inc, actor="t3c", body="tail of the log")
    services.annotate_event(ev, author=None, body="root here", tag=AnnotationTag.ROOT_CAUSE)
    resp = client.get(f"/ui/incidents/{inc.id}/rca.md")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/markdown")
    assert f"rca-{inc.id}.md" in resp["Content-Disposition"]
    body = resp.content.decode()
    assert "# RCA — disk full" in body
    assert "escalated T1→T2" in body and "tail of the log" in body
    assert "root-cause" in body and "root here" in body


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
def test_escalate_emits_system_event():
    inc = _incident(Tier.T1)
    services.escalate(inc.id, actor="7")
    sys_ev = TimelineEvent.objects.get(incident=inc, type="system")
    assert "Escalated T1→T2" in sys_ev.body and sys_ev.data["to_tier"] == "T2"


@pytest.mark.django_db
def test_auto_escalate_narrative_and_ai_post():
    inc = _incident(Tier.T1)
    from incidents.models import Transition
    services.escalate(inc.id, actor=Transition.SYSTEM_ACTOR)
    sys_ev = TimelineEvent.objects.get(incident=inc, type="system")
    assert "Auto-escalated (SLA breach)" in sys_ev.body and sys_ev.data["auto"] is True
    ai = services.post_ai_event(inc, body="likely OOM in worker", data={"confidence": 0.7})
    assert ai.type == "ai" and ai.actor == "argus"


@pytest.mark.django_db
def test_str_reprs():
    inc = _incident(Tier.T1)
    user = _user("author1")
    ev = TimelineEvent.objects.create(incident=inc, type="note", actor="author1", body="x")
    assert "note by author1" in str(ev)
    a = services.annotate_event(ev, author=user, body="y", tag=AnnotationTag.UNEXPECTED)
    assert "unexpected" in str(a) and "author1" in str(a)
    assert "unknown" in str(services.annotate_event(ev, author=None, body="z"))


@pytest.mark.django_db
def test_rca_no_events_section():
    inc = _incident(Tier.T1)
    md = services.rca_markdown(inc)
    assert "_(no events)_" in md and "_(none flagged)_" in md
