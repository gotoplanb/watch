"""
OpenTelemetry bootstrap (spec §4.8): Django + psycopg + requests -> OTLP -> Collector
-> Watchtower (LGTM). Reuse, don't rebuild.

Enabled by OTEL_ENABLED (off for hermetic unit tests; on in docker-compose). Exports
over OTLP/HTTP to OTEL_EXPORTER_OTLP_ENDPOINT (the local grafana/otel-lgtm stack).
"""
import logging

logger = logging.getLogger(__name__)

_configured = False


def configure_otel(service_name: str, endpoint: str) -> None:
    global _configured
    if _configured:
        return

    from opentelemetry import metrics, trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    base = endpoint.rstrip("/")
    resource = Resource.create({"service.name": service_name, "service.namespace": "watch"})

    # Traces -> Tempo
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces"))
    )
    trace.set_tracer_provider(tracer_provider)

    # Metrics -> Prometheus (via Alloy)
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{base}/v1/metrics")
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

    # Logs -> Loki (via Alloy): attach an OTLP handler to the root logger so app +
    # Django log records ship alongside traces/metrics (correlated by trace id).
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{base}/v1/logs"))
    )
    set_logger_provider(logger_provider)
    logging.getLogger().addHandler(LoggingHandler(logger_provider=logger_provider))

    DjangoInstrumentor().instrument()
    PsycopgInstrumentor().instrument(enable_commenter=True)
    RequestsInstrumentor().instrument()

    _configured = True
    logger.info("OTel configured (traces+metrics+logs): service=%s endpoint=%s", service_name, base)
