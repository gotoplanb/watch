from django.apps import AppConfig


class IncidentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "incidents"

    def ready(self):
        from django.conf import settings

        from . import signals  # noqa: F401 — connects the per-user keyring signal (ADR-030)

        # OTel instrumentation is wired here (apps loaded, before serving requests).
        # Off by default so hermetic unit tests don't export; compose turns it on.
        if getattr(settings, "OTEL_ENABLED", False):  # pragma: no cover - bootstrap
            from config.otel import configure_otel

            configure_otel(settings.OTEL_SERVICE_NAME, settings.OTEL_EXPORTER_OTLP_ENDPOINT)
