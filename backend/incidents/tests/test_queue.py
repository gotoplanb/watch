"""Hermetic tests for the async queue seam + worker dispatch (ADR-025) — no boto3/SQS, both
provider branches and both job kinds. `set_provider_for_tests` captures enqueues; the `sqs`
branch is exercised with a fake boto3 client."""
from uuid import uuid4

import boto3
import pytest

from incidents import checks as checks_svc
from incidents import events, queue, trace_store
from incidents.models import (
    CheckStatus,
    CheckSubjectKind,
    DeliveryStatus,
    WebhookSubscription,
)


@pytest.fixture(autouse=True)
def _reset():
    yield
    queue.set_provider_for_tests(None)
    trace_store.set_provider_for_tests(None)


class Capture:
    def __init__(self):
        self.calls = []

    def __call__(self, kind, obj_id):
        self.calls.append((kind, str(obj_id)))


# --- enqueue ---

def test_enqueue_local_is_noop(settings):
    settings.QUEUE_PROVIDER = "local"
    queue.enqueue("check", "abc")  # no provider, no boto3 — must not raise


def test_enqueue_rejects_unknown_kind():
    with pytest.raises(ValueError):
        queue.enqueue("bogus", "1")


def test_enqueue_override_captures():
    cap = Capture()
    queue.set_provider_for_tests(cap)
    queue.enqueue("delivery", 7)
    assert cap.calls == [("delivery", "7")]


def test_enqueue_sqs_sends_message(settings, monkeypatch):
    settings.QUEUE_PROVIDER = "sqs"
    settings.WATCH_QUEUE_URL = "https://sqs.local/q"
    sent = {}

    class FakeClient:
        def send_message(self, QueueUrl, MessageBody):
            sent["url"] = QueueUrl
            sent["body"] = MessageBody

    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeClient())
    queue.enqueue("check", "id-9")
    assert sent["url"] == "https://sqs.local/q"
    assert '"kind": "check"' in sent["body"] and '"id": "id-9"' in sent["body"]


def test_enqueue_guarded_swallows_send_failure(settings, monkeypatch):
    settings.QUEUE_PROVIDER = "sqs"

    def boom(*a, **k):
        raise RuntimeError("sqs down")

    monkeypatch.setattr(boto3, "client", boom)
    queue.enqueue("check", "id-9")  # guarded — must not raise into the domain


# --- run_job dispatch ---

@pytest.mark.django_db
def test_run_job_check_runs_the_check():
    trace_store.set_provider_for_tests(type("F", (), {"find_error_spans": lambda *a: []})())
    c = checks_svc.create_check(subject_kind=CheckSubjectKind.SESSION, subject_raw="corr-1")
    queue.run_job("check", c.id)
    c.refresh_from_db()
    assert c.status == CheckStatus.DONE and c.verdict == "clean"


@pytest.mark.django_db
def test_run_job_delivery_posts(monkeypatch):
    posted = {}

    class Resp:
        status_code = 200
        ok = True

    def fake_post(url, data=None, headers=None, timeout=None):
        posted["url"] = url
        return Resp()

    monkeypatch.setattr(events.requests, "post", fake_post)
    sub = WebhookSubscription.objects.create(url="https://rx.local/h", secret="s", active=True)
    d = events._deliver(sub, {"event": "incident.created", "id": str(uuid4()), "at": "t", "data": {}})
    d.status = DeliveryStatus.PENDING  # simulate the cloud row awaiting the worker
    d.save(update_fields=["status"])
    queue.run_job("delivery", d.id)
    d.refresh_from_db()
    assert posted["url"] == "https://rx.local/h"
    assert d.status == DeliveryStatus.DELIVERED


@pytest.mark.django_db
def test_run_job_unknown_kind_raises():
    with pytest.raises(ValueError):
        queue.run_job("bogus", 1)


# --- cloud branches enqueue ---

@pytest.mark.django_db
def test_create_and_run_enqueues_in_cloud_mode(settings):
    settings.CHECKS_LOCAL_MODE = False
    cap = Capture()
    queue.set_provider_for_tests(cap)
    c = checks_svc.create_and_run(subject_kind=CheckSubjectKind.SESSION, subject_raw="x")
    assert c.status == CheckStatus.QUEUED
    assert cap.calls == [("check", str(c.id))]


@pytest.mark.django_db
def test_deliver_enqueues_and_skips_post_in_cloud_mode(settings, monkeypatch):
    settings.WEBHOOKS_LOCAL_MODE = False
    cap = Capture()
    queue.set_provider_for_tests(cap)

    def must_not_post(*a, **k):  # cloud mode must not POST inline
        raise AssertionError("cloud mode must not POST synchronously")

    monkeypatch.setattr(events.requests, "post", must_not_post)
    sub = WebhookSubscription.objects.create(url="https://rx.local/h", secret="s", active=True)
    d = events._deliver(sub, {"event": "incident.created", "id": str(uuid4()), "at": "t", "data": {}})
    assert d.status == DeliveryStatus.PENDING
    assert cap.calls == [("delivery", str(d.id))]
