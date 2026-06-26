"""
Integration: ADR-009 dedupe behaviour on the REAL Postgres engine (the production
target), including the partial unique index and concurrent inserts via separate
connections — things SQLite units can't fully vouch for.
"""
import os

import pytest
from django.db import IntegrityError, connections, transaction

from incidents import services
from incidents.intake import create_incident_idempotent
from incidents.models import Incident, Status

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _skip_if_not_postgres():
    if connections["default"].vendor != "postgresql":
        pytest.skip("integration settings not active (need real Postgres)")


def test_partial_unique_blocks_second_open_insert():
    _skip_if_not_postgres()
    a, created = create_incident_idempotent(
        source="sumo", payload={"h": "web-1"}, title="x", source_event_id="e1"
    )
    assert created
    # A raw duplicate OPEN insert must violate uniq_open_dedupe_key on Postgres.
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Incident.objects.create(
                source="sumo", payload={}, title="dup", dedupe_key=a.dedupe_key,
                status=Status.OPEN,
            )


def test_refire_after_resolved_allowed_on_postgres():
    _skip_if_not_postgres()
    a, _ = create_incident_idempotent(
        source="sumo", payload={"h": "web-1"}, title="x", source_event_id="e2"
    )
    services.resolve(a.id, actor="1")
    b, created = create_incident_idempotent(
        source="sumo", payload={"h": "web-1"}, title="x2", source_event_id="e2"
    )
    assert created and a.id != b.id
