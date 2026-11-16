"""Planned maintenance windows (ADR-064, #66)."""
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, User
from django.utils import timezone
from rest_framework.test import APIClient

from incidents import automation, maintenance
from incidents.models import (
    Assessment, AssessmentPolicy, DiagnosticRule, MaintenanceWindow, ServiceDependency,
)

pytestmark = pytest.mark.django_db


def window(**scope):
    now = timezone.now()
    return MaintenanceWindow.objects.create(starts_at=now - timedelta(minutes=5),
                                            ends_at=now + timedelta(hours=1), reason="Square upgrade",
                                            created_by="t1a", **scope)


def signal(**scope):
    data = {"title": "checkout errors", "source": "sumo", "signal": "5xx", "customer": "A",
            "service": "checkout", "payload": {"fixture": True}, **scope}
    return [routed[0] for routed in automation.receive_signal(**data)]


@pytest.fixture
def depends():
    ServiceDependency.objects.create(customer="A", service="checkout", supplier="Square", active=True)


def test_supplier_window_covers_our_own_alert_on_a_dependent_service(depends):
    w = window(supplier="Square")
    [row] = signal()
    note = row.history.get(kind="maintenance_window")
    assert "Square upgrade" in note.body
    assert note.data["window_id"] == w.pk
    signal(source_event_id="again", title="more errors")  # attaches; annotated once per window
    assert row.history.filter(kind="maintenance_window").count() == 1
    assert "Square" in str(w)


@pytest.mark.parametrize("scope, covered", [
    ({"customer": "A", "service": "checkout"}, True),
    ({"customer": "A"}, True),
    ({"service": "checkout"}, True),
    ({"customer": "B"}, False),
    ({"supplier": "GitHub"}, False),
    ({}, False),  # a window scoped to nothing never matches
])
def test_scope_matching(scope, covered):
    window(**scope)
    [row] = signal()
    assert (maintenance.active_for(row) is not None) is covered


def test_expired_or_future_windows_dont_apply():
    now = timezone.now()
    MaintenanceWindow.objects.create(customer="A", starts_at=now + timedelta(hours=1),
                                     ends_at=now + timedelta(hours=2), reason="later", created_by="x")
    MaintenanceWindow.objects.create(customer="A", starts_at=now - timedelta(hours=2),
                                     ends_at=now - timedelta(hours=1), reason="done", created_by="x")
    [row] = signal()
    assert maintenance.active_for(row) is None
    assert not row.history.filter(kind="maintenance_window").exists()


def test_degradation_inside_a_window_monitors_and_rechecks_after_it(depends, settings,
                                                                   django_capture_on_commit_callbacks):
    settings.DEBUG = True
    rule = DiagnosticRule.objects.create(name="errors", provider="fixture", query="q", comparator="above",
                                         threshold=2, minimum_samples=100,
                                         fixture_result={"value": 6, "samples": 800})
    policy = AssessmentPolicy.objects.create(name="p", active=True, customer="A", service="checkout",
                                             auto_promote=True)
    policy.rules.add(rule)
    w = window(supplier="Square")
    [row] = signal()
    with django_capture_on_commit_callbacks(execute=True):
        automation.run(row.pk)
    row.refresh_from_db()
    assert (row.status, row.incident_id) == ("monitoring", None)
    assert row.next_recheck_at == w.ends_at + maintenance.RECHECK_AFTER
    assert "promotion held" in row.history.get(kind="monitoring_started").body


@pytest.fixture
def responder():
    user = User.objects.create_user("t1a")
    user.groups.add(Group.objects.get_or_create(name="T1")[0])
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_api_plans_and_lists_windows(responder):
    ends = (timezone.now() + timedelta(hours=2)).isoformat()
    made = responder.post("/api/maintenance/", {"supplier": "Square", "ends_at": ends,
                                                "reason": "Square upgrade"}, format="json")
    assert made.status_code == 201
    assert made.json()["created_by"] == "t1a"
    assert [w["supplier"] for w in responder.get("/api/maintenance/").json()] == ["Square"]


def test_api_validates_windows(responder):
    start = timezone.now()
    bad = responder.post("/api/maintenance/", {"starts_at": "soon", "ends_at": "later"}, format="json")
    assert set(bad.json()["errors"]) == {"scope", "starts_at", "ends_at", "reason"}
    too_long = responder.post("/api/maintenance/", {
        "customer": "A", "reason": "r", "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(days=8)).isoformat()}, format="json")
    assert "ends_at" in too_long.json()["errors"]
    nobody = APIClient()
    nobody.force_authenticate(User.objects.create_user("viewer"))
    assert nobody.post("/api/maintenance/", {}, format="json").status_code == 403
