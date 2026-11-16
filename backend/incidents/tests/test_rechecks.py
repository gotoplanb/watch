"""Timed rechecks for monitoring assessments (ADR-061, #67)."""
from datetime import timedelta

import pytest
from django.utils import timezone

from incidents import assessments, automation, cadence
from incidents.models import Assessment, AssessmentPolicy, DiagnosticRule
from incidents.operations_forms import PolicyForm

pytestmark = pytest.mark.django_db


@pytest.fixture
def policy():
    rule = DiagnosticRule.objects.create(
        name="Checkout errors", provider="fixture", query="q", comparator="above", threshold=2,
        minimum_samples=100, fixture_result={"value": 0.2, "samples": 800})
    policy = AssessmentPolicy.objects.create(name="Checkout", active=True, customer="A",
                                             service="checkout", auto_close_no_issue=True)
    policy.rules.add(rule)
    return policy


def monitoring(customer="A", service="checkout"):
    row = assessments.receive(title="Square unstable", source="statuspage", signal="s",
                              customer=customer, service=service, payload={"fixture": True})[0]
    assessments.dispose(row.pk, actor="t1a", outcome="monitoring", reason="supplier unstable")
    return Assessment.objects.get(pk=row.pk)


def test_monitoring_gets_a_recheck_then_reruns_and_closes(policy, settings,
                                                         django_capture_on_commit_callbacks):
    settings.DEBUG = True
    row = monitoring()
    before = timezone.now()
    cadence.tick()
    row.refresh_from_db()
    assert before + timedelta(minutes=29) < row.next_recheck_at < before + timedelta(minutes=31)
    Assessment.objects.filter(pk=row.pk).update(next_recheck_at=timezone.now() - timedelta(seconds=1))
    counts = cadence.tick()
    row.refresh_from_db()
    assert counts["rechecks"] == 1
    assert (row.automation_state, row.next_recheck_at) == ("queued", None)
    assert row.history.filter(kind="recheck_requested", actor=cadence.DEADLINE_ACTOR).exists()
    with django_capture_on_commit_callbacks(execute=True):
        automation.run(row.pk)
    row.refresh_from_db()
    assert row.status == "no_issue"


@pytest.mark.parametrize("change", ["off", "no_policy", "unscoped", "running"])
def test_not_scheduled(policy, change):
    if change == "off":
        policy.recheck_minutes = 0
        policy.save()
    if change == "no_policy":
        policy.delete()
    row = monitoring(service="" if change == "unscoped" else "checkout")
    if change == "running":
        Assessment.objects.filter(pk=row.pk).update(automation_state="running")
    cadence.tick()
    row.refresh_from_db()
    assert row.next_recheck_at is None


def test_due_recheck_on_a_closed_assessment_just_clears(policy):
    row = monitoring()
    Assessment.objects.filter(pk=row.pk).update(status="no_issue",
                                                next_recheck_at=timezone.now() - timedelta(seconds=1))
    assert cadence._recheck_due(row.pk) is False
    row.refresh_from_db()
    assert row.next_recheck_at is None
    assert not row.history.filter(kind="recheck_requested").exists()


def test_schedule_skips_rows_already_scheduled_or_not_monitoring(policy):
    row = monitoring()
    Assessment.objects.filter(pk=row.pk).update(next_recheck_at=timezone.now())
    assert cadence._schedule_recheck(row.pk, timezone.now()) is False


def test_policy_form_limits_the_interval(policy):
    data = {"name": "p", "active": True, "rules": [policy.rules.first().pk], "severity": "P2",
            "impact": "moderate", "review_seconds": 300, "communication_sla_seconds": 900,
            "recheck_minutes": 2000}
    form = PolicyForm(data)
    assert not form.is_valid()
    assert "recheck_minutes" in form.errors
