"""Operating-mode service tests (ADR-035): highway is the ambient default, race windows are
declared/closed with an actor, and the read path is `current_mode()` alone."""
import pytest

from incidents import modes
from incidents.models import OperatingMode, OperatingModeWindow


@pytest.mark.django_db
def test_current_mode_defaults_to_highway():
    assert modes.current_mode() == OperatingMode.HIGHWAY


@pytest.mark.django_db
def test_open_race_window_flips_mode_and_records_actor():
    window = modes.open_race_window("t3a", reason="v0.11 release window")
    assert modes.current_mode() == OperatingMode.RACE
    assert window.actor == "t3a" and window.reason == "v0.11 release window"
    assert window.ended_at is None


@pytest.mark.django_db
def test_open_race_window_is_idempotent():
    first = modes.open_race_window("t3a")
    second = modes.open_race_window("t3b")  # already open — returned, not duplicated
    assert first.id == second.id
    assert OperatingModeWindow.objects.count() == 1


@pytest.mark.django_db
def test_close_race_window_declares_all_clear():
    modes.open_race_window("t3a")
    window = modes.close_race_window("t3a")
    assert window.ended_at is not None
    assert modes.current_mode() == OperatingMode.HIGHWAY
    # append-only: the closed window remains as the audit record
    assert OperatingModeWindow.objects.count() == 1


@pytest.mark.django_db
def test_close_race_window_noop_when_none_open():
    assert modes.close_race_window("t3a") is None
    assert modes.current_mode() == OperatingMode.HIGHWAY
