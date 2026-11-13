"""Assessment operations: deadlines, diagnosis, handoffs, RCA integrity, and real cohorts."""
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch
import uuid

import pytest
import requests
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from incidents import assessments, automation, cadence, communications, diagnostics, operations, reporting, services
from incidents.models import (
    Assessment, AssessmentPolicy, Communication, CommunicationRevision, CuratedEvent, DiagnosticRule,
    DiagnosticRun, Incident, OperationsHeartbeat, OutboxMessage, Page, RemediationTask, ServiceDependency,
    SubjectArea, Transition,
)
from incidents.operations_forms import DiagnosticRuleForm, PolicyForm

pytestmark = pytest.mark.django_db


@pytest.fixture
def actor():
    user = User.objects.create_user("operator", password="example-password")
    user.groups.add(Group.objects.get_or_create(name="T2")[0])
    return user


@pytest.fixture
def row():
    return assessments.receive(title="Example checkout concern", source="monitor", signal="Synthetic checkout failures.", customer="Example Customer", service="Catalog", supplier="Supplier A", payload={"fixture": True})[0]


@pytest.fixture
def rule():
    return DiagnosticRule.objects.create(name="Example error rate", provider="fixture", query='errors{customer="${customer}"}', comparator="above", threshold=2, minimum_samples=100, fixture_result={"value": 6, "baseline": 0.2, "samples": 800})


@pytest.fixture
def policy(rule):
    obj = AssessmentPolicy.objects.create(name="Example scoped policy", active=True, customer="Example Customer", service="Catalog", supplier="Supplier A", auto_promote=True, auto_close_no_issue=True, auto_send=True, recipients=["operations@example.test"], review_seconds=5)
    obj.rules.add(rule)
    return obj


def mail(row, **kwargs):
    return communications.draft(row.pk, actor="system:automation", subject="Example update", body="Customer-scoped evidence summary.", recipients=["ops@example.test"], **kwargs)


def due(item):
    Communication.objects.filter(pk=item.pk).update(review_deadline=timezone.now() - timedelta(seconds=1))


def promote(row, actor):
    assessments.add_evidence(row.pk, actor=actor.username, summary="Confirmed errors", result="6% failures; 800 requests; 15 minutes.")
    assessments.conclude(row.pk, actor=actor.username, verdict="degraded", confidence="high", reason="Errors exceed threshold.")
    incident, _ = assessments.promote(row.pk, actor=actor.username, reason="Confirmed customer impact.")
    row.refresh_from_db()
    return incident


def test_review_edit_send_receipt_is_versioned_and_idempotent(row):
    item = mail(row, auto_send=True, purpose="initial")
    deadline, sla = item.review_deadline, item.sla_deadline
    assert deadline < sla
    communications.edit(item.pk, actor="operator", expected_version=1, subject="Edited update", body="Reviewed evidence.", recipients="ops@example.test, OPS@example.test", auto_send=True)
    item.refresh_from_db()
    assert item.revision == 2 and item.review_deadline == deadline and item.sla_deadline == sla
    assert item.revisions.count() == 2 and item.revisions.get(version=1).subject == "Example update"
    assert item.current.recipients == ["ops@example.test"]
    with pytest.raises(assessments.AssessmentConflict):
        communications.send(item.pk, actor="operator", expected_version=1)
    communications.send(item.pk, actor="operator", expected_version=2)
    communications.send(item.pk, actor="operator", expected_version=2)
    item.refresh_from_db()
    assert item.state == "sent" and item.sla_met and not item.is_automatic
    assert OutboxMessage.objects.count() == 1
    assert item.receipt.revision.version == 2
    assert b"Edited update" in communications.message_bytes(item.receipt)
    assert row.history.filter(kind="communication_sent").count() == 1
    with pytest.raises(assessments.AssessmentConflict):
        communications.edit(item.pk, actor="operator", expected_version=2, subject="Late", body="Late", recipients=["ops@example.test"], auto_send=True)


def test_deadline_authorization_and_suppression(row, settings):
    item = mail(row, auto_send=True)
    assert communications.send(item.pk, actor="system", automatic=True).state == "review"
    due(item)
    cadence.tick()
    item.refresh_from_db()
    assert item.state == "sent" and item.is_automatic
    assert row.history.filter(kind="review_window_expired").count() == 1
    assert row.history.filter(kind="communication_auto_sent").count() == 1
    manual = mail(row)
    due(manual)
    cadence.tick()
    manual.refresh_from_db()
    assert manual.state == "expired" and manual.sent_at is None
    communications.send(manual.pk, actor="operator", expected_version=1)
    suppressed = mail(row, auto_send=True)
    with pytest.raises(ValueError, match="reason"):
        communications.suppress(suppressed.pk, actor="operator", expected_version=1, reason=" ")
    communications.suppress(suppressed.pk, actor="operator", expected_version=1, reason="Duplicate advisory.")
    communications.suppress(suppressed.pk, actor="operator", expected_version=1, reason="Duplicate advisory.")
    with pytest.raises(assessments.AssessmentConflict):
        communications.send(suppressed.pk, actor="operator")
    due(suppressed)
    cadence.tick()
    assert OutboxMessage.objects.count() == 2
    disabled = mail(row, auto_send=True)
    due(disabled)
    settings.WATCH_AUTO_SEND_ENABLED = False
    assert communications.send(disabled.pk, actor="system", automatic=True).state == "expired"
    assert OperationsHeartbeat.objects.get(name="operations").last_tick


@pytest.mark.parametrize("change", [{"subject": "bad\nheader"}, {"body": " "}, {"recipients": []}, {"recipients": [1]}, {"recipients": "invalid"}, {"review_seconds": 0}, {"sla_seconds": 0}])
def test_invalid_drafts_do_not_leave_records(row, change):
    args = dict(actor="operator", subject="Update", body="Message", recipients=["ops@example.test"])
    with pytest.raises(ValueError):
        communications.draft(row.pk, **{**args, **change})
    assert not Communication.objects.exists()


def test_expired_edits_and_unsupported_delivery_are_explicit(row, settings):
    item = mail(row)
    due(item)
    with pytest.raises(assessments.AssessmentConflict):
        communications.edit(item.pk, actor="operator", expected_version=1, subject="Edited", body="Later", recipients="ops@example.test", auto_send=False)
    with pytest.raises(assessments.AssessmentConflict):
        communications.suppress(item.pk, actor="operator", expected_version=1, reason="Too late")
    settings.WATCH_DELIVERY_MODE = "smtp"
    with pytest.raises(ValueError, match="local outbox"):
        communications.send(item.pk, actor="operator")
    assert not OutboxMessage.objects.exists()
    revision = item.current
    revision.body = "Overwrite"
    for mutation in (revision.save, revision.delete, lambda: item.revisions.update(body="Overwrite"), lambda: item.revisions.all().delete()):
        with pytest.raises(ValueError, match="append-only"):
            mutation()


def test_fresh_evidence_revokes_stale_automatic_draft(row, rule, settings):
    settings.DEBUG = True
    item = mail(row, auto_send=True)
    diagnostics.execute(row, rule)
    item.refresh_from_db(); row.refresh_from_db()
    assert not item.auto_send and row.verdict == "unknown"
    assert row.history.filter(kind="communication_review_required").exists()
    due(item)
    assert communications.send(item.pk, actor="system", automatic=True).state == "expired"


@pytest.mark.parametrize("value,baseline,samples,comparator,expected", [(0.2, 1, 800, "above", False), (6, 1, 800, "above", True), (0.2, 1, 800, "below", True), (96.0, 100, 800, "deviation", True), (100, 100, 800, "deviation", False), (2, 1, 800, "above", False)])
def test_diagnostics_preserve_actual_checks(row, rule, settings, value, baseline, samples, comparator, expected):
    settings.DEBUG = True
    rule.comparator = comparator
    rule.fixture_result = {"value": value, "baseline": baseline, "samples": samples}
    run = diagnostics.execute(row, rule)
    assert run.state == "done" and run.result["breached"] is expected
    assert run.result["samples"] == samples and run.result["window_from"] < run.result["window_to"]
    event = row.history.get(kind="evidence_added")
    assert event.data["execution"] == "executed" and 'customer="Example Customer"' in event.data["query"]
    assert row.history.filter(kind="query_completed").count() == 1


@pytest.mark.parametrize("change,error", [({"fixture_result": {"value": 0, "samples": 0}}, "Insufficient"), ({"fixture_result": {"value": "NaN", "samples": 100}}, "non-finite"), ({"query": "${unknown}"}, "Unrecognized"), ({"threshold": float("nan")}, "finite"), ({"minimum_samples": 0}, "positive"), ({"lookback_minutes": 0}, "invalid"), ({"provider": "unknown"}, "Unknown"), ({"comparator": "invalid"}, "comparator"), ({"comparator": "deviation", "fixture_result": {"value": 4, "baseline": 0, "samples": 500}}, "nonzero")])
def test_bad_or_missing_diagnostic_data_is_not_healthy(row, rule, settings, change, error):
    settings.DEBUG = True
    for key, value in change.items():
        setattr(rule, key, value)
    run = DiagnosticRun.objects.create(assessment=row, rule=rule)
    result = diagnostics.execute(row, rule, run_id=run.pk)
    assert result.state == "failed" and error in result.error
    assert not row.history.filter(kind="evidence_added").exists()
    assert diagnostics.execute(row, rule, run_id=run.pk).state == "failed"


def test_fixture_cannot_be_used_as_real_production_evidence(row, rule):
    assert diagnostics.execute(row, rule).state == "failed"


def response(payload):
    obj = Mock(); obj.json.return_value = payload
    return obj


def test_prometheus_real_adapter_requires_fresh_unambiguous_data(row, rule):
    now = timezone.now().timestamp()
    def metric(value):
        return response({"status": "success", "data": {"resultType": "vector", "result": [{"value": [now, str(value)]}]}})
    rule.provider, rule.sample_query, rule.baseline_query = "prometheus", "count_samples", "baseline"
    with patch("incidents.diagnostics.requests.get", side_effect=[metric(6), metric(0.2), metric(800)]) as get:
        run = diagnostics.execute(row, rule)
    assert run.state == "done" and run.result["breached"] and get.call_count == 3
    with patch("incidents.diagnostics.requests.get", return_value=response({"status": "success", "data": {"resultType": "scalar", "result": [now, "1"]}})):
        assert diagnostics.prometheus("query") == 1
    for payload in [{"status": "error"}, {"status": "success", "warnings": ["partial"]}, {"status": "success", "data": {"resultType": "vector", "result": []}}, {"status": "success", "data": {"resultType": "scalar", "result": [now - 301, "1"]}}, {"status": "success", "data": {"resultType": "scalar", "result": [now]}}, {"status": "success", "data": {"resultType": "scalar", "result": [now, "bad"]}}]:
        with patch("incidents.diagnostics.requests.get", return_value=response(payload)), pytest.raises(diagnostics.DiagnosticError):
            diagnostics.prometheus("query")
    with patch("incidents.diagnostics.requests.get", side_effect=requests.Timeout("SECRET")):
        failed = diagnostics.execute(row, rule)
    assert failed.state == "failed" and "SECRET" not in failed.error
    rule.sample_query = ""
    with patch("incidents.diagnostics.prometheus", return_value=1):
        assert diagnostics.execute(row, rule).state == "failed"


def test_sumo_retains_session_and_aggregate_contract(settings):
    with pytest.raises(diagnostics.DiagnosticError, match="credentials"):
        diagnostics.sumo("query", 15)
    settings.SUMO_API_ENDPOINT = "https://example.test"
    settings.SUMO_ACCESS_ID, settings.SUMO_ACCESS_KEY = "example", "test"
    session = Mock(); session.__enter__ = Mock(return_value=session); session.__exit__ = Mock(return_value=False)
    session.post.return_value = response({"id": "job-1"})
    session.get.side_effect = [response({"state": "GATHERING RESULTS"}), response({"state": "DONE GATHERING RESULTS"}), response({"records": [{"map": {"value": "3.7"}}]})]
    with patch("incidents.diagnostics.requests.Session", return_value=session), patch("incidents.diagnostics.time.sleep"):
        assert diagnostics.sumo("query", 15) == 3.7
    assert session.auth == ("example", "test")
    session.post.return_value = response({"id": "../invalid"})
    with patch("incidents.diagnostics.requests.Session", return_value=session), pytest.raises(diagnostics.DiagnosticError):
        diagnostics.sumo("query", 15)
    session.post.return_value = response({"id": "job-1"})
    for replies in [[response({"state": "CANCELLED"})], [response({"state": "DONE GATHERING RESULTS"}), response({"records": []})], [response({"state": "GATHERING RESULTS"})] * 15]:
        session.get.side_effect = replies
        with patch("incidents.diagnostics.requests.Session", return_value=session), patch("incidents.diagnostics.time.sleep"), pytest.raises(diagnostics.DiagnosticError):
            diagnostics.sumo("query", 15)


def test_supplier_fanout_is_scoped_and_idempotent(policy):
    for customer in ("Example Customer", "Example Customer B"):
        ServiceDependency.objects.create(customer=customer, supplier="Supplier A", service="Catalog")
    args = dict(title="Supplier advisory", source="supplier", signal="Investigate actual impact", supplier="Supplier A", source_event_id="example-1")
    rows = automation.receive_signal(**args)
    assert len(rows) == 2 and all(created for _, created in rows)
    assert sum(created for _, created in automation.receive_signal(**args)) == 0
    assert {row.customer for row, _ in rows} == {"Example Customer", "Example Customer B"}
    assert not Incident.objects.exists()
    assert automation.receive_signal(**{**args, "customer": "Example Customer", "service": "Catalog"})[0][1] is False
    assert not automation.schedule(assessments.receive(title="Other", signal="Other", source="operator")[0])


@pytest.mark.parametrize("scenario,verdict,status", [("degraded", "degraded", "promoted"), ("healthy", "no_issue", "investigating"), ("missing", "unknown", "monitoring")])
def test_automatic_assessment_end_to_end(row, rule, policy, settings, django_capture_on_commit_callbacks, scenario, verdict, status):
    settings.DEBUG = True
    if scenario == "healthy":
        rule.fixture_result["value"] = 0.2
    elif scenario == "missing":
        rule.fixture_result["samples"] = 0
    rule.save()
    automation.schedule(row)
    with django_capture_on_commit_callbacks(execute=True):
        automation.run(row.pk)
    row.refresh_from_db()
    assert row.verdict == verdict and row.status == status and row.automation_state == "complete"
    assert row.communications.count() == 1
    if scenario == "degraded":
        assert row.incident_id and row.promoted_at == row.incident.created_at
        policy.route_to_t2 = True; policy.save()
        with django_capture_on_commit_callbacks(execute=True):
            cadence.tick()
        row.refresh_from_db()
        assert row.incident.current_tier == "T2"
        assert Page.objects.filter(incident=row.incident, topic="local:in-app:T2").exists()
    elif scenario == "healthy":
        due(row.communications.get()); cadence.tick(); row.refresh_from_db()
        assert row.status == "no_issue" and not row.incident_id
    else:
        assert not row.incident_id
    automation.run(row.pk)
    assert row.communications.count() == 1


def test_policy_precedence_intervention_failure_and_no_mail_closure(row, rule, policy, settings):
    settings.DEBUG = True
    broad = AssessmentPolicy.objects.create(name="Wildcard", active=True)
    assert automation.policy_for(row) == policy
    rule.fixture_result["value"] = 0; rule.save()
    policy.recipients = []; policy.save()
    automation.schedule(row); automation.run(row.pk); row.refresh_from_db()
    assert row.status == "no_issue"
    other = assessments.receive(title="Human intervention", signal="Check", source="test", customer=row.customer, service=row.service, supplier=row.supplier, payload={"fixture": True})[0]
    automation.schedule(other)
    original = diagnostics.execute
    def intervening(*args, **kwargs):
        result = original(*args, **kwargs)
        assessments.comment(other.pk, actor="operator", body="Hold for investigation.")
        return result
    with patch("incidents.automation.diagnostics.execute", side_effect=intervening):
        automation.run(other.pk)
    other.refresh_from_db()
    assert other.status == "investigating" and other.verdict == "unknown"
    assert other.history.filter(kind="automation_review_required").exists()
    missing = assessments.receive(title="No policy rules", signal="Check", source="test", customer="Other", service="Catalog")[0]
    automation.schedule(missing); automation.run(missing.pk); missing.refresh_from_db()
    assert missing.automation_state == "failed" and not missing.incident_id


def test_stale_worker_jobs_are_visible_and_recoverable(row, rule, policy):
    old = timezone.now() - timedelta(hours=2)
    Assessment.objects.filter(pk=row.pk).update(automation_state="running", updated_at=old)
    run = DiagnosticRun.objects.create(assessment=row, rule=rule, state="running", started_at=old)
    cadence.tick(); row.refresh_from_db(); run.refresh_from_db()
    assert row.automation_state == run.state == "failed"
    assert "lease" in run.error


def test_specialist_handoff_retains_context_without_changing_tier_clock(row, actor, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        promote(row, actor)
    specialist = User.objects.create_user("specialist")
    area = SubjectArea.objects.create(name="Database"); area.members.add(specialist)
    event = row.history.filter(kind="evidence_added").get()
    row.incident.refresh_from_db()
    before = (row.incident.current_tier, row.incident.current_task_token, row.incident.sla_deadline_at)
    args = dict(user=actor, area=area, reason="Inspect connection saturation", selected=[event.pk], request_id=uuid.uuid4())
    handoff = operations.engage(row.pk, **args)
    assert operations.engage(row.pk, **args).pk == handoff.pk
    with pytest.raises(PermissionError):
        operations.respond_engagement(handoff.pk, user=actor, action="ack")
    with pytest.raises(ValueError):
        operations.respond_engagement(handoff.pk, user=specialist, action="complete", body="Not yet acknowledged")
    type(handoff).objects.filter(pk=handoff.pk).update(ack_deadline=timezone.now()-timedelta(seconds=1))
    cadence.tick(); handoff.refresh_from_db(); assert handoff.state == "overdue"
    operations.respond_engagement(handoff.pk, user=specialist, action="ack")
    operations.respond_engagement(handoff.pk, user=specialist, action="ack")
    operations.respond_engagement(handoff.pk, user=specialist, action="complete", body="Reduced churn; pool saturation recovered.")
    operations.respond_engagement(handoff.pk, user=specialist, action="complete", body="Repeated submit")
    row.incident.refresh_from_db()
    assert before == (row.incident.current_tier, row.incident.current_task_token, row.incident.sla_deadline_at)
    assert row.history.get(kind="sme_acknowledged").data["late"] is True
    assert row.history.filter(kind="sme_action").count() == 1
    uploaded = operations.upload(row.pk, user=specialist, file=SimpleUploadedFile("pool.txt", b"Pool recovered"), description="Specialist evidence")
    assert uploaded.uploaded_by == specialist
    with pytest.raises(ValueError):
        operations.respond_engagement(handoff.pk, user=specialist, action="invalid")


def test_evidence_files_and_source_scoped_rca(row, actor, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        incident = promote(row, actor)
    event = row.history.filter(kind="evidence_added").get()
    for data in [b"\x89PNG\r\n\x1a\nexample", b"\xff\xd8\xffimage", b"%PDF-example", b"RIFFabcdWEBPexample", b"plain text"]:
        item = operations.upload(row.pk, user=actor, file=SimpleUploadedFile('../../example.txt', data), description="Evidence sample")
        assert len(item.sha256) == 64 and "/" not in item.filename
    for data in [b"", b"\x00binary", b"\xff", b"a" * (5 * 1024 * 1024 + 1)]:
        with pytest.raises(ValueError):
            operations.upload(row.pk, user=actor, file=SimpleUploadedFile("bad", data), description="Bad")
    other = assessments.receive(title="Unrelated", source="test", signal="Other")[0]
    with pytest.raises(ValueError, match="belong"):
        operations.curate(row.pk, user=actor, event=other.history.first(), section="impact")
    projection = operations.curate(row.pk, user=actor, event=event, section="impact", note="Confirmed scope")
    operations.curate(row.pk, user=actor, event=event, section="impact", note="Updated enrichment")
    assert projection.selections.count() == 1 and event.body == "Confirmed errors"
    with pytest.raises(ValueError, match="Final RCA"):
        operations.save_rca(row.pk, user=actor, state="final", summary="Summary", root_cause="Cause")
    services.resolve(incident.pk, actor=str(actor.pk), reason="Recovery confirmed")
    operations.save_rca(row.pk, user=actor, state="final", summary="Summary", root_cause="Pool exhaustion", detection_gaps="Add alert")
    owner = User.objects.create_user("action-owner")
    task = operations.remediation(row.pk, user=actor, title="Add pool alert", owner=owner, due_date=timezone.now().date())
    with pytest.raises(ValueError):
        operations.update_remediation(task.pk, user=owner, state="done")
    operations.update_remediation(task.pk, user=owner, state="done", verification="Alert fired in controlled replay.")
    exported = operations.rca_markdown(row)
    assert f"?event={event.pk}" in exported and "Pool exhaustion" in exported and "controlled replay" in exported
    assert "6% failures" in exported
    operations.curate(row.pk, user=actor, event=event, section="impact", remove=True)
    assert not CuratedEvent.objects.exists() and row.history.filter(kind="rca_selection_changed").count() == 3


def test_invalid_operations_are_atomic(row, actor):
    outsider = User.objects.create_user("viewer")
    with pytest.raises(PermissionError):
        operations.record_action(row.pk, user=outsider, kind="hypothesis", body="Guess")
    for kind, body in [("invalid", "Action"), ("mitigation", " ")]:
        with pytest.raises(ValueError):
            operations.record_action(row.pk, user=actor, kind=kind, body=body)
    with pytest.raises(ValueError):
        operations.engage(row.pk, user=actor, target=actor, reason="Help", selected=[], request_id=uuid.uuid4())
    with pytest.raises(ValueError):
        operations.curate(row.pk, user=actor, event=row.history.first(), section="impact")
    with pytest.raises(ValueError):
        operations.save_rca(row.pk, user=actor, summary="Premature")


def test_reports_do_not_mix_assessments_with_confirmed_incident_clocks(row, actor, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        incident = promote(row, actor)
    # Create the first ACK at +2m, a subsequent ACK at +5m, resolution at +10m.
    base = row.promoted_at
    for seconds, status in [(120, "OPEN"), (300, "OPEN"), (600, "RESOLVED")]:
        transition = Transition.objects.create(incident=incident, from_tier="T1", to_tier="T1", from_status="OPEN", to_status=status, actor=str(actor.pk), reason="Example")
        Transition.objects.filter(pk=transition.pk).update(at=base + timedelta(seconds=seconds))
    other = assessments.receive(title="Healthy concern", signal="Check", source="customer", customer="Example Customer", service="Catalog")[0]
    assessments.add_evidence(other.pk, actor="system", summary="Healthy", result="Healthy sample")
    assessments.conclude(other.pk, actor="system", verdict="no_issue", confidence="high", reason="No errors")
    assessments.dispose(other.pk, actor="system", outcome="no_issue", reason="Healthy")
    result = reporting.summarize(include_examples=True)
    assert result["assessments"] == 2 and result["incidents"] == 1
    assert result["mtta_minutes"] == 2 and result["mttr_minutes"] == 10
    assert result["automatic_no_issue"] == 1 and result["conversion_percent"] == 50
    assert reporting.summarize()["incidents"] == 0  # synthetic fixture excluded
    assert reporting.summarize()["assessments"] == 1  # non-fixture JSON with no key remains included
    assert reporting.summarize(customer="Missing")["mtta_minutes"] is None
    assert reporting.summarize(include_examples=True, service="Catalog")["incidents"] == 1
    assert reporting.summarize(include_examples=True, severity="P1")["incidents"] == 0


def test_operations_ui_and_api_full_flow(row, actor, rule, django_capture_on_commit_callbacks):
    client = Client(); client.force_login(actor)
    url = f"/ui/assessments/{row.pk}/"
    assert client.get(url + "state/").json()["version"]
    assert client.post(url, {"action": "query", "query-rule": rule.pk}).status_code == 302
    assert DiagnosticRun.objects.get().state == "queued"
    assert client.post(url, {"action": "attachment", "attachment-description": "Regional evidence", "attachment-file": SimpleUploadedFile("region.txt", b"Errors by region")}).status_code == 302
    item = row.attachments.get()
    downloaded = client.get(f"/ui/evidence/{item.pk}/")
    assert downloaded.content == b"Errors by region" and downloaded["X-Content-Type-Options"] == "nosniff"
    assert client.post(url, {"action": "operation", "operation-kind": "hypothesis", "operation-body": "Regional connection churn"}).status_code == 302
    assert client.post(url, {"action": "claim", "claim-reason": "Taking investigation"}).status_code == 302
    row.refresh_from_db(); assert row.assignee == actor
    assert client.post(url, {"action": "context", "context-severity": "P2", "context-impact": "moderate", "context-reason": "Scope confirmed"}).status_code == 302
    row.refresh_from_db(); assert row.severity == "P2"
    data = {"action": "email", "email-subject": "Synthetic update", "email-body": "Investigation continues", "email-recipients": "ops@example.test", "email-review_seconds": 300, "email-sla_seconds": 900, "email-purpose": "update", "email-auto_send": True}
    result = client.post(url, data); assert result.status_code == 302
    communication_url = result.url
    communication = row.communications.get()
    assert client.get(communication_url).status_code == 200
    assert client.get(communication_url + "state/").json()["version"] == "review:1"
    edit = {"action": "edit", "draft-subject": "Reviewed update", "draft-body": "Reviewed facts", "draft-recipients": "ops@example.test", "draft-expected_version": 1, "draft-auto_send": True}
    assert client.post(communication_url, edit).status_code == 302
    assert client.post(communication_url, edit).status_code == 409
    assert client.post(communication_url, {"action": "edit"}).status_code == 400
    assert client.post(communication_url, {"action": "invalid"}).status_code == 400
    assert client.post(communication_url, {"action": "send", "expected_version": 2}).status_code == 302
    assert b"Download delivered message" in client.get(communication_url).content
    assert b"Reviewed update" in client.get("/ui/outbox/").content
    assert client.get(f"/ui/outbox/{OutboxMessage.objects.get().pk}/message.eml")["Content-Type"] == "message/rfc822"
    for tab in ("evidence", "communications", "sme", "rca", "invalid"):
        assert client.get(url + f"?tab={tab}").status_code == 200
    for lens in ("conversation", "evidence"):
        assert client.get(url + f"?lens={lens}").status_code == 200
    assert client.get(url + f"?event={row.history.first().pk}").status_code == 200
    with django_capture_on_commit_callbacks(execute=True):
        promote(row, actor)
    assert client.get(f"/ui/incidents/{row.incident_id}/").status_code == 200
    assert client.post(url, {"action": "evidence", "evidence-summary": "After promotion", "evidence-source": "operator", "evidence-result": "New observation"}).status_code == 302
    action_url = url + "incident-action/"
    assert client.post(action_url, {"action": "ack", "expected_tier": "T2"}).status_code == 409
    assert client.post(action_url, {"action": "bad", "expected_tier": "T1"}).status_code == 400
    assert client.post(action_url, {"action": "ack", "expected_tier": "T1"}).status_code == 302
    assert client.post(action_url, {"action": "escalate", "expected_tier": "T1"}).status_code == 302
    row.refresh_from_db(); assert row.incident.current_tier == "T2"
    assert client.post(action_url, {"action": "resolve", "expected_tier": "T2", "reason": "Recovery verified"}).status_code == 302
    event = row.history.filter(kind="evidence_added").first()
    assert client.post(url, {"action": "curate", "curate-event": event.pk, "curate-section": "recovery"}).status_code == 302
    assert client.post(url, {"action": "rca", "rca-state": "final", "rca-summary": "Example summary", "rca-root_cause": "Example cause"}).status_code == 302
    assert client.post(url, {"action": "remediation", "remediation-title": "Add alert", "remediation-owner": actor.pk, "remediation-due_date": "2026-12-01"}).status_code == 302
    task = row.remediations.get()
    assert client.post(f"/ui/remediations/{task.pk}/update/", {"state": "done"}).status_code == 400
    assert client.post(f"/ui/remediations/{task.pk}/update/", {"state": "done", "verification": "Replay verified"}).status_code == 302
    assert b"Example cause" in client.get(url + "rca.md").content
    assert client.get(url + "?tab=rca").status_code == 200
    assert client.get("/ui/reports/?days=bad").status_code == 200
    assert client.get("/ui/reports/data/?days=bad").status_code == 400
    report = client.get("/ui/reports/data/?examples=1&customer=Example%20Customer").json()
    assert report["incidents"] == 1 and report["remediations_complete"] == 1
    assert client.get("/ui/reports/?examples=1").status_code == 200
    from rest_framework.test import APIClient
    api = APIClient()
    payload = {"title": "Supplier notice", "source": "supplier", "signal": "Investigate", "source_event_id": "api-1"}
    assert api.post("/api/signals/intake", payload, format="json").status_code == 401
    assert api.post("/api/signals/intake", {**payload, "signal": ""}, format="json", HTTP_X_WATCH_WEBHOOK_SECRET="test-secret").status_code == 400
    assert api.post("/api/signals/intake", payload, format="json", HTTP_X_WATCH_WEBHOOK_SECRET="test-secret").status_code == 201
    assert api.post("/api/signals/intake", payload, format="json", HTTP_X_WATCH_WEBHOOK_SECRET="test-secret").status_code == 200


def test_configuration_validates_policy_and_queries(actor, rule):
    client = Client(); client.force_login(actor)
    url = "/ui/automation/"
    for kind in ("policy", "rule", "dependency", "area", "invalid"):
        assert client.get(url + "?kind=" + kind).status_code == 200
    assert client.post(url, {"kind": "policy"}).status_code == 400
    args = {"kind": "dependency", "config-customer": "Example Customer", "config-service": "Catalog", "config-supplier": "Supplier A", "config-active": True}
    assert client.post(url, args).status_code == 302
    item = ServiceDependency.objects.get()
    assert client.get(url + f"?kind=dependency&item_id={item.pk}").status_code == 200
    rule_data = {"name": "Real check", "provider": "prometheus", "query": "up", "minimum_samples": 0, "comparator": "deviation", "threshold": "nan", "lookback_minutes": 0}
    form = DiagnosticRuleForm(rule_data)
    assert not form.is_valid() and {"minimum_samples", "sample_query", "threshold", "baseline_query", "lookback_minutes"}.issubset(form.errors)
    valid = {**rule_data, "minimum_samples": 1, "threshold": 20, "lookback_minutes": 15, "baseline_query": "baseline", "sample_query": "samples"}
    form = DiagnosticRuleForm(valid); assert form.is_valid(), form.errors
    form.save()
    form = PolicyForm({"name": "Policy", "active": True, "severity": "bad", "impact": "bad", "review_seconds": 0, "communication_sla_seconds": 0, "recipients": "bad"})
    assert not form.is_valid() and {"rules", "recipients", "severity", "impact", "review_seconds", "communication_sla_seconds"}.issubset(form.errors)
    form = PolicyForm({"name": "Policy", "active": True, "rules": [rule.pk], "severity": "P2", "impact": "moderate", "review_seconds": 300, "communication_sla_seconds": 900, "recipients": "ops@example.test"})
    assert form.is_valid(), form.errors
    policy = form.save(); assert PolicyForm(instance=policy).initial["recipients"] == "ops@example.test"


def test_ui_roles_sme_access_and_suppression(row, actor, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        promote(row, actor)
    specialist = User.objects.create_user("specialist")
    item = operations.engage(row.pk, user=actor, target=specialist, reason="Inspect regional errors", selected=[], request_id=uuid.uuid4())
    client = Client(); client.force_login(actor)
    url = f"/ui/assessments/{row.pk}/"
    endpoint = f"/ui/engagements/{item.pk}/respond/"
    assert client.post(endpoint, {"action": "ack"}).status_code == 403
    client.force_login(specialist)
    assert client.get(url).status_code == 200
    assert client.post(endpoint, {"action": "complete", "body": "Early"}).status_code == 400
    assert client.post(endpoint, {"action": "ack"}).status_code == 302
    assert client.post(endpoint, {"action": "complete", "body": "Pool stabilized"}).status_code == 302
    assert client.post(url, {"action": "comment", "comment-body": "Specialist context"}).status_code == 302
    assert client.post(url, {"action": "claim", "claim-reason": "SME is not incident owner"}).status_code == 403
    communication = mail(row)
    mail_url = f"/ui/communications/{communication.pk}/"
    assert client.post(mail_url, {"action": "send", "expected_version": 1}).status_code == 403
    assert client.get("/ui/automation/").status_code == 403
    assert client.post(url + "incident-action/", {"action": "ack", "expected_tier": "T1"}).status_code == 403
    task = operations.remediation(row.pk, user=actor, title="Add alert", owner=actor, due_date=timezone.now().date())
    assert client.post(f"/ui/remediations/{task.pk}/update/", {"state": "done", "verification": "Not owner"}).status_code == 403
    client.force_login(actor)
    assert client.post(mail_url, {"action": "suppress", "expected_version": 1, "reason": "Duplicate"}).status_code == 302
    assert client.post(url, {}).status_code == 400
    assert client.post(url, {"action": "email", "email-subject": ""}).status_code == 400


def test_deadline_escalates_tiers_without_browser(row, actor, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        incident = promote(row, actor)
    for expected in ("T2", "T3", "T3"):
        Incident.objects.filter(pk=incident.pk).update(sla_deadline_at=timezone.now()-timedelta(seconds=1))
        with django_capture_on_commit_callbacks(execute=True):
            cadence.tick()
        incident.refresh_from_db(); assert incident.current_tier == expected
    assert incident.sla_deadline_at is None
    assert row.history.filter(kind="escalation_exhausted").count() == 1
    assert row.history.filter(kind="lifecycle_transition").count() == 2


def test_local_scenarios_are_repeatable_and_do_not_send_external_notifications(settings):
    settings.DEBUG = True
    for username, tier in [("t2a", "T2"), ("t3a", "T3")]:
        user = User.objects.create_user(username); user.groups.add(Group.objects.get_or_create(name=tier)[0])
    with patch("incidents.notify.send", side_effect=AssertionError("external notification")):
        call_command("seed_operations", stdout=StringIO())
        call_command("seed_operations", stdout=StringIO())
    assert Assessment.objects.count() == 4 and Incident.objects.count() == 2
    assert Assessment.objects.filter(status="no_issue").count() == 1
    assert RemediationTask.objects.count() == 1
    assert OutboxMessage.objects.count() == 1


def test_append_only_history_cannot_be_overwritten_through_explicit_primary_key_or_upsert(row):
    from django.db import IntegrityError, transaction
    from incidents.models import AssessmentEvent
    event = row.history.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        AssessmentEvent(pk=event.pk, assessment=row, kind="forged", actor="operator", body="Overwrite").save()
    event.refresh_from_db(); assert event.kind == "signal_received"
    with pytest.raises(ValueError, match="upserted"):
        AssessmentEvent.objects.bulk_create([AssessmentEvent(pk=event.pk, assessment=row, kind="forged", actor="operator", body="Overwrite")], update_conflicts=True, update_fields=["body"], unique_fields=["pk"])
    item = mail(row)
    revision = item.current
    with pytest.raises(IntegrityError), transaction.atomic():
        CommunicationRevision(pk=revision.pk, communication=item, version=1, subject="Overwrite", body="Overwrite", recipients=[]).save()
    revision.refresh_from_db(); assert revision.subject == "Example update"


def test_each_tier_deadline_is_audited_and_token_retries_do_not_extend_it(row, actor, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        incident = promote(row, actor)
    incident.refresh_from_db()
    before = incident.sla_deadline_at
    services.record_tier_token(incident.pk, "T1", incident.current_task_token)
    incident.refresh_from_db(); assert incident.sla_deadline_at == before
    services.escalate(incident.pk, actor=str(actor.pk), reason="Higher tier required")
    timers = list(row.history.filter(kind="tier_timer_started"))
    assert [e.data["tier"] for e in timers] == ["T1", "T2"]
    assert all(e.data["deadline"] for e in timers)
    assert all("token" not in e.data for e in timers)


def test_note_and_photos_are_one_source_event_and_dashboard_links_are_safe(row, actor):
    client = Client(); client.force_login(actor)
    url = f"/ui/assessments/{row.pk}/"
    body = 'Dashboard https://dashboards.example.test/d/air?region=example — errors recovered. <script>alert(1)</script>'
    before = row.history.count()
    result = client.post(url, {"action": "comment", "comment-body": body, "comment-photos": [SimpleUploadedFile("air.png", b"\x89PNG\r\n\x1a\nexample"), SimpleUploadedFile("region.jpg", b"\xff\xd8\xffexample")]})
    assert result.status_code == 302
    event = row.history.get(kind="comment")
    assert row.history.count() == before + 1 and event.body == body
    assert len(event.data["attachments"]) == row.attachments.count() == 2
    assert all(len(photo["sha256"]) == 64 for photo in event.data["attachments"])
    page = client.get(url).content.decode()
    assert 'href="https://dashboards.example.test/d/air?region=example"' in page
    assert '<script>alert(1)</script>' not in page and '&lt;script&gt;alert(1)&lt;/script&gt;' in page
    assert 'Open full-size air.png' in page and 'Download region.jpg' in page
    evidence = client.get(url + '?tab=evidence').content.decode()
    assert 'air.png' in evidence and 'errors recovered' in evidence
    for photo in event.data["attachments"]:
        response = client.get(f"/ui/evidence/{photo['attachment_id']}/?inline=1")
        assert response.status_code == 200 and response["Content-Type"].startswith("image/")


def test_bad_photo_rolls_back_the_entire_note_without_orphaned_files(row, actor):
    before = row.history.count()
    with pytest.raises(ValueError, match="Photos must"):
        operations.add_history_note(row.pk, user=actor, body="Keep this text if photos fail", photos=[SimpleUploadedFile("valid.png", b"\x89PNG\r\n\x1a\nexample"), SimpleUploadedFile("not-a-photo.png", b"plain text")])
    assert row.history.count() == before and row.attachments.count() == 0
    client = Client(); client.force_login(actor)
    response = client.post(f"/ui/assessments/{row.pk}/", {"action": "comment", "comment-body": "Retained operator note", "comment-photos": SimpleUploadedFile("bad.png", b"plain text")})
    assert response.status_code == 400 and b"Retained operator note" in response.content
    assert row.history.count() == before and row.attachments.count() == 0


def test_photo_only_notes_and_upload_limits(row, actor):
    from incidents.operations_forms import PhotoNoteForm
    event = operations.add_history_note(row.pk, user=actor, photos=[SimpleUploadedFile("photo.webp", b"RIFFabcdWEBPexample")])
    assert event.body == "Added photos." and len(event.data["attachments"]) == 1
    assert not PhotoNoteForm({"body": " "}).is_valid()
    assert not PhotoNoteForm({}, {"photos": [SimpleUploadedFile(f"photo-{i}.png", b"\x89PNG\r\n\x1a\nexample") for i in range(9)]}).is_valid()
    assert not PhotoNoteForm({}, {"photos": [SimpleUploadedFile("large.png", b"x" * (5 * 1024 * 1024 + 1))]}).is_valid()
    with pytest.raises(ValueError, match="8 photos"):
        operations.add_history_note(row.pk, user=actor, photos=[None]*9)
    with pytest.raises(ValueError, match="Add a note"):
        operations.add_history_note(row.pk, user=actor)
    client = Client(); client.force_login(actor)
    assert client.post(f"/ui/assessments/{row.pk}/", {"action": "comment", "comment-photos": SimpleUploadedFile("photo.png", b"\x89PNG\r\n\x1a\nexample")}).status_code == 302


def test_note_photos_are_preserved_in_curated_rca_export(row, actor, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        promote(row, actor)
    event = operations.add_history_note(row.pk, user=actor, body="Dashboard screenshot establishes regional impact", photos=[SimpleUploadedFile("dashboard.png", b"\x89PNG\r\n\x1a\nexample")])
    operations.curate(row.pk, user=actor, event=event, section="impact")
    export = operations.rca_markdown(row)
    assert 'Photo: dashboard.png' in export
    assert event.data["attachments"][0]["attachment_id"] in export
    assert event.data["attachments"][0]["sha256"] in export
