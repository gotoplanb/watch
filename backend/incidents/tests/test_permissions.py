"""ADR-008: authz — current-tier-role-or-above."""
import pytest
from django.contrib.auth.models import Group, User

from incidents.models import Incident, Tier
from incidents.permissions import can_act_on


def _user(*tiers):
    u = User.objects.create(username=f"u-{'-'.join(tiers) or 'none'}")
    for t in tiers:
        g, _ = Group.objects.get_or_create(name=t)
        u.groups.add(g)
    return u


def _incident(tier):
    return Incident.objects.create(source="s", payload={}, title="t", dedupe_key="k",
                                   current_tier=tier)


@pytest.mark.django_db
def test_exact_tier_can_act():
    assert can_act_on(_user("T1"), _incident(Tier.T1)) is True


@pytest.mark.django_db
def test_higher_tier_can_act_senior_override():
    assert can_act_on(_user("T3"), _incident(Tier.T1)) is True


@pytest.mark.django_db
def test_lower_tier_cannot_act():
    assert can_act_on(_user("T1"), _incident(Tier.T3)) is False


@pytest.mark.django_db
def test_no_tier_cannot_act():
    assert can_act_on(_user(), _incident(Tier.T1)) is False


@pytest.mark.django_db
def test_superuser_can_act():
    su = User.objects.create(username="root", is_superuser=True)
    assert can_act_on(su, _incident(Tier.T3)) is True
