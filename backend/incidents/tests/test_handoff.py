"""Tier handoff briefs (ADR-040), hermetic: the deterministic stub, the escalate() hook (both
flag branches, auto vs manual actor phrasing), the soft-fail contract, provider dispatch, and
the newest-first detail rendering with the emphasized handoff card."""
import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from incidents import flags, handoff_ai, services
from incidents.models import EventType, Incident, Tier
from incidents.rca_ai import DraftResult


@pytest.fixture(autouse=True)
def _handoff_on():
    flags.set_provider_for_tests(flags.InMemoryProvider({services.HANDOFF_FLAG: True}))
    yield
    flags.set_provider_for_tests(None)


def _mk_incident(**kw):
    return Incident.objects.create(source="test", title="checkout latency", dedupe_key="h1", **kw)


def _mk_user(name, tier):
    user = User.objects.create_user(name, password="pw")
    group, _ = Group.objects.get_or_create(name=tier)
    user.groups.add(group)
    return user


BASE_CTX = {
    "from_tier": "T2", "to_tier": "T3", "auto": True, "actor_label": "the SLA clock",
    "reason": "", "source": "check", "triage": "real (internal/software)",
    "open_for": "42 min", "event_count": 3,
}


# --- the stub brief ---

def test_stub_brief_covers_the_three_questions(settings):
    settings.HANDOFF_AI_PROVIDER = "stub"
    result = handoff_ai.brief(dict(BASE_CTX), "history")
    assert "WHAT HAS HAPPENED" in result.text
    assert "WHY T3 IS ENGAGED" in result.text and "SLA clock expired" in result.text
    assert "WHAT T3 SHOULD DO NOW" in result.text and "shop foreman" in result.text
    # T3 has no higher tier — the brief must never instruct an impossible escalation
    assert "escalate" not in result.text.split("WHAT T3 SHOULD DO NOW")[1].lower()
    assert "buck stops here" in result.text
    assert "real (internal/software)" in result.text
    assert result.provider == "stub"


def test_stub_brief_manual_escalation_names_the_human(settings):
    settings.HANDOFF_AI_PROVIDER = "stub"
    ctx = dict(BASE_CTX, auto=False, actor_label="t2a", reason="beyond my diagnosis",
               to_tier="T3")
    result = handoff_ai.brief(ctx, "history")
    assert "t2a deliberately escalated" in result.text
    assert "beyond my diagnosis" in result.text


@pytest.mark.parametrize("provider,module_name", [("bedrock", "bedrock"), ("conduct", "conduct")])
def test_brief_dispatches_to_model_providers(settings, monkeypatch, provider, module_name):
    settings.HANDOFF_AI_PROVIDER = provider
    import importlib
    module = importlib.import_module(f"incidents.{module_name}")
    seen = {}

    def fake_draft(prompt, source):
        seen["prompt"] = prompt
        seen["source"] = source
        return DraftResult(text="BRIEF", provider=provider, model="m1")

    monkeypatch.setattr(module, "draft", fake_draft)
    result = handoff_ai.brief(dict(BASE_CTX), "THE HISTORY")
    assert result.text == "BRIEF" and result.model == "m1"
    assert "T3" in seen["prompt"] and "shop foreman" in seen["prompt"]  # tier + role in prompt
    assert "THE HISTORY" in seen["source"] and "SLA clock expired" in seen["source"]


def test_brief_unknown_provider_raises(settings):
    settings.HANDOFF_AI_PROVIDER = "gpt"
    with pytest.raises(handoff_ai.HandoffError, match="unknown HANDOFF_AI_PROVIDER"):
        handoff_ai.brief(dict(BASE_CTX), "h")


# --- the escalate() hook ---

@pytest.mark.django_db(transaction=True)
def test_escalate_posts_brief_as_newest_event(settings, django_capture_on_commit_callbacks):
    settings.HANDOFF_AI_PROVIDER = "stub"
    user = _mk_user("t2a", Tier.T2)
    incident = _mk_incident()
    with django_capture_on_commit_callbacks(execute=True):
        services.escalate(incident.id, actor=str(user.pk), reason="need the foreman")
    event = incident.events.filter(type=EventType.AI, data__kind="handoff").get()
    assert event.data["to_tier"] == "T2"
    assert "WHY T2 IS ENGAGED" in event.body and "t2a deliberately escalated" in event.body
    assert event.actor == "system:handoff"


@pytest.mark.django_db(transaction=True)
def test_auto_escalation_brief_blames_the_clock(settings, django_capture_on_commit_callbacks):
    settings.HANDOFF_AI_PROVIDER = "stub"
    incident = _mk_incident()
    from incidents.models import Transition
    with django_capture_on_commit_callbacks(execute=True):
        services.escalate(incident.id, actor=Transition.SYSTEM_ACTOR)
    event = incident.events.filter(data__kind="handoff").get()
    assert "SLA clock expired" in event.body


@pytest.mark.django_db(transaction=True)
def test_flag_off_no_brief(settings, django_capture_on_commit_callbacks):
    flags.set_provider_for_tests(flags.InMemoryProvider({services.HANDOFF_FLAG: False}))
    incident = _mk_incident()
    with django_capture_on_commit_callbacks(execute=True):
        services.escalate(incident.id, actor="1")
    assert not incident.events.filter(data__kind="handoff").exists()


@pytest.mark.django_db(transaction=True)
def test_brief_failure_never_breaks_escalation(settings, monkeypatch,
                                               django_capture_on_commit_callbacks):
    def boom(ctx, history):
        raise handoff_ai.HandoffError("model down")

    monkeypatch.setattr(handoff_ai, "brief", boom)
    incident = _mk_incident()
    with django_capture_on_commit_callbacks(execute=True):
        services.escalate(incident.id, actor="1")
    incident.refresh_from_db()
    assert incident.current_tier == Tier.T2          # escalation committed regardless
    assert not incident.events.filter(data__kind="handoff").exists()


# --- the detail page: newest-first, handoff card on top ---

@pytest.mark.django_db(transaction=True)
def test_detail_renders_brief_first(settings, django_capture_on_commit_callbacks):
    settings.HANDOFF_AI_PROVIDER = "stub"
    user = _mk_user("t3a", Tier.T3)
    incident = _mk_incident()
    services.add_note(incident, actor="t1a", body="looked at the dashboards early on")
    with django_capture_on_commit_callbacks(execute=True):
        services.escalate(incident.id, actor=str(user.pk), reason="beyond T1")
    client = Client()
    client.force_login(user)
    html = client.get(f"/ui/incidents/{incident.id}/").content.decode()
    assert "Handoff brief — T2 engaged" in html
    # newest-first: the brief appears before the earlier human note in the document
    assert html.index("Handoff brief") < html.index("looked at the dashboards early on")
