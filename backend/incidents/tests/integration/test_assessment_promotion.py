"""Real Postgres locks must serialize delivery and promotion races."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections

from incidents import assessments
from incidents.models import Assessment, Incident

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _race(fn):
    barrier = Barrier(2)

    def worker():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return fn()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        return [future.result(timeout=20) for future in futures]


def test_concurrent_delivery_has_one_assessment():
    rows = _race(lambda: assessments.receive(title="Concurrent signal", source="test",
                                            signal="Check the checkout journey", source_event_id="race-1"))
    assert rows[0][0].pk == rows[1][0].pk
    assert sorted(created for _, created in rows) == [False, True]
    assert Assessment.objects.count() == 1
    assert Assessment.objects.get().history.filter(kind="signal_received").count() == 1


def test_concurrent_promotion_has_one_incident_and_one_engine_start(monkeypatch, settings):
    row, _ = assessments.receive(title="Confirmed failure", source="test", signal="Checkout fails")
    assessments.add_evidence(row.pk, actor="system", summary="Journey failure", result="Repeated failures in a 15-minute observation")
    assessments.conclude(row.pk, actor="system", verdict="degraded", confidence="high", reason="Customer journey fails")
    from incidents.models import WebhookDelivery, WebhookSubscription
    settings.WEBHOOKS_LOCAL_MODE = False
    WebhookSubscription.objects.create(url="https://receiver.example.test/hook", secret="test", event_types=["incident.created"])
    monkeypatch.setattr("incidents.events.queue.enqueue", lambda *args: None)
    starts = []

    def start(incident):
        starts.append(incident.pk)
        return f"local:{incident.pk}"

    monkeypatch.setattr("incidents.escalation.start_escalation", start)
    results = _race(lambda: assessments.promote(row.pk, actor="system", reason="Confirmed impact"))
    assert results[0][0].pk == results[1][0].pk
    assert sorted(created for _, created in results) == [False, True]
    assert Incident.objects.count() == 1 and len(starts) == 1
    assert row.history.filter(kind="incident_promoted").count() == 1
    delivery = WebhookDelivery.objects.get(event_type="incident.created")
    assert delivery.payload["data"]["incident_id"] == str(results[0][0].pk)


def test_concurrent_automatic_delivery_has_one_receipt_and_one_history_event():
    from datetime import timedelta
    from django.utils import timezone
    from incidents import communications
    from incidents.models import Communication, OutboxMessage
    row, _ = assessments.receive(title="Deadline race", source="test", signal="Customer update")
    mail = communications.draft(row.pk, actor="system", subject="Synthetic update", body="Confirmed facts", recipients=["ops@example.test"], auto_send=True)
    Communication.objects.filter(pk=mail.pk).update(review_deadline=timezone.now()-timedelta(seconds=1))
    results = _race(lambda: communications.send(mail.pk, actor="system:deadline", automatic=True))
    assert all(result.state == "sent" for result in results)
    assert OutboxMessage.objects.count() == 1
    assert row.history.filter(kind="communication_auto_sent").count() == 1
    assert row.history.filter(kind="review_window_expired").count() == 1


def test_edit_and_send_serialize_without_sending_an_unreviewed_revision():
    from incidents import communications
    from incidents.models import Communication, OutboxMessage
    row, _ = assessments.receive(title="Editor race", source="test", signal="Customer update")
    mail = communications.draft(row.pk, actor="operator", subject="Original", body="Original facts", recipients=["ops@example.test"])
    barrier = Barrier(2)
    def worker(kind):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                if kind == "send":
                    return communications.send(mail.pk, actor="operator", expected_version=1).state
                return communications.edit(mail.pk, actor="operator", expected_version=1, subject="Edited", body="Reviewed facts", recipients=["ops@example.test"], auto_send=True).revision
            except assessments.AssessmentConflict:
                return "conflict"
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, kind) for kind in ("send", "edit")]
        results = [future.result(timeout=20) for future in futures]
    mail.refresh_from_db()
    assert "conflict" in results
    if mail.state == "sent":
        assert mail.revision == 1 and OutboxMessage.objects.get().revision.subject == "Original"
    else:
        assert mail.revision == 2 and not OutboxMessage.objects.exists()


def test_suppression_and_send_cannot_both_commit():
    from incidents import communications
    from incidents.models import OutboxMessage
    row, _ = assessments.receive(title="Suppression race", source="test", signal="Customer update")
    mail = communications.draft(row.pk, actor="operator", subject="Original", body="Original facts", recipients=["ops@example.test"])
    barrier = Barrier(2)
    def worker(kind):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                if kind == "send":
                    return communications.send(mail.pk, actor="operator", expected_version=1).state
                return communications.suppress(mail.pk, actor="operator", expected_version=1, reason="Duplicate notice").state
            except assessments.AssessmentConflict:
                return "conflict"
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, kind) for kind in ("send", "suppress")]
        results = [future.result(timeout=20) for future in futures]
    assert "conflict" in results
    mail.refresh_from_db()
    assert OutboxMessage.objects.count() == (1 if mail.state == "sent" else 0)


def test_concurrent_due_engine_retries_do_not_double_attempt_or_append_failures(monkeypatch):
    from django.utils import timezone
    calls = []
    def failing_start(incident):
        calls.append(incident.pk)
        raise RuntimeError("Synthetic engine outage")
    monkeypatch.setattr("incidents.escalation.start_escalation", failing_start)
    row, _ = assessments.receive(title="Synthetic engine retry", source="test", signal="Confirm impact")
    assessments.promote(row.pk, actor="system:test", reason="Immediate handling", policy_reason="Synthetic immediate-incident policy")
    row.refresh_from_db()
    assert row.engine_start_failures == 1
    Assessment.objects.filter(pk=row.pk).update(engine_start_retry_at=timezone.now())
    _race(lambda: assessments.start_engine(row.pk))
    row.refresh_from_db()
    assert len(calls) == row.engine_start_failures == 2
    assert row.engine_start_retry_at > timezone.now()
    assert row.history.filter(kind="escalation_start_failed").count() == 1
