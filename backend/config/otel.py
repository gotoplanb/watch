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
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    base = endpoint.rstrip("/")
    resource = Resource.create({"service.name": service_name, "service.namespace": "watch"})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces"))
    )
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{base}/v1/metrics")
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

    DjangoInstrumentor().instrument()
    PsycopgInstrumentor().instrument(enable_commenter=True)
    RequestsInstrumentor().instrument()

    _configured = True
    logger.info("OTel configured: service=%s endpoint=%s", service_name, base)
