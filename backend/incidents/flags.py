"""
Feature-flag / rollout-mode seam (ADR-003 / ADR-014).

All evaluation goes through this seam; swapping providers later is one class. A control resolves to
a **rollout mode** — `on`, `off`, or `sample:<rate>` (0.0–1.0):
  - `is_enabled(name, default)` — the on/off convenience wrapper (a release-flag fork).
  - `active(name, key=None)` — the general form: `on`→True, `off`→False, `sample:R`→
    **deterministic** `hash(key) < R` (a given entity is consistently in or out), or random when
    no `key`.

Providers return the RAW value (bool or a string like "on"/"sample:0.1"); the seam interprets it.
Unit tests use the in-memory provider; prod/local use the AppConfig Agent over localhost:2772.
Operational toggles (`paging_enabled`, `devops_agent.*`) are permanent kill-switches — both branches
stay tested forever (ADR-014); release flags get a removal step once permanent.
"""
import hashlib
import uuid

import requests
from django.conf import settings


class InMemoryProvider:
    """Hermetic provider for unit tests. Values may be bool or a rollout string ("sample:0.3")."""

    def __init__(self, values: dict | None = None):
        self._values = dict(values or {})

    def get(self, name: str, default):
        return self._values.get(name, default)


class AppConfigAgentProvider:
    """Reads controls from the AppConfig Agent sidecar (ECS) / container (compose). Values are the
    raw flag-document entries (bool or a rollout string)."""

    def __init__(self):
        self._base = settings.APPCONFIG_AGENT_URL.rstrip("/")
        self._path = (
            f"/applications/{settings.APPCONFIG_APPLICATION}"
            f"/environments/{settings.APPCONFIG_ENVIRONMENT}"
            f"/configurations/{settings.APPCONFIG_PROFILE}"
        )

    def get(self, name: str, default):
        try:
            resp = requests.get(self._base + self._path, timeout=1)
            resp.raise_for_status()
            # Poll-based propagation (~45s) — never assume sub-second flips (ADR-003).
            return resp.json().get(name, default)
        except Exception:
            # Fail safe to the caller-supplied default; controls must never hard-fail.
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


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("on", "true", "1", "yes")


def is_enabled(name: str, default: bool = False) -> bool:
    """On/off convenience wrapper — treats `sample:*` as off (use `active` for sampling)."""
    return _truthy(_provider().get(name, default))


def active(name: str, key=None, default="off") -> bool:
    """Resolve a rollout mode to a boolean (ADR-014). `on`→True, `off`→False, `sample:R`→ a
    deterministic `hash(key) < R` (stable per entity) or random when key is None."""
    value = _provider().get(name, default)
    if isinstance(value, bool):
        return value
    val = str(value).strip().lower()
    if val in ("on", "true", "1", "yes"):
        return True
    if val.startswith("sample:"):
        try:
            rate = float(val.split(":", 1)[1])
        except (ValueError, IndexError):
            return False
        if rate <= 0:
            return False
        if rate >= 1:
            return True
        return _fraction(key) < rate
    return False


def _fraction(key) -> float:
    """A stable [0,1) fraction for `key` (same key → same fraction). With no key, a random uuid4 makes
    it a fresh random fraction per call — via the same hash, so no PRNG is involved."""
    if key is None:
        key = uuid.uuid4().hex
    digest = hashlib.sha256(str(key).encode()).hexdigest()[:8]
    return int(digest, 16) / 0xFFFFFFFF


def set_provider_for_tests(provider) -> None:
    """Test helper: inject a provider (e.g. InMemoryProvider)."""
    global _provider_instance
    _provider_instance = provider
