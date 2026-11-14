"""Regression coverage for the concrete lifecycle findings from PR review."""
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth.models import Group, User
from django.db import IntegrityError
from django.test import Client
from django.utils import timezone

from incidents import assessments, cadence, communications, services
from incidents.models import Assessment, Incident, OperationsHeartbeat, SmeEngagement, WebhookDelivery, WebhookSubscription

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator():
    user = User.objects.create_user("review-operator")
    user.groups.add(Group.objects.get_or_create(name="T2")[0])
    return user


@pytest.fixture
def case():
    row, _ = assessments.receive(title="Synthetic checkout concern", source="test", signal="Confirm checkout impact")
    assessments.add_evidence(row.pk, actor="system:test", summary="Failures confirmed", result="Invented diagnostic failure sample")
    assessments.conclude(row.pk, actor="system:test", verdict="degraded", confidence="high", reason="Confirmed by synthetic evidence")
    return row


def test_promotion_emits_created_once_before_later_lifecycle_events(case, operator, django_capture_on_commit_callbacks, settings):
    settings.WEBHOOKS_LOCAL_MODE = True
    WebhookSubscription.objects.create(url="https://receiver.example.test/hook", secret="test", active=True)
    response = Mock(ok=True, status_code=200)
    with patch("incidents.events.requests.post", return_value=response) as post:
        with django_capture_on_commit_callbacks(execute=True):
            incident, _ = assessments.promote(case.pk, actor=str(operator.pk), reason="Confirmed impact")
        assessments.promote(case.pk, actor=str(operator.pk), reason="Duplicate submit")
        services.escalate(incident.pk, actor=str(operator.pk), reason="Higher tier needed")
        services.resolve(incident.pk, actor=str(operator.pk), reason="Recovery verified")
    deliveries = list(WebhookDelivery.objects.order_by("created_at", "pk"))
    assert [row.event_type for row in deliveries] == ["incident.created", "incident.escalated", "incident.resolved"]
    assert deliveries[0].payload["data"] == {
        "incident_id": str(incident.pk), "number": incident.number, "title": incident.title, "source": incident.source,
    }
    assert post.call_count == 3


def test_exercise_promotion_preserves_no_dispatch_contract(case, settings):
    settings.DEBUG = True
    with patch("incidents.assessments.events.emit") as emit:
        assessments.promote(case.pk, actor="system:test", reason="Exercise", dispatch=False)
    emit.assert_not_called()


def test_failed_engine_start_is_logged_once_and_retries_back_off(case, django_capture_on_commit_callbacks):
    with patch("incidents.escalation.start_escalation", side_effect=RuntimeError("Unavailable")) as start:
        with django_capture_on_commit_callbacks(execute=True):
            assessments.promote(case.pk, actor="system:test", reason="Confirmed impact")
        case.refresh_from_db()
        first_retry = case.engine_start_retry_at
        assert case.engine_start_failures == start.call_count == 1
        assert 25 <= (first_retry - timezone.now()).total_seconds() <= 30
        for _ in range(4):
            cadence.tick()
        assert start.call_count == 1
        for expected_delay in (60, 120, 240, 480, 900, 900):
            case.refresh_from_db()
            due = case.engine_start_retry_at
            with patch("incidents.assessments.timezone.now", return_value=due):
                cadence.tick()
            case.refresh_from_db()
            assert case.engine_start_retry_at == due + timedelta(seconds=expected_delay)
        assert case.history.filter(kind="escalation_start_failed").count() == 1
    with patch("incidents.escalation.start_escalation", return_value="local:recovered") as start:
        assessments.start_engine(case.pk)
        start.assert_not_called()
        assessments.start_engine(case.pk, force=True)
        assessments.start_engine(case.pk, force=True)
        assert start.call_count == 1
    case.refresh_from_db()
    assert case.engine_start_retry_at is None
    assert case.incident.escalation_execution_arn == "local:recovered"
    assert case.history.get(kind="escalation_started").data["attempts"] == 8


def test_existing_failure_history_is_not_duplicated_after_upgrade(case, django_capture_on_commit_callbacks):
    with patch("incidents.escalation.start_escalation", side_effect=RuntimeError("Unavailable")):
        with django_capture_on_commit_callbacks(execute=True):
            assessments.promote(case.pk, actor="system:test", reason="Confirmed impact")
        # A pre-migration assessment has failure history and the new fields' defaults.
        Assessment.objects.filter(pk=case.pk).update(engine_start_failures=0, engine_start_retry_at=None)
        assessments.start_engine(case.pk)
    assert case.history.filter(kind="escalation_start_failed").count() == 1


def test_bad_communication_does_not_block_other_deadlines(case, operator, django_capture_on_commit_callbacks, settings):
    with django_capture_on_commit_callbacks(execute=True):
        incident, _ = assessments.promote(case.pk, actor=str(operator.pk), reason="Confirmed impact")
    message = communications.draft(case.pk, actor="system:test", subject="Example update", body="Synthetic update", recipients=["ops@example.test"], auto_send=True)
    type(message).objects.filter(pk=message.pk).update(review_deadline=timezone.now() - timedelta(seconds=1))
    Incident.objects.filter(pk=incident.pk).update(sla_deadline_at=timezone.now() - timedelta(seconds=1))
    handoff = SmeEngagement.objects.create(assessment=case, requested_by=operator, target=operator, reason="Inspect example", ack_deadline=timezone.now() - timedelta(seconds=1))
    settings.WATCH_DELIVERY_MODE = "unconfigured"
    with django_capture_on_commit_callbacks(execute=True):
        counts = cadence.tick()
    incident.refresh_from_db(); handoff.refresh_from_db(); message.refresh_from_db()
    assert incident.current_tier == "T2"
    assert handoff.state == "overdue"
    assert message.state == "review"
    assert message.sent_at is None
    assert counts["communications"] == 0
    assert counts["errors"] == 1
    assert counts["handoffs"] == counts["escalations"] == 1
    assert OperationsHeartbeat.objects.get(name="operations").detail["errors"] == 1


def test_failed_item_rolls_back_before_next_item_in_the_same_batch(case):
    other, _ = assessments.receive(title="Another synthetic concern", source="test", signal="Check this too")
    counts = {"errors": 0, "changed": 0}
    def action(pk):
        if pk == case.pk:
            Assessment.objects.create(pk=case.pk, number=case.number, dedupe_key=case.dedupe_key)
        Assessment.objects.filter(pk=pk).update(impact="low")
        return True
    cadence._sweep([case.pk, other.pk], action, counts, "changed")
    case.refresh_from_db(); other.refresh_from_db()
    assert case.impact == "unknown"
    assert other.impact == "low"
    assert counts == {"errors": 1, "changed": 1}


def test_lifecycle_actor_identity_survives_rename_and_resolves_at_display(case, operator, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        incident, _ = assessments.promote(case.pk, actor="system:test", reason="Confirmed impact")
    services.acknowledge(incident.pk, actor=str(operator.pk), reason="Taking ownership")
    event = case.history.get(kind="lifecycle_transition")
    assert event.actor == str(operator.pk)
    operator.username = "renamed-operator"
    operator.save(update_fields=["username"])
    client = Client(); client.force_login(operator)
    page = client.get(f"/ui/assessments/{case.pk}/").content.decode()
    assert "Lifecycle transition · renamed-operator" in page
    legacy_body = client.get(f"/ui/incidents/{incident.pk}/body/").content.decode()
    assert "Lifecycle transition · renamed-operator" in legacy_body
    timeline = services.timeline(incident)
    assert next(item["obj"] for item in timeline if item["obj"].pk == event.pk).actor_label == "renamed-operator"
    assert "renamed-operator" in services.rca_markdown(incident)
    event.refresh_from_db()
    assert event.actor == str(operator.pk)
