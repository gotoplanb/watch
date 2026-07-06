"""Escalation paging hook (ADR-013): pages the on-call on a real tier entry (new T1 / escalate),
never on ack/resolve; per-user topic with tier fallback; gated by the paging_enabled rollout; a
best-effort audit Page row per attempt."""
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from incidents import flags, notify, services
from incidents.models import Incident, OnCallShift, Page, PageStatus, Status, Tier


class FakeNotifier:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, topic, title, message, priority="default", tags=None):
        if self.fail:
            raise RuntimeError("ntfy down")
        self.sent.append({"topic": topic, "title": title, "message": message})


@pytest.fixture
def notifier():
    f = FakeNotifier()
    notify.set_provider_for_tests(f)
    yield f
    notify.set_provider_for_tests(None)


@pytest.fixture(autouse=True)
def _reset_flags():
    yield
    flags.set_provider_for_tests(None)


def _paging(mode="on"):
    flags.set_provider_for_tests(flags.InMemoryProvider({"paging_enabled": mode}))


def _incident(tier=Tier.T1):
    return Incident.objects.create(title="disk full", source="sumo", dedupe_key="d1", current_tier=tier)


@pytest.mark.django_db
def test_pages_the_on_call_user(settings, notifier):
    settings.PAGING_ENV = "test"
    _paging("on")
    user = get_user_model().objects.create_user("t2a")
    now = timezone.now()
    OnCallShift.objects.create(tier="T2", user=user, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1))
    inc = _incident("T2")
    services._page(inc, "T2")
    assert notifier.sent[0]["topic"] == f"watch-test-user-{user.id}"
    page = Page.objects.get()
    assert page.status == PageStatus.SENT and page.target == user and page.tier == "T2"


@pytest.mark.django_db
def test_falls_back_to_tier_topic_on_rota_gap(settings, notifier):
    settings.PAGING_ENV = "test"
    _paging("on")
    services._page(_incident("T3"), "T3")  # no shift for T3
    assert notifier.sent[0]["topic"] == "watch-test-tier-T3"
    page = Page.objects.get()
    assert page.target is None and page.status == PageStatus.SENT


@pytest.mark.django_db
def test_no_page_when_rollout_off(notifier):
    _paging("off")
    services._page(_incident(), "T1")
    assert notifier.sent == [] and Page.objects.count() == 0


@pytest.mark.django_db
def test_sample_rollout_keyed_on_incident(settings, notifier):
    settings.PAGING_ENV = "test"
    _paging("sample:1")  # always in
    services._page(_incident(), "T1")
    assert Page.objects.count() == 1


@pytest.mark.django_db
def test_records_failed_audit_when_notify_fails(settings):
    settings.PAGING_ENV = "test"
    _paging("on")
    notify.set_provider_for_tests(FakeNotifier(fail=True))
    try:
        services._page(_incident(), "T1")
        page = Page.objects.get()
        assert page.status == PageStatus.FAILED and "ntfy down" in page.error
    finally:
        notify.set_provider_for_tests(None)


@pytest.mark.django_db
def test_escalate_pages_new_tier(settings, notifier, django_capture_on_commit_callbacks):
    settings.PAGING_ENV = "test"
    _paging("on")
    inc = Incident.objects.create(title="x", source="s", dedupe_key="e1", current_tier=Tier.T1, status=Status.OPEN)
    with django_capture_on_commit_callbacks(execute=True):
        services.escalate(inc.id, actor="alice", reason="test")
    assert Page.objects.filter(incident=inc, tier="T2").exists()


def test_paging_topic_plain_without_secret(settings):
    settings.PAGING_ENV = "test"
    settings.NTFY_TOPIC_SECRET = ""
    assert services.paging_topic("user", 3) == "watch-test-user-3"
    assert services.paging_topic("tier", "T2") == "watch-test-tier-T2"


def test_paging_topic_hmac_suffix_with_secret(settings):
    settings.PAGING_ENV = "test"
    settings.NTFY_TOPIC_SECRET = "s3kret"
    topic = services.paging_topic("user", 3)
    # Keeps the human-readable base, adds a 12-hex HMAC suffix; the raw secret never appears.
    assert topic.startswith("watch-test-user-3-") and "s3kret" not in topic
    suffix = topic.rsplit("-", 1)[1]
    assert len(suffix) == 12 and all(c in "0123456789abcdef" for c in suffix)
    # Deterministic per identity, distinct across identities and kinds.
    assert services.paging_topic("user", 3) == topic
    assert services.paging_topic("user", 4) != topic
    assert services.paging_topic("tier", "T1") != services.paging_topic("user", "T1")


@pytest.mark.django_db
def test_page_uses_secret_suffixed_topic(settings, notifier):
    settings.PAGING_ENV = "test"
    settings.NTFY_TOPIC_SECRET = "s3kret"
    _paging("on")
    user = get_user_model().objects.create_user("t2a")
    now = timezone.now()
    OnCallShift.objects.create(tier="T2", user=user, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1))
    services._page(_incident("T2"), "T2")
    # The user topic mixes the user's rotation seed (ADR-030).
    from incidents import apikeys
    assert notifier.sent[0]["topic"] == services.paging_topic("user", user.id, seed=apikeys.seed_for(user))
    assert notifier.sent[0]["topic"].startswith(f"watch-test-user-{user.id}-")


@pytest.mark.django_db
def test_acknowledge_does_not_page(notifier, django_capture_on_commit_callbacks):
    _paging("on")
    inc = Incident.objects.create(title="x", source="s", dedupe_key="a1", current_tier=Tier.T1, status=Status.OPEN)
    with django_capture_on_commit_callbacks(execute=True):
        services.acknowledge(inc.id, actor="alice")
    assert Page.objects.count() == 0 and notifier.sent == []
