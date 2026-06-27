"""Unit tests for the AppConfig flag provider + provider selection (ADR-003)."""
from unittest import mock

from incidents import flags


def test_appconfig_provider_reads_value():
    provider = flags.AppConfigAgentProvider()
    resp = mock.Mock()
    resp.json.return_value = {"feature_x": True}
    resp.raise_for_status.return_value = None
    with mock.patch.object(flags.requests, "get", return_value=resp):
        assert provider.get("feature_x", False) is True


def test_appconfig_provider_failsafe_to_default_on_error():
    provider = flags.AppConfigAgentProvider()
    with mock.patch.object(flags.requests, "get", side_effect=RuntimeError("agent down")):
        assert provider.get("feature_x", True) is True


def test_provider_selection_appconfig(settings):
    flags.set_provider_for_tests(None)  # reset the cached instance
    settings.FLAGS_PROVIDER = "appconfig"
    try:
        assert isinstance(flags._provider(), flags.AppConfigAgentProvider)
    finally:
        flags.set_provider_for_tests(None)


def test_provider_selection_memory_default(settings):
    flags.set_provider_for_tests(None)
    settings.FLAGS_PROVIDER = "memory"
    try:
        assert isinstance(flags._provider(), flags.InMemoryProvider)
    finally:
        flags.set_provider_for_tests(None)
