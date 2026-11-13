from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient

from incidents import assessments, services
from incidents.models import Assessment, AssessmentEvent, Incident

pytestmark = pytest.mark.django_db


@pytest.fixture
def responder():
    user = User.objects.create_user("responder", password="test-password")
    user.groups.add(Group.objects.get_or_create(name="T2")[0])
    return user


@pytest.fixture
def assessment(responder):
    record, _ = assessments.receive(
        title="Checkout concern", source="customer", signal="A customer reports failures.",
        customer="Example Customer", service="Checkout", supplier="Supplier A",
        source_event_id="signal-1", actor=responder.username, assignee=responder,
        signal_at=timezone.now() - timedelta(hours=2),
    )
    return record


def evidence(record):
    return assessments.add_evidence(record.pk, actor="responder", summary="Journey failures confirmed",
                                    query="compare successful requests in matching windows",
                                    result="Checkout failures 6%; baseline 0.2%; sample window 15 minutes.")


def conclusion(record, verdict="degraded"):
    return assessments.conclude(record.pk, actor="responder", verdict=verdict, confidence="high",
                                reason="The recorded checks support this conclusion.")


def test_dedupe_is_source_scoped_and_survives_closure(assessment):
    evidence(assessment)
    conclusion(assessment, "no_issue")
    assessments.dispose(assessment.pk, actor="responder", outcome="no_issue", reason="No impact found.")
    retry, created = assessments.receive(title="Retry", source="customer", signal="Redelivery", source_event_id="signal-1")
    assert not created and retry.pk == assessment.pk and retry.status == "no_issue"
    other, created = assessments.receive(title="Different source", source="supplier", signal="New source", source_event_id="signal-1")
    assert created and other.pk != assessment.pk
    assert not Incident.objects.exists()


def test_payload_identity_is_stable_without_external_id():
    data = {"title": "Concern", "source": "operator", "signal": "Check this", "payload": {"a": 1, "b": 2}}
    first, _ = assessments.receive(**data)
    again, created = assessments.receive(**{**data, "payload": {"b": 2, "a": 1}})
    assert not created and first.pk == again.pk


@pytest.mark.parametrize("verdict", ["no_issue", "degraded"])
def test_conclusion_requires_evidence(assessment, verdict):
    with pytest.raises(ValueError, match="Record evidence"):
        conclusion(assessment, verdict)


def test_new_evidence_invalidates_previous_conclusion(assessment):
    evidence(assessment)
    conclusion(assessment)
    evidence(assessment)
    assessment.refresh_from_db()
    assert (assessment.verdict, assessment.confidence, assessment.conclusion) == ("unknown", "unknown", "")
    assert assessment.history.filter(kind="conclusion_recorded").count() == 1
    with pytest.raises(ValueError, match="Promotion needs evidence"):
        assessments.promote(assessment.pk, actor="responder", reason="Stale verdict")


def test_monitoring_keeps_assessment_out_of_incident_counts(assessment):
    assessments.dispose(assessment.pk, actor="responder", outcome="monitoring", reason="Need a longer window.")
    assessment.refresh_from_db()
    assert assessment.status == "monitoring" and assessment.closed_at is None
    assert not Incident.objects.exists()
    with pytest.raises(ValueError, match="no-issue conclusion"):
        assessments.dispose(assessment.pk, actor="responder", outcome="no_issue", reason="Close prematurely")


def test_no_issue_is_terminal_but_allows_audited_comments(assessment):
    evidence(assessment)
    conclusion(assessment, "no_issue")
    assessments.dispose(assessment.pk, actor="responder", outcome="no_issue", reason="Checks are healthy.")
    assessment.refresh_from_db()
    assert assessment.closed_at is not None
    with pytest.raises(assessments.AssessmentConflict):
        evidence(assessment)
    with pytest.raises(assessments.AssessmentConflict):
        assessments.promote(assessment.pk, actor="responder", reason="Stale browser")
    assert assessments.comment(assessment.pk, actor="responder", body="Follow-up note").kind == "comment"
    assert not Incident.objects.exists()


def test_promotion_is_idempotent_and_retains_one_history(assessment, responder, django_capture_on_commit_callbacks):
    first = evidence(assessment)
    conclusion(assessment)
    with patch("incidents.escalation.start_escalation", return_value="local:test") as start:
        with django_capture_on_commit_callbacks(execute=True):
            incident, created = assessments.promote(assessment.pk, actor=responder.username, reason="Checkout is degraded.")
            assert created
        assert start.call_count == 1
        repeated, created = assessments.promote(assessment.pk, actor=responder.username, reason="Repeated submit")
        assert not created and repeated.pk == incident.pk
        assert start.call_count == 1
    assessment.refresh_from_db()
    assert assessment.status == "promoted" and assessment.promoted_at == incident.created_at
    assert assessment.signal_at < incident.created_at
    assert Incident.objects.count() == 1
    services.add_note(incident, actor=responder.username, body="T2 investigation")
    services.acknowledge(incident.pk, actor=str(responder.pk), reason="Taking ownership")
    services.escalate(incident.pk, actor=str(responder.pk), reason="Specialist authority needed")
    services.resolve(incident.pk, actor=str(responder.pk), reason="Journey restored")
    history = services.timeline(incident)
    assert first.pk in [e["obj"].pk for e in history]
    assert any("Taking ownership" in e["obj"].body for e in history)
    assert any("Specialist authority needed" in e["obj"].body for e in history)
    assert any("Journey restored" in e["obj"].body for e in history)
    exported = services.rca_markdown(incident)
    assert "A customer reports failures." in exported and "sample window 15 minutes" in exported
    assert "compare successful requests" in exported


def test_policy_override_records_reason_and_no_issue_still_cannot_promote(assessment, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        incident, _ = assessments.promote(assessment.pk, actor="responder", reason="Immediate response required",
                                         policy_reason="Emergency policy requires an incident pending evidence.")
    event = assessment.history.get(kind="incident_promoted")
    assert event.data["policy_reason"].startswith("Emergency policy")
    incident.refresh_from_db()
    assert incident.escalation_execution_arn.startswith("local:")


def test_failed_dispatch_is_durable_and_retryable(assessment, django_capture_on_commit_callbacks):
    evidence(assessment)
    conclusion(assessment)
    with patch("incidents.escalation.start_escalation", side_effect=RuntimeError("Unavailable")):
        with django_capture_on_commit_callbacks(execute=True):
            incident, _ = assessments.promote(assessment.pk, actor="responder", reason="Confirmed degradation")
    incident.refresh_from_db()
    assert not incident.escalation_execution_arn
    assert assessment.history.filter(kind="escalation_start_failed").count() == 1
    with patch("incidents.escalation.start_escalation", return_value="local:recovered") as start:
        assessments.start_engine(assessment.pk)
        assessments.start_engine(assessment.pk)
        assert start.call_count == 1
    incident.refresh_from_db()
    assert incident.escalation_execution_arn == "local:recovered"


def test_history_is_append_only(assessment):
    event = assessment.history.first()
    event.body = "Overwrite"
    with pytest.raises(ValueError, match="append-only"):
        event.save()
    with pytest.raises(ValueError, match="append-only"):
        event.delete()
    with pytest.raises(ValueError, match="append-only"):
        assessment.history.update(body="Overwrite")
    with pytest.raises(ValueError, match="append-only"):
        assessment.history.all().delete()


@pytest.mark.parametrize("function,kwargs", [
    (assessments.add_evidence, {"summary": "", "result": "result"}),
    (assessments.conclude, {"verdict": "bogus", "confidence": "high", "reason": "x"}),
    (assessments.conclude, {"verdict": "unknown", "confidence": "low", "reason": " "}),
    (assessments.dispose, {"outcome": "monitoring", "reason": " "}),
    (assessments.dispose, {"outcome": "bogus", "reason": "reason"}),
    (assessments.comment, {"body": " "}),
    (assessments.promote, {"reason": " "}),
])
def test_invalid_decisions_leave_history_unchanged(assessment, function, kwargs):
    count = assessment.history.count()
    with pytest.raises(ValueError):
        function(assessment.pk, actor="responder", **kwargs)
    assert assessment.history.count() == count and not Incident.objects.exists()


def test_intake_api_creates_assessments_without_incidents_and_protects_reads():
    client = APIClient()
    url = "/api/assessments/intake"
    data = {"title": "Concern", "source": "monitor", "signal": "Errors observed", "source_event_id": "abc"}
    assert client.post(url, data, format="json").status_code == 401
    assert client.post(url, data, format="json", HTTP_X_WATCH_WEBHOOK_SECRET="test-secret").status_code == 201
    assert client.post(url, data, format="json", HTTP_X_WATCH_WEBHOOK_SECRET="test-secret").status_code == 200
    bad = {**data, "payload": []}
    assert client.post(url, bad, format="json", HTTP_X_WATCH_WEBHOOK_SECRET="test-secret").status_code == 400
    assert Assessment.objects.count() == 1 and not Incident.objects.exists()
    assert client.get("/api/assessments/").status_code in (401, 403)


def test_ui_auth_roles_and_filtering(assessment, responder):
    client = Client()
    base = f"/ui/assessments/{assessment.pk}/"
    assert client.get(base).status_code == 302
    client.force_login(User.objects.create_user("viewer"))
    assert client.get(base).status_code == 200
    assert client.get("/ui/assessments/new/").status_code == 403
    assert client.post(base, {"action": "comment", "comment-body": "A note"}).status_code == 403
    client.force_login(responder)
    assert assessment.title in client.get("/ui/assessments/?q=Example&status=all").content.decode()
    assert assessment.title not in client.get("/ui/assessments/?status=no_issue").content.decode()
    assert client.get("/ui/assessments/new/").status_code == 200
    assert client.post("/ui/assessments/new/", {}).status_code == 400
    assert client.post(base, {"action": "invalid"}).status_code == 400
    assert client.post(base, {"action": "evidence"}).status_code == 400
    assert client.post(base, {"action": "conclusion", "conclusion-verdict": "degraded",
                              "conclusion-confidence": "high", "conclusion-reason": "No evidence yet"}).status_code == 400


def test_ui_forms_close_and_promote(assessment, responder):
    client = Client()
    client.force_login(responder)
    url = f"/ui/assessments/{assessment.pk}/"
    assert client.post(url, {"action": "evidence", "evidence-summary": "Checks", "evidence-source": "operator",
                             "evidence-result": "Journey failure in a 15-minute window"}).status_code == 302
    assert client.post(url, {"action": "conclusion", "conclusion-verdict": "no_issue", "conclusion-confidence": "high",
                             "conclusion-reason": "Healthy journey"}).status_code == 302
    assert client.post(url, {"action": "monitor", "monitor-reason": "Observe longer"}).status_code == 302
    assert client.post(url, {"action": "no_issue", "no_issue-reason": "No degradation"}).status_code == 302
    assert client.post(url, {"action": "evidence", "evidence-summary": "Later check", "evidence-source": "operator",
                             "evidence-result": "Later result"}).status_code == 409
    assert client.post(url, {"action": "comment", "comment-body": "Follow-up"}).status_code == 302
    assert b"Assessment complete" in client.get(url).content


def test_ui_promoted_history_api_and_annotation(assessment, responder, django_capture_on_commit_callbacks):
    client = Client()
    client.force_login(responder)
    evidence(assessment)
    conclusion(assessment)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(f"/ui/assessments/{assessment.pk}/", {"action": "promote", "promote-reason": "Real degradation"})
    assessment.refresh_from_db()
    incident = assessment.incident
    assert response.url == f"/ui/incidents/{incident.pk}/"
    assert b"Continues from" in client.get(response.url).content
    assert b"Open incident" in client.get(f"/ui/assessments/{assessment.pk}/").content
    api = APIClient()
    api.force_authenticate(responder)
    assert api.get(f"/api/assessments/{assessment.pk}/").status_code == 200
    assert api.get(f"/api/assessments/{assessment.pk}/history/").status_code == 200
    target = assessment.history.filter(kind="evidence_added").first()
    response = client.post(f"/ui/incidents/{incident.pk}/annotate/",
                           {"target": f"case_event:{target.pk}", "tag": "root-cause", "body": "Important evidence"})
    assert response.status_code == 200
    assert target.annotations.count() == 1
    assert "Important evidence" in services.rca_markdown(incident)


def test_generic_demo_is_repeatable_and_has_no_external_side_effects(settings):
    settings.DEBUG = True
    user = User.objects.create_user("t2a")
    user.groups.add(Group.objects.get_or_create(name="T2")[0])
    with patch("incidents.notify.send", side_effect=AssertionError("No notification")), \
         patch("incidents.escalation.start_escalation", side_effect=AssertionError("No execution")):
        call_command("seed_assessments", stdout=StringIO())
        call_command("seed_assessments", stdout=StringIO())
    assert Assessment.objects.count() == 4 and Incident.objects.count() == 1
    assert Assessment.objects.get(status="no_issue").incident is None
    assert Assessment.objects.get(status="monitoring").verdict == "unknown"


def test_ui_creates_a_signal_and_preserves_duplicate_submit(responder):
    import uuid
    client = Client()
    client.force_login(responder)
    body = {"signal-title": "New concern", "signal-source": "operator",
            "signal-signal": "Please establish impact", "signal-source_event_id": str(uuid.uuid4())}
    first = client.post("/ui/assessments/new/", body)
    again = client.post("/ui/assessments/new/", body)
    assert first.status_code == 302 and first.url == again.url
    assert Assessment.objects.count() == 1 and not Incident.objects.exists()


def test_ui_retry_is_visible_and_recovers_dispatch(assessment, responder, django_capture_on_commit_callbacks):
    evidence(assessment)
    conclusion(assessment)
    with patch("incidents.escalation.start_escalation", side_effect=RuntimeError("Unavailable")):
        with django_capture_on_commit_callbacks(execute=True):
            assessments.promote(assessment.pk, actor="responder", reason="Impact confirmed")
    client = Client()
    client.force_login(responder)
    url = f"/ui/assessments/{assessment.pk}/"
    assert b"Retry escalation start" in client.get(url).content
    assert client.post(url, {"action": "retry_engine", "retry_engine-reason": "Provider recovered"}).status_code == 302
    assessment.refresh_from_db()
    assert assessment.incident.escalation_execution_arn.startswith("local:")
    assert b"Retry escalation start" not in client.get(url).content


def test_exercise_dispatch_bypass_requires_development_settings(assessment, settings):
    with pytest.raises(ValueError, match="local development"):
        assessments.promote(assessment.pk, actor="responder", reason="Test", dispatch=False)
