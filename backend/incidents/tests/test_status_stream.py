"""Hermetic tests for the status SSE feed (ADR-024) — the generator is bounded by `iterations`
so it terminates without real sleeps."""
import pytest
from django.test import Client

from incidents.health import status_stream


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
def test_status_stream_emits_posture_then_keepalive():
    frames = list(status_stream(2, 0))  # poll=0 -> instant; two ticks
    assert frames[0].startswith("event: status\ndata: ")
    assert '"status"' in frames[0] and '"incidents"' in frames[0] and '"by_tier"' in frames[0]
    # posture unchanged between ticks (only generated_at differs, which is ignored) -> keepalive
    assert frames[1].startswith(": keepalive")


@pytest.mark.django_db
def test_status_stream_view_headers_and_body(client, settings):
    settings.STATUS_STREAM_MAX_SECONDS = 1  # iterations = max(1, 1 // poll) = 1 -> one frame, no sleep
    resp = client.get("/api/status/stream")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/event-stream")
    assert resp["Cache-Control"] == "no-cache"
    assert resp["X-Accel-Buffering"] == "no"
    assert resp["Access-Control-Allow-Origin"]  # CORS for the cross-origin SPA
    body = b"".join(resp.streaming_content).decode()
    assert "event: status" in body and '"checks"' in body


@pytest.mark.django_db
def test_status_stream_view_zero_poll_still_one_iteration(client, settings):
    settings.STATUS_STREAM_POLL_SECONDS = 0  # guard the // by zero -> iterations falls back to 1
    resp = client.get("/api/status/stream")
    assert resp.status_code == 200
    assert b"event: status" in b"".join(resp.streaming_content)
