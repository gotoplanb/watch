"""Unit tests for service-layer branches not covered by the lifecycle tests:
record_tier_token (ADR-007/010) and the resolved-incident no-ops (ADR-001)."""
import pytest

from incidents import services
from incidents.models import Incident, Status, Tier


def _incident(**kw):
    data = dict(source="sumo", payload={}, title="t", dedupe_key="rt", current_tier=Tier.T1)
    data.update(kw)
    return Incident.objects.create(**data)


@pytest.mark.django_db
def test_record_tier_token_sets_token_tier_and_deadline():
    inc = _incident(dedupe_key="rt1")
    services.record_tier_token(inc.id, Tier.T2, "tok-xyz", sla_seconds=120)
    inc.refresh_from_db()
    assert inc.current_tier == Tier.T2
    assert inc.current_task_token == "tok-xyz"
    assert inc.sla_deadline_at is not None


@pytest.mark.django_db
def test_record_tier_token_defaults_sla_from_settings():
    inc = _incident(dedupe_key="rt2")
    services.record_tier_token(inc.id, Tier.T1, "tok")  # sla_seconds=None -> settings
    inc.refresh_from_db()
    assert inc.sla_deadline_at is not None


@pytest.mark.django_db
def test_record_tier_token_noop_when_resolved():
    inc = _incident(dedupe_key="rt3", status=Status.RESOLVED)
    services.record_tier_token(inc.id, Tier.T2, "tok")
    inc.refresh_from_db()
    assert inc.current_task_token == ""  # untouched; terminal


@pytest.mark.django_db
def test_escalate_noop_when_resolved():
    inc = _incident(dedupe_key="rt4", status=Status.RESOLVED, current_tier=Tier.T1)
    services.escalate(inc.id, actor="1")
    inc.refresh_from_db()
    assert inc.current_tier == Tier.T1 and inc.transitions.count() == 0


@pytest.mark.django_db
def test_acknowledge_noop_when_resolved():
    inc = _incident(dedupe_key="rt5", status=Status.RESOLVED)
    services.acknowledge(inc.id, actor="1")
    inc.refresh_from_db()
    assert inc.acknowledged_at is None and inc.transitions.count() == 0
