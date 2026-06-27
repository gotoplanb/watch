"""Unit tests for the on-call schedule (ADR-012): current_on_call resolution,
auto-assignment on tier entry, and the schedule UI."""
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from incidents import services
from incidents.intake import create_incident_idempotent
from incidents.models import Incident, OnCallShift, Tier


def _user(username):
    return User.objects.create(username=username)


def _shift(tier, user, start_offset_h, end_offset_h):
    now = timezone.now()
    return OnCallShift.objects.create(
        tier=tier, user=user,
        starts_at=now + timedelta(hours=start_offset_h),
        ends_at=now + timedelta(hours=end_offset_h),
    )


def _incident(tier=Tier.T1, **kw):
    data = dict(source="sumo", payload={}, title="t",
                dedupe_key=f"sc-{tier}-{Incident.objects.count()}", current_tier=tier)
    data.update(kw)
    return Incident.objects.create(**data)


@pytest.mark.django_db
def test_current_on_call_active_and_gap():
    u = _user("oncall1")
    _shift(Tier.T1, u, -1, 1)  # covers now
    assert services.current_on_call(Tier.T1).user == u
    assert services.on_call_user(Tier.T1) == u
    assert services.current_on_call(Tier.T2) is None  # gap
    assert services.on_call_user(Tier.T2) is None


@pytest.mark.django_db
def test_overlapping_shifts_most_recent_start_wins():
    early, late = _user("early"), _user("late")
    _shift(Tier.T1, early, -3, 3)
    _shift(Tier.T1, late, -1, 3)  # starts later, also covers now
    assert services.current_on_call(Tier.T1).user == late


@pytest.mark.django_db
def test_escalate_assigns_new_tier_on_call():
    u2 = _user("t2oncall")
    _shift(Tier.T2, u2, -1, 1)
    inc = _incident(Tier.T1)
    services.escalate(inc.id, actor="9")
    inc.refresh_from_db()
    assert inc.current_tier == Tier.T2 and inc.assignee == u2


@pytest.mark.django_db
def test_escalate_into_gap_leaves_unassigned():
    inc = _incident(Tier.T1, assignee=_user("prev"))
    services.escalate(inc.id, actor="9")  # no T2 shift
    inc.refresh_from_db()
    assert inc.current_tier == Tier.T2 and inc.assignee is None


@pytest.mark.django_db
def test_record_tier_token_assigns_on_call():
    u3 = _user("t3oncall")
    _shift(Tier.T3, u3, -1, 1)
    inc = _incident(Tier.T2)
    services.record_tier_token(inc.id, Tier.T3, "tok")
    inc.refresh_from_db()
    assert inc.assignee == u3


@pytest.mark.django_db
def test_intake_assigns_t1_on_call():
    u1 = _user("t1oncall")
    _shift(Tier.T1, u1, -1, 1)
    inc, created = create_incident_idempotent(source="sumo", payload={"h": "x"},
                                              title="t", source_event_id="sc-intake")
    assert created and inc.assignee == u1


@pytest.mark.django_db
def test_oncallshift_str():
    s = _shift(Tier.T1, _user("s1"), -1, 1)
    assert "T1" in str(s) and "s1" in str(s)


# --- UI ---

@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
def test_schedule_requires_login(client):
    resp = client.get("/ui/schedule/")
    assert resp.status_code == 302 and "/api-auth/login/" in resp["Location"]


@pytest.mark.django_db
def test_schedule_page_shows_on_call(client):
    u = _user("viewer")
    _shift(Tier.T1, u, -1, 1)
    client.force_login(u)
    resp = client.get("/ui/schedule/")
    assert resp.status_code == 200 and b"on-call now" in resp.content and b"viewer" in resp.content


@pytest.mark.django_db
def test_add_shift_creates_and_renders(client):
    u = _user("scheduler")
    client.force_login(u)
    now = timezone.now()
    resp = client.post("/ui/schedule/shift/", {
        "tier": "T2", "user": str(u.id),
        "starts_at": now.strftime("%Y-%m-%dT%H:%M"),
        "ends_at": (now + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M"),
    })
    assert resp.status_code == 200
    assert OnCallShift.objects.filter(tier="T2", user=u).count() == 1


@pytest.mark.django_db
def test_add_shift_rejects_invalid(client):
    u = _user("scheduler2")
    client.force_login(u)
    # Missing user + end before start -> no shift created.
    resp = client.post("/ui/schedule/shift/", {"tier": "T2", "user": "", "starts_at": "", "ends_at": ""})
    assert resp.status_code == 200 and OnCallShift.objects.count() == 0
