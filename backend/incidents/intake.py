"""
Idempotent intake (ADR-002 / ADR-009).

Dedupe key = source-provided event id when present, else sha256 over a normalized
payload (volatile fields stripped). Dedupe is scoped to the open incident via the
partial unique constraint `uniq_open_dedupe_key` + an ON CONFLICT DO NOTHING create.

This module is hermetic: pure functions + one DB write, testable with no Docker or
network (spec §6).
"""
import hashlib
import json

from django.conf import settings

from . import events, numbering, services
from .models import Incident, Status, Tier


def normalize_payload(payload: dict) -> dict:
    """Strip per-delivery volatile fields so retries hash identically (ADR-009)."""
    volatile = set(settings.INTAKE_VOLATILE_FIELDS)
    return {k: v for k, v in payload.items() if k not in volatile}


def compute_dedupe_key(payload: dict, source_event_id: str | None) -> str:
    if source_event_id:
        return f"src:{source_event_id}"
    normalized = json.dumps(normalize_payload(payload), sort_keys=True, separators=(",", ":"))
    return "sha:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def create_incident_idempotent(
    *, source: str, payload: dict, title: str, source_event_id: str | None = None
) -> tuple[Incident, bool]:
    """
    Returns (incident, created).

    `created=False` means a retry/redelivery hit an already-open incident with the
    same key — an idempotent no-op (ADR-009). A re-fire after RESOLVED is not
    blocked by the partial constraint, so it creates a new incident.
    """
    dedupe_key = compute_dedupe_key(payload, source_event_id)

    # The UUID PK is generated client-side (default=uuid4), so we keep our candidate
    # id to tell "we inserted it" from "we hit an existing open row".
    candidate = Incident(
        source=source, payload=payload, title=title, dedupe_key=dedupe_key,
        assignee=services.on_call_user(Tier.T1),  # auto-route a new incident to T1 on-call
    )
    candidate_id = candidate.id

    # bulk_create(ignore_conflicts=True) issues INSERT ... ON CONFLICT DO NOTHING,
    # race-safe across concurrent consumers (the partial unique index is authority).
    Incident.objects.bulk_create([candidate], ignore_conflicts=True)

    live = Incident.objects.filter(dedupe_key=dedupe_key, status=Status.OPEN).first()
    if live is None:  # pragma: no cover - defensive: row resolved between insert and read
        # No open row with this key (e.g. it was resolved between insert and read) —
        # our insert is the authoritative row; read it back by id.
        incident, created = Incident.objects.get(pk=candidate_id), True
    else:
        incident, created = live, (live.id == candidate_id)

    if created:
        # Intake inserts via bulk_create (skips save), so assign the human number here (ADR-031).
        incident.number = numbering.next_number("INC")
        incident.save(update_fields=["number", "updated_at"])
        events.emit("incident.created", {
            "incident_id": str(incident.id), "number": incident.number,
            "title": incident.title, "source": incident.source,
        })
        services.page_on_tier_entry(incident, Tier.T1)  # page the T1 on-call (ADR-013)
    return incident, created
