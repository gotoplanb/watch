"""Generic timeline (ADR-031): TimelineEvents attach to any record via a GFK; timeline() merges
Transitions only for records that have them (incidents), events-only otherwise."""
import pytest
from django.contrib.contenttypes.models import ContentType

from incidents import services
from incidents.models import Incident, Tier


def _incident():
    return Incident.objects.create(source="s", payload={}, title="t", dedupe_key="tl1", current_tier=Tier.T1)


@pytest.mark.django_db
def test_event_attaches_to_record_via_gfk():
    inc = _incident()
    ev = services.add_note(inc, actor="u", body="hi")
    assert ev.record == inc                                        # GFK resolves back to the record
    assert ev.content_type == ContentType.objects.get_for_model(Incident)
    assert str(ev.object_id) == str(inc.pk)                        # stored as the UUID string
    assert list(inc.events.all()) == [ev]                          # reverse GenericRelation


@pytest.mark.django_db
def test_timeline_merges_events_and_incident_transitions():
    inc = _incident()
    services.add_note(inc, actor="u", body="note")
    services.escalate(inc.id, actor="7")                           # -> a Transition + a system event
    kinds = [i["kind"] for i in services.timeline(inc)]
    assert "transition" in kinds and kinds.count("event") >= 1


@pytest.mark.django_db
def test_timeline_is_events_only_for_a_record_without_transitions():
    """Stand-in for Problem/RCA (which have events but no Transitions) — the generic branch."""
    inc = _incident()
    services.add_note(inc, actor="u", body="only an event")

    class RecordWithoutTransitions:  # exposes .events, no .transitions
        events = inc.events

    items = services.timeline(RecordWithoutTransitions())
    assert [i["kind"] for i in items] == ["event"]                 # transitions branch skipped
