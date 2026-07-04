"""Hermetic tests for outbound event webhooks (ADR-023): the emit backbone (delivery records +
HMAC signing + local sync), the event-type filter, cloud-mode, the domain hooks (incident.*,
check.completed), and the /ui/webhooks surface. requests.post is faked — no network."""
import hashlib
import hmac
import json

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from incidents import checks as checks_svc
from incidents import events, services, trace_store
from incidents.intake import create_incident_idempotent
from incidents.models import (
    CheckSubjectKind,
    DeliveryStatus,
    Incident,
    Tier,
    WebhookDelivery,
    WebhookSubscription,
)


class FakePost:
    def __init__(self, status=200, raise_exc=None):
        self.status = status
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, url, data=None, headers=None, timeout=None):
        if self.raise_exc:
            raise self.raise_exc
        self.calls.append({"url": url, "data": data, "headers": headers})

        class Resp:
            status_code = self.status
            ok = 200 <= self.status < 300

        return Resp()


@pytest.fixture
def post(monkeypatch):
    fp = FakePost()
    monkeypatch.setattr(events.requests, "post", fp)
    return fp


@pytest.fixture
def client():
    return Client()


def _sub(**kw):
    return WebhookSubscription.objects.create(
        url=kw.get("url", "https://rx.example/hook"),
        secret=kw.get("secret", "sk"),
        event_types=kw.get("event_types", []),
        active=kw.get("active", True),
    )


# --- emit backbone ---

@pytest.mark.django_db
def test_emit_no_subscriptions_is_noop(post):
    events.emit("incident.escalated", {"x": 1})
    assert WebhookDelivery.objects.count() == 0 and post.calls == []


@pytest.mark.django_db
def test_emit_delivers_and_signs(post):
    sub = _sub(secret="topsecret")
    events.emit("incident.escalated", {"incident_id": "i1"})
    d = WebhookDelivery.objects.get()
    assert d.status == DeliveryStatus.DELIVERED and d.status_code == 200 and d.attempts == 1
    # signature is HMAC-SHA256 over the exact posted body
    call = post.calls[0]
    expected = hmac.new(b"topsecret", call["data"], hashlib.sha256).hexdigest()
    assert call["headers"]["X-Watch-Signature"] == f"sha256={expected}"
    assert call["headers"]["X-Watch-Event"] == "incident.escalated"
    body = json.loads(call["data"])
    assert body["event"] == "incident.escalated" and body["data"]["incident_id"] == "i1" and body["id"]


@pytest.mark.django_db
def test_emit_respects_event_type_filter(post):
    _sub(event_types=["incident.resolved"])
    events.emit("incident.escalated", {})       # not in filter
    assert WebhookDelivery.objects.count() == 0
    events.emit("incident.resolved", {})         # in filter
    assert WebhookDelivery.objects.count() == 1


@pytest.mark.django_db
def test_emit_skips_inactive(post):
    _sub(active=False)
    events.emit("incident.escalated", {})
    assert WebhookDelivery.objects.count() == 0


@pytest.mark.django_db
def test_delivery_marked_failed_on_error_status(monkeypatch):
    monkeypatch.setattr(events.requests, "post", FakePost(status=500))
    _sub()
    events.emit("incident.escalated", {})
    assert WebhookDelivery.objects.get().status == DeliveryStatus.FAILED


@pytest.mark.django_db
def test_delivery_marked_failed_on_exception(monkeypatch):
    monkeypatch.setattr(events.requests, "post", FakePost(raise_exc=events.requests.RequestException("down")))
    _sub()
    events.emit("incident.escalated", {})
    d = WebhookDelivery.objects.get()
    assert d.status == DeliveryStatus.FAILED and "down" in d.error


@pytest.mark.django_db
def test_cloud_mode_leaves_pending(settings, post):
    settings.WEBHOOKS_LOCAL_MODE = False
    _sub()
    events.emit("incident.escalated", {})
    assert WebhookDelivery.objects.get().status == DeliveryStatus.PENDING and post.calls == []


def test_subscription_matches():
    s = WebhookSubscription(event_types=[])
    assert s.matches("anything")                 # empty filter = all
    s.event_types = ["a", "b"]
    assert s.matches("a") and not s.matches("c")


# --- domain hooks (emitted from services, ADR-010) ---

def _incident(tier=Tier.T1, **kw):
    data = dict(source="sumo", payload={}, title="disk full",
                dedupe_key=f"ev-{tier}-{Incident.objects.count()}", current_tier=tier)
    data.update(kw)
    return Incident.objects.create(**data)


@pytest.mark.django_db
def test_escalate_and_resolve_emit(post):
    _sub()
    inc = _incident(Tier.T1)
    services.escalate(inc.id, actor="7")
    services.resolve(inc.id, actor="7")
    events_seen = set(WebhookDelivery.objects.values_list("event_type", flat=True))
    assert {"incident.escalated", "incident.resolved"} <= events_seen


@pytest.mark.django_db
def test_intake_create_emits(post):
    _sub()
    create_incident_idempotent(source="s", payload={}, title="t", source_event_id="e1")
    assert WebhookDelivery.objects.filter(event_type="incident.created").count() == 1


@pytest.mark.django_db
def test_check_completed_emits(post):
    _sub()
    trace_store.set_provider_for_tests(type("F", (), {"find_error_spans": lambda *a: []})())
    checks_svc.create_and_run(subject_kind=CheckSubjectKind.SESSION, subject_raw="x")
    trace_store.set_provider_for_tests(None)
    d = WebhookDelivery.objects.get(event_type="check.completed")
    assert d.payload["data"]["verdict"] == "clean"


# --- UI ---

def _user(username):
    return User.objects.create(username=username)


@pytest.mark.django_db
def test_ui_webhooks_list_and_add(client, post):
    client.force_login(_user("op"))
    resp = client.post("/ui/webhooks/add/",
                       {"url": "https://rx/hook", "secret": "sk", "event_types": "incident.escalated, check.completed"})
    assert resp.status_code == 302
    s = WebhookSubscription.objects.get()
    assert s.event_types == ["incident.escalated", "check.completed"]
    page = client.get("/ui/webhooks/")
    assert page.status_code == 200 and b"https://rx/hook" in page.content


@pytest.mark.django_db
def test_ui_add_subscription_requires_url_and_secret(client):
    client.force_login(_user("op2"))
    client.post("/ui/webhooks/add/", {"url": "", "secret": "sk"})
    assert WebhookSubscription.objects.count() == 0


@pytest.mark.django_db
def test_model_strs():
    s = _sub()
    assert "webhook ->" in str(s)
    d = WebhookDelivery.objects.create(subscription=s, event_type="incident.escalated",
                                       event_id="00000000-0000-0000-0000-000000000000", payload={})
    assert "incident.escalated" in str(d)
