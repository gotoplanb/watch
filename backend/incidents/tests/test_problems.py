"""Unit tests for the Problem record (ADR-031 Cluster 2): thin ops-record with human numbering,
generic timeline (work notes + status events), and the server-rendered /ui surface — hermetic."""
import pytest
from django.contrib.auth.models import User
from django.test import Client

from incidents import numbering, services
from incidents.models import Problem, ProblemStatus, RecordCounter


@pytest.fixture
def client():
    return Client()


def _user(username="prbuser"):
    return User.objects.create(username=username)


# --- numbering ------------------------------------------------------------

@pytest.mark.django_db
def test_next_number_is_monotonic_per_prefix():
    assert numbering.next_number("PRB") == "PRB-0001"
    assert numbering.next_number("PRB") == "PRB-0002"
    # a different prefix has its own independent counter
    assert numbering.next_number("RCA") == "RCA-0001"
    assert RecordCounter.objects.get(prefix="PRB").value == 2


@pytest.mark.django_db
def test_str_reprs():
    p = Problem.objects.create(title="pool exhaustion")
    assert p.number in str(p) and "pool exhaustion" in str(p)
    assert str(RecordCounter.objects.get(prefix="PRB")) == "PRB=1"


@pytest.mark.django_db
def test_problem_save_assigns_number():
    p = Problem.objects.create(title="db connection pool exhaustion")
    assert p.number == "PRB-0001"
    # an explicit number is preserved (no clobber)
    p2 = Problem(title="preset", number="PRB-0099")
    p2.save()
    assert p2.number == "PRB-0099"


# --- timeline is shared (generic) -----------------------------------------

@pytest.mark.django_db
def test_problem_timeline_records_notes_and_status_events():
    p = Problem.objects.create(title="recurring 500s")
    services.add_note(p, actor="alice", body="looks like a bad deploy")
    services.post_system_event(p, "Status open → investigating by alice")
    tl = services.timeline(p)
    assert [i["kind"] for i in tl] == ["event", "event"]
    assert p.events.count() == 2


# --- UI --------------------------------------------------------------------

@pytest.mark.django_db
def test_problem_list_requires_login(client):
    resp = client.get("/ui/problems/")
    assert resp.status_code == 302 and "/api-auth/login/" in resp["Location"]


@pytest.mark.django_db
def test_list_renders_problems_when_logged_in(client):
    client.force_login(_user("lister"))
    Problem.objects.create(title="visible on the list")
    body = client.get("/ui/problems/").content.decode()
    assert "PRB-0001" in body and "visible on the list" in body


@pytest.mark.django_db
def test_create_then_detail(client):
    client.force_login(_user())
    resp = client.post("/ui/problems/create/", {"title": "flapping health check", "description": "since 3pm"})
    assert resp.status_code == 302
    p = Problem.objects.get()
    assert p.number == "PRB-0001" and p.description == "since 3pm"
    assert resp["Location"] == f"/ui/problems/{p.id}/"
    body = client.get(f"/ui/problems/{p.id}/").content.decode()
    assert "PRB-0001" in body and "flapping health check" in body


@pytest.mark.django_db
def test_create_blank_title_is_noop(client):
    client.force_login(_user())
    resp = client.post("/ui/problems/create/", {"title": "   "})
    assert resp.status_code == 302 and resp["Location"] == "/ui/problems/"
    assert not Problem.objects.exists()


@pytest.mark.django_db
def test_add_note_appears_on_timeline(client):
    client.force_login(_user("noter"))
    p = Problem.objects.create(title="cache stampede")
    client.post(f"/ui/problems/{p.id}/note/", {"body": "added a jitter"})
    ev = p.events.get()
    assert ev.type == "note" and ev.actor == "noter" and ev.body == "added a jitter"


@pytest.mark.django_db
def test_blank_note_is_noop(client):
    client.force_login(_user("noter2"))
    p = Problem.objects.create(title="x")
    client.post(f"/ui/problems/{p.id}/note/", {"body": "  "})
    assert p.events.count() == 0


@pytest.mark.django_db
def test_update_status_posts_system_event(client):
    client.force_login(_user("mover"))
    p = Problem.objects.create(title="noisy alert")
    client.post(f"/ui/problems/{p.id}/update/", {"status": ProblemStatus.INVESTIGATING})
    p.refresh_from_db()
    assert p.status == ProblemStatus.INVESTIGATING
    ev = p.events.get()
    assert ev.type == "system" and "investigating" in ev.body


@pytest.mark.django_db
def test_update_assignee_and_unassign(client):
    client.force_login(_user("assigner"))
    target = _user("owner")
    p = Problem.objects.create(title="assign me")
    client.post(f"/ui/problems/{p.id}/update/", {"status": p.status, "assignee": str(target.id)})
    p.refresh_from_db()
    assert p.assignee_id == target.id
    # no status change ⇒ no system event
    assert p.events.count() == 0
    client.post(f"/ui/problems/{p.id}/update/", {"status": p.status, "assignee": ""})
    p.refresh_from_db()
    assert p.assignee_id is None


@pytest.mark.django_db
def test_update_with_no_changes_is_noop(client):
    client.force_login(_user("nochange"))
    p = Problem.objects.create(title="steady")
    # same status, still-unassigned ⇒ nothing to save, no events
    client.post(f"/ui/problems/{p.id}/update/", {"status": p.status, "assignee": ""})
    p.refresh_from_db()
    assert p.status == ProblemStatus.OPEN and p.events.count() == 0
