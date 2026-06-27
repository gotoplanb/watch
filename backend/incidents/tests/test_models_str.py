"""__str__ smoke tests for admin/debug readability."""
import pytest

from incidents.models import Incident, Tier, Transition, next_tier


@pytest.mark.django_db
def test_incident_and_transition_str():
    inc = Incident.objects.create(source="sumo", payload={}, title="disk full",
                                  dedupe_key="s1", current_tier=Tier.T1)
    assert "disk full" in str(inc)
    tr = Transition.objects.create(incident=inc, from_status="OPEN", from_tier="T1",
                                   to_status="OPEN", to_tier="T2", actor="1")
    assert str(inc.id) in str(tr) and "T1" in str(tr)


def test_next_tier_progression():
    assert next_tier(Tier.T1) == Tier.T2
    assert next_tier(Tier.T2) == Tier.T3
    assert next_tier(Tier.T3) is None
