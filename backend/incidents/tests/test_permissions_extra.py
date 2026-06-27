"""Edge cases for authz helpers + the DRF permission class (ADR-008)."""
import pytest
from django.contrib.auth.models import Group, User

from incidents.models import Incident, Tier
from incidents.permissions import CanActOnIncident, user_tier_rank


def test_user_tier_rank_none_user():
    assert user_tier_rank(None) == -1


def test_user_tier_rank_unauthenticated():
    class Anon:
        is_authenticated = False

    assert user_tier_rank(Anon()) == -1


@pytest.mark.django_db
def test_can_act_on_incident_permission_class():
    user = User.objects.create(username="p")
    user.groups.add(Group.objects.get_or_create(name="T2")[0])
    inc = Incident.objects.create(source="s", payload={}, title="t",
                                  dedupe_key="perm", current_tier=Tier.T1)

    class _Req:
        pass

    req = _Req()
    req.user = user
    assert CanActOnIncident().has_object_permission(req, None, inc) is True
