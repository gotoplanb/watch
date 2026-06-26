"""ADR-007: lifecycle transitions — idempotent, ack-doesn't-consume-token."""
import pytest

from incidents import services
from incidents.models import Incident, Status, Tier


def _incident(**kw):
    defaults = dict(source="sumo", payload={}, title="t", dedupe_key="k",
                    current_task_token="tok-T1")
    defaults.update(kw)
    return Incident.objects.create(**defaults)


@pytest.mark.django_db
def test_acknowledge_is_postgres_only_and_idempotent():
    inc = _incident()
    services.acknowledge(inc.id, actor="7")
    inc.refresh_from_db()
    assert inc.acknowledged_at is not None
    assert inc.current_task_token == "tok-T1"  # ACK never consumes the token
    # Re-acking is a no-op: still exactly one audit record.
    services.acknowledge(inc.id, actor="7")
    assert inc.transitions.count() == 1


@pytest.mark.django_db
def test_escalate_advances_tier_and_clears_ack():
    inc = _incident(current_tier=Tier.T1)
    services.acknowledge(inc.id, actor="7")
    services.escalate(inc.id, actor="7")
    inc.refresh_from_db()
    assert inc.current_tier == Tier.T2
    assert inc.acknowledged_at is None  # new tier, not yet acknowledged


@pytest.mark.django_db
def test_escalate_at_top_tier_is_noop():
    inc = _incident(current_tier=Tier.T3)
    services.escalate(inc.id, actor="7")
    inc.refresh_from_db()
    assert inc.current_tier == Tier.T3  # nowhere to go; ASL Fail state handles alarm


@pytest.mark.django_db
def test_resolve_is_terminal_and_consumes_token():
    inc = _incident(current_tier=Tier.T2)
    services.resolve(inc.id, actor="7", reason="fixed")
    inc.refresh_from_db()
    assert inc.status == Status.RESOLVED
    assert inc.current_task_token == ""  # consumed -> no zombie timer
    # Resolve-from-any-tier: audit records the tier it resolved at (T2).
    last = inc.transitions.last()
    assert last.to_status == Status.RESOLVED and last.to_tier == Tier.T2
    # Idempotent.
    services.resolve(inc.id, actor="7")
    assert inc.transitions.filter(to_status=Status.RESOLVED).count() == 1
