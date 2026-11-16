"""Noticing when a monitoring source goes quiet (ADR-062, #68)."""
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient

from incidents import automation, cadence, heartbeats
from incidents.models import Assessment, MonitoredSource

pytestmark = pytest.mark.django_db


@pytest.fixture
def robot():
    return MonitoredSource.objects.create(name="uptimerobot", max_silence_minutes=15)


def _age(source, minutes):
    MonitoredSource.objects.filter(pk=source.pk).update(
        created_at=timezone.now() - timedelta(minutes=minutes),
        last_seen_at=None if source.last_seen_at is None else timezone.now() - timedelta(minutes=minutes))


def _tick(django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        return cadence.tick()


def test_token_is_generated_and_kept(robot):
    assert len(robot.token) >= 24
    token = robot.token
    robot.save()
    assert robot.token == token
    assert str(robot) == "uptimerobot (every 15 min)"


def test_silence_opens_one_assessment_and_a_beat_records_recovery(robot, django_capture_on_commit_callbacks):
    _age(robot, 20)
    counts = _tick(django_capture_on_commit_callbacks)
    assert counts["silent"] == 1
    row = Assessment.objects.get(source="watch")
    assert row.title == "uptimerobot has gone quiet"
    assert "never heard from" in row.signal
    assert _tick(django_capture_on_commit_callbacks)["silent"] == 0  # flagged once per silence
    heartbeats.seen("uptimerobot", via="heartbeat")
    robot.refresh_from_db()
    assert (robot.silent_since, robot.last_seen_at is not None) == (None, True)
    resolved = row.history.get(kind="signal_resolved")
    assert "reporting again (heartbeat)" in resolved.body
    assert Assessment.objects.count() == 1


def test_recent_or_inactive_sources_are_not_flagged(robot, django_capture_on_commit_callbacks):
    heartbeats.seen("uptimerobot")
    MonitoredSource.objects.create(name="old", active=False)
    _age(MonitoredSource.objects.get(name="old"), 99)
    assert _tick(django_capture_on_commit_callbacks)["silent"] == 0
    assert heartbeats.seen("unknown") is None
    assert heartbeats.check_silence(robot.pk) is False


def test_any_signal_from_the_source_counts_as_alive(robot):
    automation.receive_signal(title="t", source="uptimerobot", signal="down", customer="A", service="x")
    robot.refresh_from_db()
    assert robot.last_seen_at is not None


def test_a_second_silence_after_recovery_reuses_the_open_assessment(robot, django_capture_on_commit_callbacks):
    _age(robot, 20)
    _tick(django_capture_on_commit_callbacks)
    heartbeats.seen("uptimerobot")
    MonitoredSource.objects.filter(pk=robot.pk).update(last_seen_at=timezone.now() - timedelta(minutes=30))
    _tick(django_capture_on_commit_callbacks)
    row = Assessment.objects.get(source="watch")
    assert row.history.filter(kind="signal_repeated").exists()
    assert "last heard from at" in row.history.get(kind="signal_repeated").body


def test_heartbeat_endpoints(robot):
    client = APIClient()
    assert client.get(f"/api/heartbeats/{robot.token}").json() == {"source": "uptimerobot", "ok": True}
    assert client.post(f"/api/heartbeats/{robot.token}").status_code == 200
    assert client.get("/api/heartbeats/nope").status_code == 404
    assert client.get("/api/heartbeats/").status_code == 403
    client.force_authenticate(User.objects.create_user("viewer"))
    [listed] = client.get("/api/heartbeats/").json()
    assert (listed["name"], listed["state"]) == ("uptimerobot", "ok")
