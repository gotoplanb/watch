"""ADR-009: intake idempotency — key composition and open-scoped dedupe."""
import pytest

from incidents import services
from incidents.intake import compute_dedupe_key, create_incident_idempotent, normalize_payload
from incidents.models import Incident


def test_normalize_strips_volatile_fields():
    payload = {"host": "web-1", "timestamp": "2026-06-26T00:00:00Z", "deliveryId": "abc"}
    assert normalize_payload(payload) == {"host": "web-1"}


def test_dedupe_key_prefers_source_event_id():
    # Same content, different volatile fields -> same hash key.
    k1 = compute_dedupe_key({"host": "web-1", "timestamp": "t1"}, None)
    k2 = compute_dedupe_key({"host": "web-1", "timestamp": "t2"}, None)
    assert k1 == k2 and k1.startswith("sha:")
    # An explicit source id wins over the hash.
    assert compute_dedupe_key({"host": "web-1"}, "alert-123") == "src:alert-123"


@pytest.mark.django_db
def test_retry_while_open_is_idempotent_noop():
    a, created_a = create_incident_idempotent(
        source="sumo", payload={"host": "web-1"}, title="Disk full", source_event_id="alert-1"
    )
    b, created_b = create_incident_idempotent(
        source="sumo", payload={"host": "web-1"}, title="Disk full (retry)", source_event_id="alert-1"
    )
    assert created_a is True and created_b is False
    assert a.id == b.id
    assert Incident.objects.count() == 1


@pytest.mark.django_db
def test_refire_after_resolved_creates_new_incident():
    a, _ = create_incident_idempotent(
        source="sumo", payload={"host": "web-1"}, title="Disk full", source_event_id="alert-1"
    )
    services.resolve(a.id, actor="42")
    # Same key, but the prior incident is RESOLVED -> partial constraint no longer blocks.
    b, created_b = create_incident_idempotent(
        source="sumo", payload={"host": "web-1"}, title="Disk full again", source_event_id="alert-1"
    )
    assert created_b is True
    assert a.id != b.id
    assert Incident.objects.count() == 2
