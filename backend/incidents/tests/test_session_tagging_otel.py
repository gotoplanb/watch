"""Repro/regression for watch#33 — the session.id/session.user span attributes must actually land
on the exported OTel server span (not silently no-op on a non-recording span). Runs a real request
through Django + the OTel Django instrumentation with an in-memory exporter."""
import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from opentelemetry import trace
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def spans(settings):
    settings.SESSION_USER_HMAC_KEY = "test-hmac-key"
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    prev = trace._TRACER_PROVIDER
    trace._TRACER_PROVIDER = provider  # bypass the set-once guard for the test
    DjangoInstrumentor().instrument()
    yield exporter
    DjangoInstrumentor().uninstrument()
    trace._TRACER_PROVIDER = prev


@pytest.mark.django_db
def test_session_id_and_user_land_on_span(spans):
    User = get_user_model()
    user = User.objects.create_user("spanuser", password="x")
    client = Client()
    client.force_login(user)  # authenticated session -> middleware mints + tags a correlation id
    resp = client.get("/api/status")
    assert resp.status_code in (200, 503)

    server_spans = [s for s in spans.get_finished_spans() if s.kind == trace.SpanKind.SERVER]
    assert server_spans, "no server span was exported (OTel Django not instrumenting?)"
    attrs = {}
    for s in server_spans:
        attrs.update(dict(s.attributes or {}))
    assert "session.id" in attrs, f"session.id not on span; attrs seen: {sorted(attrs)}"
    assert attrs["session.id"], "session.id present but empty"
    assert "session.user" in attrs, f"session.user not on span; attrs seen: {sorted(attrs)}"
