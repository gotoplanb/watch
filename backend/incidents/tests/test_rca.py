"""Unit tests for the RCA record (ADR-031 Cluster 3): a stored root-cause writeup whose document is
assembly-seeded from an incident timeline (services.rca_markdown) then hand-edited — hermetic."""
import pytest
from django.contrib.auth.models import User
from django.test import Client

from incidents import services
from incidents.models import Incident, Rca, RcaStatus, Status, Tier


@pytest.fixture
def client():
    return Client()


def _user(username="rcauser"):
    return User.objects.create(username=username)


def _incident(**kw):
    data = dict(source="sumo", payload={}, title="disk full",
                dedupe_key=f"rca-{Incident.objects.count()}", current_tier=Tier.T1)
    data.update(kw)
    return Incident.objects.create(**data)


# --- model + numbering ----------------------------------------------------

@pytest.mark.django_db
def test_rca_save_assigns_number_and_str():
    r = Rca.objects.create(title="why the pager stayed silent")
    assert r.number == "RCA-0001"
    assert r.number in str(r) and "why the pager stayed silent" in str(r)


# --- seeding --------------------------------------------------------------

@pytest.mark.django_db
def test_seed_blank_rca():
    r = services.seed_rca(title="freeform")
    assert r.number == "RCA-0001" and r.document == "" and r.title == "freeform"
    assert r.events.count() == 0  # no incident ⇒ no provenance event


@pytest.mark.django_db
def test_seed_untitled_when_no_title_and_no_incident():
    r = services.seed_rca()
    assert r.title == "Untitled RCA"


@pytest.mark.django_db
def test_seed_from_incident_fills_document_and_posts_provenance():
    inc = _incident(title="auth outage")
    services.add_note(inc, actor="alice", body="rolled back the deploy")
    r = services.seed_rca(incident=inc, actor="alice")
    assert r.title == "RCA — auth outage"
    assert r.document.startswith("# RCA — auth outage")
    assert "rolled back the deploy" in r.document  # the timeline flowed into the seed
    ev = r.events.get()
    assert ev.type == "system" and inc.number in ev.body and "by alice" in ev.body


# --- UI -------------------------------------------------------------------

@pytest.mark.django_db
def test_rca_list_requires_login(client):
    resp = client.get("/ui/rcas/")
    assert resp.status_code == 302 and "/api-auth/login/" in resp["Location"]


@pytest.mark.django_db
def test_list_renders_when_logged_in(client):
    client.force_login(_user("lister"))
    Rca.objects.create(title="visible rca")
    body = client.get("/ui/rcas/").content.decode()
    assert "RCA-0001" in body and "visible rca" in body


@pytest.mark.django_db
def test_create_with_title(client):
    client.force_login(_user())
    resp = client.post("/ui/rcas/create/", {"title": "postmortem for the cache stampede"})
    r = Rca.objects.get()
    assert resp["Location"] == f"/ui/rcas/{r.id}/" and r.number == "RCA-0001"


@pytest.mark.django_db
def test_create_seeded_from_incident(client):
    client.force_login(_user("seeder"))
    inc = _incident(title="db failover")
    resp = client.post("/ui/rcas/create/", {"title": "", "incident": str(inc.id)})
    r = Rca.objects.get()
    assert resp["Location"] == f"/ui/rcas/{r.id}/"
    assert r.title == "RCA — db failover" and r.document.startswith("# RCA — db failover")
    # detail renders the seeded document
    body = client.get(f"/ui/rcas/{r.id}/").content.decode()
    assert "db failover" in body


@pytest.mark.django_db
def test_create_blank_is_noop(client):
    client.force_login(_user())
    resp = client.post("/ui/rcas/create/", {"title": "  ", "incident": ""})
    assert resp["Location"] == "/ui/rcas/" and not Rca.objects.exists()


@pytest.mark.django_db
def test_save_document_persists(client):
    client.force_login(_user("editor"))
    r = Rca.objects.create(title="edit me")
    client.post(f"/ui/rcas/{r.id}/document/", {"document": "## Root cause\n\nit was DNS"})
    r.refresh_from_db()
    assert r.document == "## Root cause\n\nit was DNS"


@pytest.mark.django_db
def test_add_note_and_status_and_assignee(client):
    client.force_login(_user("worker"))
    target = _user("owner")
    r = Rca.objects.create(title="workflow")
    client.post(f"/ui/rcas/{r.id}/note/", {"body": "reviewed with the team"})
    assert r.events.get(type="note").body == "reviewed with the team"
    client.post(f"/ui/rcas/{r.id}/update/", {"status": RcaStatus.FINAL, "assignee": str(target.id)})
    r.refresh_from_db()
    assert r.status == RcaStatus.FINAL and r.assignee_id == target.id
    assert r.events.filter(type="system", body__contains="final").exists()
    # unassign + no status change ⇒ no new system event
    before = r.events.filter(type="system").count()
    client.post(f"/ui/rcas/{r.id}/update/", {"status": r.status, "assignee": ""})
    r.refresh_from_db()
    assert r.assignee_id is None and r.events.filter(type="system").count() == before


@pytest.mark.django_db
def test_blank_note_is_noop(client):
    client.force_login(_user("worker2"))
    r = Rca.objects.create(title="x")
    client.post(f"/ui/rcas/{r.id}/note/", {"body": "   "})
    assert r.events.count() == 0
