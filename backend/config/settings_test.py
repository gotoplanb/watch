"""
Hermetic test settings (spec §6): no Docker, no network. SQLite in-memory (still
supports the ADR-009 partial unique index), local-memory cache, in-memory flags,
escalation in local no-op mode.
"""
from .settings import *  # noqa: F401,F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
SESSION_ENGINE = "django.contrib.sessions.backends.cache"

FLAGS_PROVIDER = "memory"
ESCALATION_LOCAL_MODE = True
INTAKE_WEBHOOK_SECRET = "test-secret"
API_KEY_SECRET = "test-api-key-secret"
