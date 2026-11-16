"""The assessment workspace over the API (ADR-056): the UI's forms and handlers behind /api/, so the
`wt` CLI drives the same validation, permissions and history as the page."""
import pytest
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from incidents import assessments, communications
from incidents.models import DiagnosticRule, OutboxMessage, RemediationTask, SubjectArea
from incidents.operations_api import prefixed
from incidents.tests.test_operations import promote

pytestmark = pytest.mark.django_db
PNG = b"\x89PNG\r\n\x1a\nexample"


def _user(username, *tiers):
    user = User.objects.create_user(username)
    for tier in tiers:
        user.groups.add(Group.objects.get_or_create(name=tier)[0])
    return user


def _as(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def t1():
    return _user("t1a", "T1")


@pytest.fixture
def row():
    return assessments.receive(title="Example checkout concern", source="monitor",
                               signal="Synthetic checkout failures.", customer="Example Customer")[0]


def _act(client, row, action, data=None, fmt="json"):
    return client.post(f"/api/assessments/{row.number}/actions/{action}/", data or {}, format=fmt)


# --- create + history --------------------------------------------------------------------------

def test_responder_creates_an_owned_assessment(t1):
    reply = _as(t1).post("/api/assessments/", {"title": "Checkout errors", "signal": "5xx up"},
                         format="json")
    assert reply.status_code == 201
    created = assessments.Assessment.objects.get(number=reply.json()["number"])
    assert (created.assignee, created.source) == (t1, "operator")


def test_create_needs_a_tier_and_valid_fields(t1):
    assert _as(_user("nobody")).post("/api/assessments/", {}, format="json").status_code == 403
    bad = _as(t1).post("/api/assessments/", {"title": "x"}, format="json")
    assert bad.status_code == 400
    assert "signal" in bad.json()["errors"]


def test_history_carries_actor_names(row, t1):
    _act(_as(t1), row, "comment", {"body": "looking"})
    history = _as(t1).get(f"/api/assessments/{row.number}/history/").json()
    assert history[-1]["kind"] == "comment"
    assert history[-1]["actor_name"] == "t1a"


# --- workspace actions -------------------------------------------------------------------------

def test_action_returns_only_the_new_events(row, t1):
    reply = _act(_as(t1), row, "evidence", {"summary": "5xx at 4%", "result": "4% for 10m",
                                            "source": "prometheus"})
    assert reply.status_code == 200
    body = reply.json()
    assert [e["kind"] for e in body["events"]] == ["evidence_added"]
    assert (body["assessment"], body["incident"]) == (row.number, None)


def test_unknown_forbidden_and_invalid_actions(row, t1):
    assert _act(_as(t1), row, "nonsense").status_code == 400
    assert _act(_as(_user("nobody")), row, "claim", {"reason": "x"}).status_code == 403
    invalid = _act(_as(t1), row, "conclusion", {"verdict": "sideways", "confidence": "high",
                                               "reason": "x"})
    assert invalid.status_code == 400
    assert "verdict" in invalid.json()["errors"]


def test_domain_refusal_maps_to_400(row, t1):
    reply = _act(_as(t1), row, "promote", {"reason": "no evidence yet"})
    assert reply.status_code == 400
    assert "Promotion needs evidence" in reply.json()["detail"]


def test_conflict_maps_to_409(row, t1, monkeypatch):
    def conflict(*args, **kwargs):
        raise assessments.AssessmentConflict("moved on")

    monkeypatch.setitem(operations_ui_actions(), "claim", conflict)
    assert _act(_as(t1), row, "claim", {"reason": "x"}).status_code == 409


def operations_ui_actions():
    from incidents.operations_ui import WORKSPACE_ACTIONS
    return WORKSPACE_ACTIONS


def test_note_with_photos_over_multipart(row, t1):
    reply = _act(_as(t1), row, "comment",
                 {"body": "dashboard", "photos": [SimpleUploadedFile("a.png", PNG)]}, fmt="multipart")
    assert reply.status_code == 200
    assert len(reply.json()["events"][0]["data"]["attachments"]) == 1


def test_promote_then_engage_by_username(row, t1, django_capture_on_commit_callbacks):
    t3 = _user("t3a", "T3")
    with django_capture_on_commit_callbacks(execute=True):
        promote(row, t1)
    reply = _act(_as(t1), row, "engage", {"target": "t3a", "reason": "need help"})
    assert reply.status_code == 200, reply.json()
    engagements = _as(t1).get(f"/api/assessments/{row.number}/engagements/").json()
    assert engagements[0]["target"] == "t3a"
    gid = engagements[0]["id"]
    assert _as(t1).post(f"/api/engagements/{gid}/respond/", {"action": "ack"},
                        format="json").status_code == 403
    acked = _as(t3).post(f"/api/engagements/{gid}/respond/", {"action": "ack"}, format="json")
    assert acked.json()["state"] == "acknowledged"


def test_prefixed_maps_users_bools_and_lists():
    data, _ = prefixed("engage", {"target": "ghost", "selected": [1, 2], "flag": True, "skip": None})
    assert data.getlist("engage-selected") == ["1", "2"]
    assert data["engage-target"] == "ghost"  # unknown names are left for the form to reject
    assert data["engage-flag"] == "true"
    assert "engage-skip" in data
    assert data.getlist("engage-skip") == []


# --- communications ----------------------------------------------------------------------------

@pytest.fixture
def mail(row):
    return communications.draft(row.pk, actor="t1a", subject="Update", body="Investigating.",
                                recipients=["ops@example.test"])


def test_communication_edit_send_and_stale_version(row, t1, mail):
    client = _as(t1)
    listed = client.get(f"/api/assessments/{row.number}/communications/").json()
    assert listed[0]["id"] == str(mail.pk)
    edited = client.post(f"/api/communications/{mail.pk}/edit/", {
        "subject": "Update 2", "body": "Rolled back.", "recipients": "ops@example.test",
        "auto_send": False, "expected_version": 1}, format="json")
    assert edited.json()["revision"] == 2
    assert len(edited.json()["revisions"]) == 2
    stale = client.post(f"/api/communications/{mail.pk}/send/", {"expected_version": 1},
                        format="json")
    assert stale.status_code == 409
    sent = client.post(f"/api/communications/{mail.pk}/send/", {"expected_version": 2},
                       format="json")
    assert sent.json()["state"] == "sent"
    assert client.get("/api/outbox/").json()[0]["subject"] == "Update 2"
    assert OutboxMessage.objects.count() == 1


def test_communication_suppress_and_refusals(t1, mail):
    client = _as(t1)
    assert client.get(f"/api/communications/{mail.pk}/").json()["body"] == "Investigating."
    assert client.post(f"/api/communications/{mail.pk}/bogus/", {}, format="json").status_code == 400
    assert _as(_user("nobody")).post(f"/api/communications/{mail.pk}/send/", {},
                                     format="json").status_code == 403
    bad_edit = client.post(f"/api/communications/{mail.pk}/edit/", {"expected_version": 1},
                           format="json")
    assert bad_edit.status_code == 400
    assert "subject" in bad_edit.json()["errors"]
    done = client.post(f"/api/communications/{mail.pk}/suppress/",
                       {"expected_version": 1, "reason": "duplicate"}, format="json")
    assert done.json()["state"] == "suppressed"


# --- RCA + remediation --------------------------------------------------------------------------

def test_rca_curation_and_remediation(row, t1, django_capture_on_commit_callbacks):
    t2 = _user("t2a", "T2")
    with django_capture_on_commit_callbacks(execute=True):
        promote(row, t1)
    client = _as(t1)
    assert client.get(f"/api/assessments/{row.number}/rca/").json()["state"] is None
    event = row.history.get(kind="evidence_added")
    assert _act(client, row, "curate", {"event": event.pk, "section": "root_cause"}).status_code == 200
    assert _act(client, row, "rca", {"summary": "s", "root_cause": "r",
                                     "state": "draft"}).status_code == 200
    rca = client.get(f"/api/assessments/{row.number}/rca/").json()
    assert rca["selections"] == [{"event": event.pk, "section": "root_cause", "note": ""}]
    assert "# RCA" in rca["markdown"]
    assert _act(client, row, "remediation", {"title": "Canary", "owner": "t2a",
                                             "due_date": "2026-12-01"}).status_code == 200
    task = client.get(f"/api/assessments/{row.number}/remediations/").json()[0]
    assert task["owner"] == "t2a"
    done = _as(t2).post(f"/api/remediations/{task['id']}/",
                        {"state": "done", "verification": "verified"}, format="json")
    assert done.json()["state"] == "done"
    assert RemediationTask.objects.get().verification == "verified"


def test_remediation_refusal(row, t1, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        promote(row, t1)
    _act(_as(t1), row, "remediation", {"title": "x", "owner": "t1a", "due_date": "2026-12-01"})
    task = RemediationTask.objects.get()
    reply = _as(t1).post(f"/api/remediations/{task.pk}/", {"state": "nonsense"}, format="json")
    assert reply.status_code == 400


# --- reference data + reports -------------------------------------------------------------------

def test_reference_lists_and_reports(t1):
    DiagnosticRule.objects.create(name="Errors", provider="fixture", query="q", comparator="above",
                                  threshold=2)
    area = SubjectArea.objects.create(name="Payments")
    area.members.add(t1)
    client = _as(t1)
    assert client.get("/api/diagnostic-rules/").json()[0]["name"] == "Errors"
    assert client.get("/api/subject-areas/").json()[0]["members"] == ["t1a"]
    assert client.get("/api/reports/?days=7").json()["days"] == 7
    assert client.get("/api/reports/?days=soon").status_code == 400
    assert client.get("/api/outbox/").json() == []


def test_workspace_api_requires_authentication(row):
    assert APIClient().get(f"/api/assessments/{row.number}/rca/").status_code == 403


def test_email_defaults_to_the_drafted_text(row, t1):
    reply = _act(_as(t1), row, "email", {"recipients": "ops@example.test"})
    assert reply.status_code == 200, reply.json()
    subject, _ = communications.draft_text(row)
    assert _as(t1).get(f"/api/assessments/{row.number}/communications/").json()[0]["subject"] == \
        subject
