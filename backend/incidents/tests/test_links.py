"""Unit tests for generic record links (ADR-031 Cluster 4): the RecordLink table + services +
the shared /ui linking block. Links cross record types (incident/problem/rca) — hermetic."""
import pytest
from django.contrib.auth.models import User
from django.test import Client

from incidents import services
from incidents.models import Incident, LinkKind, Problem, Rca, RecordLink, Tier


@pytest.fixture
def client():
    return Client()


def _user(username="linker"):
    return User.objects.create(username=username)


def _incident(**kw):
    data = dict(source="sumo", payload={}, title="disk full",
                dedupe_key=f"lnk-{Incident.objects.count()}", current_tier=Tier.T1)
    data.update(kw)
    return Incident.objects.create(**data)


# --- number resolution ----------------------------------------------------

@pytest.mark.django_db
def test_record_for_number_resolves_each_type():
    inc, prb, rca = _incident(), Problem.objects.create(title="p"), Rca.objects.create(title="r")
    assert services.record_for_number(inc.number) == inc
    assert services.record_for_number(prb.number.lower()) == prb  # case-insensitive
    assert services.record_for_number(rca.number) == rca


@pytest.mark.django_db
def test_record_for_number_bad_input():
    assert services.record_for_number("") is None
    assert services.record_for_number("XYZ-0001") is None   # unknown prefix
    assert services.record_for_number("INC-9999") is None   # no such record


# --- linking --------------------------------------------------------------

@pytest.mark.django_db
def test_link_records_creates_and_narrates_both_sides():
    inc, prb = _incident(), Problem.objects.create(title="root cause")
    link, created = services.link_records(inc, prb, kind=LinkKind.CAUSED_BY, actor="alice")
    assert created and link is not None
    assert inc.events.filter(type="system", body__contains="caused by").exists()
    assert prb.events.filter(type="system").exists()
    # idempotent on the exact tuple
    link2, created2 = services.link_records(inc, prb, kind=LinkKind.CAUSED_BY, actor="alice")
    assert not created2 and link2.id == link.id
    assert RecordLink.objects.count() == 1


@pytest.mark.django_db
def test_link_refuses_self_and_defaults_bad_kind():
    inc = _incident()
    link, created = services.link_records(inc, inc, kind=LinkKind.RELATES_TO)
    assert link is None and not created
    prb = Problem.objects.create(title="p")
    link, _ = services.link_records(inc, prb, kind="not-a-kind")
    assert link.kind == LinkKind.RELATES_TO


@pytest.mark.django_db
def test_links_for_reports_direction():
    inc, prb = _incident(), Problem.objects.create(title="p")
    services.link_records(inc, prb, kind=LinkKind.RELATES_TO)
    out = services.links_for(inc)
    assert len(out) == 1 and out[0]["direction"] == "out" and out[0]["other"] == prb
    inn = services.links_for(prb)
    assert len(inn) == 1 and inn[0]["direction"] == "in" and inn[0]["other_label"] == inc.number


@pytest.mark.django_db
def test_unlink():
    inc, prb = _incident(), Problem.objects.create(title="p")
    link, _ = services.link_records(inc, prb, kind=LinkKind.BLOCKS)
    assert services.unlink(link.id) is True
    assert services.unlink(link.id) is False  # already gone
    assert RecordLink.objects.count() == 0


# --- UI -------------------------------------------------------------------

@pytest.mark.django_db
def test_link_add_via_ui_then_remove(client):
    client.force_login(_user())
    inc, rca = _incident(), Rca.objects.create(title="writeup")
    resp = client.post("/ui/links/add/", {
        "from_number": inc.number, "to_number": rca.number, "kind": LinkKind.RELATES_TO,
    })
    assert resp["Location"] == f"/ui/incidents/{inc.id}/"
    link = RecordLink.objects.get()
    # the source detail page renders the link block
    body = client.get(f"/ui/incidents/{inc.id}/").content.decode()
    assert rca.number in body and "relates to" in body
    resp = client.post(f"/ui/links/{link.id}/remove/", {"from_number": inc.number})
    assert resp["Location"] == f"/ui/incidents/{inc.id}/" and RecordLink.objects.count() == 0


@pytest.mark.django_db
def test_link_add_bad_target_is_noop(client):
    client.force_login(_user())
    inc = _incident()
    resp = client.post("/ui/links/add/", {"from_number": inc.number, "to_number": "PRB-9999", "kind": "relates_to"})
    assert resp["Location"] == f"/ui/incidents/{inc.id}/" and not RecordLink.objects.exists()


@pytest.mark.django_db
def test_link_add_bad_source_falls_back_to_list(client):
    client.force_login(_user())
    resp = client.post("/ui/links/add/", {"from_number": "INC-9999", "to_number": "INC-9999", "kind": "relates_to"})
    assert resp["Location"] == "/ui/incidents/"


@pytest.mark.django_db
def test_problem_and_rca_detail_render_link_block(client):
    client.force_login(_user())
    prb = Problem.objects.create(title="p")
    rca = Rca.objects.create(title="r")
    assert "Add link" in client.get(f"/ui/problems/{prb.id}/").content.decode()
    assert "Add link" in client.get(f"/ui/rcas/{rca.id}/").content.decode()


@pytest.mark.django_db
def test_link_add_redirects_to_problem_and_rca_sources(client):
    client.force_login(_user())
    prb, rca, inc = Problem.objects.create(title="p"), Rca.objects.create(title="r"), _incident()
    resp = client.post("/ui/links/add/", {"from_number": prb.number, "to_number": inc.number, "kind": "relates_to"})
    assert resp["Location"] == f"/ui/problems/{prb.id}/"
    resp = client.post("/ui/links/add/", {"from_number": rca.number, "to_number": inc.number, "kind": "relates_to"})
    assert resp["Location"] == f"/ui/rcas/{rca.id}/"


@pytest.mark.django_db
def test_link_remove_bad_source_falls_back_to_list(client):
    client.force_login(_user())
    inc, prb = _incident(), Problem.objects.create(title="p")
    link, _ = services.link_records(inc, prb, kind=LinkKind.RELATES_TO)
    resp = client.post(f"/ui/links/{link.id}/remove/", {"from_number": "INC-9999"})
    assert resp["Location"] == "/ui/incidents/"


@pytest.mark.django_db
def test_recordlink_str():
    inc, prb = _incident(), Problem.objects.create(title="p")
    link, _ = services.link_records(inc, prb, kind=LinkKind.CAUSED_BY)
    assert "caused_by" in str(link)
