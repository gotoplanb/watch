"""Exercise imports preserve local edits and cannot publish events or partially load a file."""
import copy
import json
from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import CommandError

from incidents.models import Annotation, Incident, Problem, Rca, RecordLink, Transition
from incidents.services import timeline

pytestmark = pytest.mark.django_db


@pytest.fixture
def example_file(tmp_path, settings):
    settings.DEBUG = True
    settings.ESCALATION_LOCAL_MODE = True
    for tier in (1, 2, 3):
        user = User.objects.create_user(f"responder{tier}")
        user.groups.add(Group.objects.get_or_create(name=f"T{tier}")[0])
    scenario = {
        "key": "sample", "title": "Checkout unavailable", "summary": "A modeled service outage.",
        "source_reference": "invented training scenario", "facts": {"synthetic": True},
        "assignee": "responder2",
        "steps": [
            {"minute": 1, "kind": "ack", "actor": "responder1", "body": "Investigating checkout."},
            {"minute": 3, "kind": "escalate", "actor": "responder1", "body": "Runtime expertise needed."},
            {"minute": 4, "kind": "ack", "actor": "responder2", "body": "T2 owns investigation."},
            {"minute": 6, "kind": "note", "actor": "responder2", "body": "Pool saturation confirmed.",
             "annotations": [{"tag": "root-cause", "body": "Bounded pool was exhausted."}]},
            {"minute": 8, "kind": "handoff", "actor": "responder2", "body": "Rollback approved."},
            {"minute": 10, "kind": "resolve", "actor": "responder2", "body": "Checkout verified after rollback."},
        ],
        "problem": {"title": "Pool capacity", "status": "known_error", "description": "Add a capacity test."},
        "rca": {"title": "Checkout exercise", "status": "draft", "document": "# Example\nRecovery verified."},
    }
    path = tmp_path / "examples.json"
    document = {"version": 1, "collection": "unit-examples", "scenarios": [scenario]}
    path.write_text(json.dumps(document))
    return path, document


def _run(path):
    output = StringIO()
    call_command("seed_incident_examples", str(path), stdout=output)
    return output.getvalue()


def test_import_is_linked_chronological_and_preserves_operator_edits(example_file, client):
    path, _ = example_file
    with patch("incidents.events.emit", side_effect=AssertionError("No outbound events")), \
         patch("incidents.notify.send", side_effect=AssertionError("No paging")):
        assert "Created 1" in _run(path)
    incident = Incident.objects.get()
    assert (incident.status, incident.current_tier) == ("RESOLVED", "T2")
    assert incident.assignee.username == "responder2"
    assert not incident.current_task_token and not incident.escalation_execution_arn
    assert incident.payload["fixture"] is True
    times = [item["at"] for item in timeline(incident)]
    assert times == sorted(times)
    assert times[0] == incident.created_at and times[-1] == incident.updated_at
    assert incident.created_at < incident.acknowledged_at < incident.updated_at
    assert Transition.objects.count() == 4
    assert Annotation.objects.get().tag == "root-cause"
    assert (Problem.objects.count(), Rca.objects.count(), RecordLink.objects.count()) == (1, 1, 3)
    client.force_login(incident.assignee)
    for url in (f"/ui/incidents/{incident.pk}/", f"/ui/problems/{Problem.objects.get().pk}/",
                f"/ui/rcas/{Rca.objects.get().pk}/", f"/ui/incidents/{incident.pk}/rca.md"):
        assert client.get(url).status_code == 200
    incident.title = "Edited by operator"
    incident.save()
    assert "preserved 1" in _run(path)
    incident.refresh_from_db()
    assert incident.title == "Edited by operator"
    assert Transition.objects.count() == 4 and RecordLink.objects.count() == 3


@pytest.mark.parametrize("field,value", [("DEBUG", False), ("ESCALATION_LOCAL_MODE", False)])
def test_refuses_nonlocal_configuration(example_file, settings, field, value):
    path, _ = example_file
    setattr(settings, field, value)
    with pytest.raises(CommandError, match="requires DEBUG"):
        _run(path)
    assert not Incident.objects.exists()


@pytest.mark.parametrize("case", ["missing_user", "wrong_role", "bad_time", "after_resolve", "final_rca", "duplicate"])
def test_invalid_scenario_cannot_partially_import(example_file, case):
    path, document = example_file
    bad = copy.deepcopy(document["scenarios"][0])
    bad["key"] = "bad"
    if case == "missing_user":
        bad["steps"][0]["actor"] = "missing"
    elif case == "wrong_role":
        bad["steps"][2]["actor"] = "responder1"
    elif case == "bad_time":
        bad["steps"][1]["minute"] = -1
    elif case == "after_resolve":
        bad["steps"].append({"minute": 11, "kind": "ack", "actor": "responder2", "body": "Invalid."})
    elif case == "final_rca":
        bad["rca"]["status"] = "final"
    else:
        bad["key"] = "sample"
    document["scenarios"].append(bad)
    path.write_text(json.dumps(document))
    with pytest.raises(CommandError):
        _run(path)
    assert not Incident.objects.exists()


def test_database_failure_rolls_back_entire_collection(example_file):
    path, _ = example_file
    with patch.object(Rca.objects, "create", side_effect=RuntimeError("database failure")):
        with pytest.raises(RuntimeError, match="database failure"):
            _run(path)
    assert not Incident.objects.exists() and not Problem.objects.exists()
    assert not Transition.objects.exists() and not Annotation.objects.exists()
