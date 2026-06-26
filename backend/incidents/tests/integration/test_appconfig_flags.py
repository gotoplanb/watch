"""
Integration: ADR-003 — flags resolve over the real AppConfig Agent localhost:2772
path (the same path used in Fargate). Reads local/flags/flags.json via the agent.
"""
import pytest
from django.conf import settings

from incidents import flags
from incidents.flags import AppConfigAgentProvider

from ._reach import require

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _agent_up():
    require("localhost", 2772, "AppConfig Agent")
    # Force the real provider regardless of the active settings module.
    flags.set_provider_for_tests(AppConfigAgentProvider())
    yield
    flags.set_provider_for_tests(None)  # reset; next call lazily rebuilds


def test_known_flag_true_over_agent():
    # local/flags/flags.json seeds auto_route_on_escalation=true.
    assert flags.is_enabled("auto_route_on_escalation", default=False) is True


def test_unknown_flag_falls_back_to_default():
    assert flags.is_enabled("definitely_not_a_flag", default=False) is False


def test_settings_select_appconfig_provider():
    assert settings.FLAGS_PROVIDER == "appconfig"
