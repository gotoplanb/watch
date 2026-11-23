"""The read-only copy of Grafana IRM's on-call schedules (ADR-066 §7, watch#74).

Event shapes follow a live `filter_events?type=final` response (verified 2026-11-23)."""
from datetime import datetime, timedelta
from datetime import timezone as dt_tz

import pytest
import requests
from django.contrib import admin
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import Client, RequestFactory
from django.utils import timezone

from incidents import cadence, irm_schedule, services
from incidents.models import MonitoredSource, OnCallShift, OperationsHeartbeat

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=dt_tz.utc)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _event(start, end, *emails, rotation="OK5WTUY1E5M28", **flags):
    return {
        "all_day": False, "start": _iso(start), "end": _iso(end),
        "users": [{"display_name": e.split("@")[0], "email": e, "pk": f"U{i}"} for i, e in enumerate(emails)],
        "missing_users": [], "priority_level": 0, "source": "web", "calendar_type": 0,
        "is_empty": flags.get("is_empty", False), "is_gap": flags.get("is_gap", False),
        "is_override": False, "shift": {"pk": rotation, "name": "rotation", "type": 2},
    }


def _day(offset):
    return datetime(2026, 11, 23, tzinfo=dt_tz.utc) + timedelta(days=offset)


@pytest.fixture
def irm(settings):
    settings.GRAFANA_IRM_URL = "https://stack.grafana.net"
    settings.GRAFANA_IRM_TOKEN = "test-token"
    settings.IRM_SCHEDULE_IDS = "T1:S1, T2:S2, bogus:S9, T3:"
    settings.IRM_SYNC_INTERVAL_SECONDS = 300
    settings.IRM_SYNC_DAYS = 14
    return settings


@pytest.fixture
def dave():
    return User.objects.create(username="dave", email="Dave@Example.com")


def _fake_irm(monkeypatch, by_schedule, calls=None):
    def fetch(schedule_id, start, days):
        if calls is not None:
            calls.append((schedule_id, start.date().isoformat(), days))
        result = by_schedule[schedule_id]
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(irm_schedule, "fetch_events", fetch)


def test_schedule_ids_and_enabled(settings, irm):
    assert irm_schedule.schedule_ids() == {"T1": "S1", "T2": "S2"}
    assert irm_schedule.enabled()
    settings.GRAFANA_IRM_TOKEN = ""
    assert not irm_schedule.enabled()


def test_off_by_default(settings):
    assert not irm_schedule.enabled()
    assert irm_schedule.sync_if_due() is None


def test_fetch_events_calls_the_plugin_api(monkeypatch, irm):
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "S1", "events": [{"start": "x"}]}

    def get(url, params, headers, timeout):
        seen.update(url=url, params=params, auth=headers["Authorization"])
        return Resp()

    monkeypatch.setattr(irm_schedule.requests, "get", get)
    assert irm_schedule.fetch_events("S1", NOW, 14) == [{"start": "x"}]
    assert seen == {
        "url": "https://stack.grafana.net/api/plugins/grafana-irm-app/resources/schedules/S1/filter_events/",
        "params": {"type": "final", "date": "2026-11-23", "days": 14},
        "auth": "Bearer test-token",
    }


def test_plan_joins_back_to_back_days_and_skips_gaps_past_and_unknown_people():
    events = [
        _event(_day(1), _day(2), "dave@example.com"),
        _event(_day(0), _day(1), "DAVE@example.com "),
        _event(_day(-1), _day(0), "dave@example.com"),         # already ended
        _event(_day(2), _day(3), is_gap=True),
        _event(_day(3), _day(4), is_empty=True),
        _event(_day(4), _day(5), "stranger@example.com", "dave@example.com"),
        {**_event(_day(5), _day(6), "dave@example.com"), "start": "not a date"},
    ]
    result = irm_schedule.plan(events, {"dave@example.com": 7}, NOW)
    assert result.shifts == [(7, _day(0), _day(2), "OK5WTUY1E5M28"), (7, _day(4), _day(5), "OK5WTUY1E5M28")]
    assert result.unmatched == {"stranger@example.com"}


def test_plan_keeps_different_rotations_apart():
    events = [_event(_day(0), _day(1), "a@x.io", rotation="R1"), _event(_day(1), _day(2), "a@x.io", rotation="R2")]
    assert len(irm_schedule.plan(events, {"a@x.io": 1}, NOW).shifts) == 2


def test_sync_replaces_current_and_future_copies_and_keeps_history(monkeypatch, irm, dave):
    other = User.objects.create(username="t2a", email="t2a@example.com")
    ended = OnCallShift.objects.create(tier="T1", user=dave, starts_at=_day(-2), ends_at=_day(-1), source="irm")
    stale = OnCallShift.objects.create(tier="T1", user=other, starts_at=_day(0), ends_at=_day(3), source="irm")
    local = OnCallShift.objects.create(tier="T1", user=other, starts_at=_day(5), ends_at=_day(6))
    calls = []
    _fake_irm(monkeypatch, {
        "S1": [_event(_day(0), _day(1), "dave@example.com"), _event(_day(1), _day(2), "dave@example.com")],
        "S2": [_event(_day(0), _day(1), "nobody@example.com")],
    }, calls)

    summary = irm_schedule.sync(NOW)

    assert summary == {"ok": True, "shifts": {"T1": 1, "T2": 0}, "unmatched": ["nobody@example.com"]}
    assert calls == [("S1", "2026-11-23", 14), ("S2", "2026-11-23", 14)]
    copied = OnCallShift.objects.get(tier="T1", source="irm", ends_at__gt=NOW)
    assert (copied.user, copied.starts_at, copied.ends_at, copied.external_id) == (dave, _day(0), _day(2), "OK5WTUY1E5M28")
    assert OnCallShift.objects.filter(pk=ended.pk).exists()       # history kept
    assert not OnCallShift.objects.filter(pk=stale.pk).exists()   # replaced
    assert OnCallShift.objects.filter(pk=local.pk).exists()       # Watch-entered rows untouched
    assert services.current_on_call("T1", NOW).user == dave
    assert services.current_on_call("T2", NOW) is None            # unmatched person = gap in Watch
    source = MonitoredSource.objects.get(name="irm-schedule")
    assert source.last_seen_at is not None
    assert source.max_silence_minutes == 15


def test_a_failed_read_changes_nothing_and_sends_no_heartbeat(monkeypatch, irm, dave):
    kept = OnCallShift.objects.create(tier="T1", user=dave, starts_at=_day(0), ends_at=_day(2), source="irm")
    _fake_irm(monkeypatch, {"S1": [], "S2": requests.ConnectionError("down")})
    summary = irm_schedule.sync(NOW)
    assert summary["ok"] is False
    assert "down" in summary["error"]
    assert OnCallShift.objects.filter(pk=kept.pk).exists()
    assert not MonitoredSource.objects.filter(name="irm-schedule").exists()


def test_sync_if_due_runs_once_per_interval(monkeypatch, irm, dave):
    calls = []
    _fake_irm(monkeypatch, {"S1": [], "S2": []}, calls)
    assert irm_schedule.sync_if_due(NOW)["ok"] is True
    assert irm_schedule.sync_if_due(NOW + timedelta(seconds=60)) is None
    assert irm_schedule.sync_if_due(NOW + timedelta(seconds=301))["ok"] is True
    assert len(calls) == 4
    assert OperationsHeartbeat.objects.get(name="irm-schedule-sync").detail["ok"] is True


def test_operations_tick_runs_the_sync_and_survives_a_crash(monkeypatch, irm):
    ran = []
    monkeypatch.setattr(irm_schedule, "sync_if_due", lambda now: ran.append(now))
    assert cadence.tick()["errors"] == 0
    assert len(ran) == 1

    def boom(now):
        raise RuntimeError("bad")

    monkeypatch.setattr(irm_schedule, "sync_if_due", boom)
    assert cadence.tick()["errors"] == 1


def test_command(monkeypatch, irm, capsys):
    _fake_irm(monkeypatch, {"S1": [], "S2": []})
    call_command("sync_irm_schedule")
    assert '"ok": true' in capsys.readouterr().out
    _fake_irm(monkeypatch, {"S1": requests.Timeout("slow"), "S2": []})
    with pytest.raises(CommandError, match="unchanged"):
        call_command("sync_irm_schedule")


def test_command_when_off():
    with pytest.raises(CommandError, match="off"):
        call_command("sync_irm_schedule")


def _staff_client():
    user = User.objects.create_superuser("boss", "boss@example.com", "pw")
    client = Client()
    client.force_login(user)
    return client, user


def test_shift_editing_is_locked_when_irm_is_on(irm):
    client, user = _staff_client()
    page = client.get("/ui/schedule/").content.decode()
    assert "read-only here" in page
    assert "Add shift" not in page
    resp = client.post("/ui/schedule/shift/", {"tier": "T1", "user": user.pk,
                                                "starts_at": "2026-11-23T10:00", "ends_at": "2026-11-23T12:00"})
    assert resp.status_code == 403
    assert not OnCallShift.objects.exists()
    model_admin = admin.site._registry[OnCallShift]
    request = RequestFactory().get("/")
    request.user = user
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)


def test_shift_editing_stays_open_locally():
    client, user = _staff_client()
    assert "Add shift" in client.get("/ui/schedule/").content.decode()
    model_admin = admin.site._registry[OnCallShift]
    request = RequestFactory().get("/")
    request.user = user
    assert model_admin.has_add_permission(request)
    assert model_admin.has_change_permission(request)
    assert model_admin.has_delete_permission(request)
    OnCallShift.objects.create(tier="T1", user=user, starts_at=timezone.now(), ends_at=timezone.now(), source="irm")
    assert ">IRM</span>" in client.get("/ui/schedule/").content.decode()
