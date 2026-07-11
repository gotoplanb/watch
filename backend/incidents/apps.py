import os
import sys

from django.apps import AppConfig


def _autoreload_parent() -> bool:
    """True only in runserver's file-watcher parent. ready() fires in BOTH autoreload processes;
    the serving child is marked RUN_MAIN=true by Django. Skipping OTel in the parent keeps exactly
    one exporting process, so hot reload (`make dev`) doesn't double telemetry in Watchtower."""
    return (
        "runserver" in sys.argv
        and "--noreload" not in sys.argv
        and os.environ.get("RUN_MAIN") != "true"
    )


class IncidentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "incidents"

    def ready(self):
        from django.conf import settings

        from . import signals  # noqa: F401 — connects the per-user keyring signal (ADR-030)

        # OTel instrumentation is wired here (apps loaded, before serving requests).
        # Off by default so hermetic unit tests don't export; compose turns it on.
        if getattr(settings, "OTEL_ENABLED", False) and not _autoreload_parent():  # pragma: no cover - bootstrap
            from config.otel import configure_otel

            configure_otel(settings.OTEL_SERVICE_NAME, settings.OTEL_EXPORTER_OTLP_ENDPOINT)
