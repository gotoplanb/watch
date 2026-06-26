"""
Feature-flag seam (ADR-003).

All evaluation goes through `is_enabled(name, default)`. Swapping providers later
is one class. Unit tests use the in-memory provider; prod/local use the AppConfig
Agent over the identical localhost:2772 path.

A flag is a fork: "done" for a flagged feature means BOTH branches tested and a
documented flag-removal step once permanent (spec §4.5).
"""
import requests
from django.conf import settings


class InMemoryProvider:
    """Hermetic provider for unit tests — tests stub their dependencies."""

    def __init__(self, values: dict[str, bool] | None = None):
        self._values = dict(values or {})

    def get(self, name: str, default: bool) -> bool:
        return self._values.get(name, default)


class AppConfigAgentProvider:
    """Reads flags from the AppConfig Agent sidecar (ECS) / container (compose)."""

    def __init__(self):
        self._base = settings.APPCONFIG_AGENT_URL.rstrip("/")
        self._path = (
            f"/applications/{settings.APPCONFIG_APPLICATION}"
            f"/environments/{settings.APPCONFIG_ENVIRONMENT}"
            f"/configurations/{settings.APPCONFIG_PROFILE}"
        )

    def get(self, name: str, default: bool) -> bool:
        try:
            resp = requests.get(self._base + self._path, timeout=1)
            resp.raise_for_status()
            # Poll-based propagation (~45s) — never assume sub-second flips (ADR-003).
            return bool(resp.json().get(name, default))
        except Exception:
            # Fail safe to the caller-supplied default; flags must never hard-fail.
            return default


_provider_instance = None


def _provider():
    global _provider_instance
    if _provider_instance is None:
        if settings.FLAGS_PROVIDER == "appconfig":
            _provider_instance = AppConfigAgentProvider()
        else:
            _provider_instance = InMemoryProvider()
    return _provider_instance


def is_enabled(name: str, default: bool = False) -> bool:
    return _provider().get(name, default)


def set_provider_for_tests(provider) -> None:
    """Test helper: inject a provider (e.g. InMemoryProvider)."""
    global _provider_instance
    _provider_instance = provider
