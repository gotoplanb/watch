"""The wt CLI documents every workspace action and its fields (cli/src/wt/actions.py, ADR-056).
This keeps that reference honest: it must name exactly the server's actions and form fields."""
import importlib.util
from pathlib import Path

import pytest

from incidents import assessments
from incidents.operations_ui import PARTICIPANT_ACTIONS, WORKSPACE_ACTIONS, _forms

ACTIONS_FILE = Path(__file__).resolve().parents[3] / "cli" / "src" / "wt" / "actions.py"


def _cli_actions():
    spec = importlib.util.spec_from_file_location("wt_actions", ACTIONS_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.django_db
def test_every_action_and_field_is_documented():
    cli = _cli_actions()
    assert set(cli.ACTIONS) == set(WORKSPACE_ACTIONS)
    assert set(cli.PARTICIPANT_ACTIONS) == set(PARTICIPANT_ACTIONS)
    row = assessments.receive(title="t", source="monitor", signal="s")[0]
    forms = _forms(row)
    for name, (_, fields, _) in cli.ACTIONS.items():
        assert set(fields) == set(forms[name].fields), name
