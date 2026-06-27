"""
Integration (ADR-001/007): the REAL escalation engine, no LOCAL_MODE shortcut.

Step Functions Local drives executions; the host lambda shim invokes the real
record_token / commit handlers, which call incidents.services against Postgres. Proves:
  - record_token persists the tier's task token,
  - a human SendTaskSuccess(ESCALATE/RESOLVE) advances tiers and writes Transitions
    with the acting user as actor, ending the execution in SUCCEEDED,
  - an SLA timeout auto-escalates with actor=system:auto-escalation.

Requires the compose `integration` profile (Step Functions Local with
LAMBDA_ENDPOINT -> host shim). Skips cleanly if SFN Local isn't up.
"""
import json
import threading
import time
from pathlib import Path

import boto3
import pytest
from django.conf import settings

from incidents.lambda_shim import build_server
from incidents.models import Incident, Status, Tier, Transition

from ._reach import require

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

SFN_ENDPOINT = "http://localhost:8083"
ASL_PATH = Path(__file__).resolve().parents[4] / "escalation" / "statemachine.asl.json"


def _definition(timeouts=None) -> str:
    raw = (
        ASL_PATH.read_text()
        .replace("${record_token_function_arn}", "record_token")
        .replace("${commit_function_arn}", "commit")
    )
    spec = json.loads(raw)
    for state, secs in (timeouts or {}).items():
        spec["States"][state]["TimeoutSeconds"] = secs
    return json.dumps(spec)


def _poll(fn, timeout=20, interval=0.3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


@pytest.fixture(scope="module")
def shim():
    require("localhost", 8083, "Step Functions Local")
    server, _ = build_server("0.0.0.0", settings.LAMBDA_SHIM_PORT)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield
    server.shutdown()


@pytest.fixture
def sfn():
    return boto3.client(
        "stepfunctions", endpoint_url=SFN_ENDPOINT, region_name="us-east-1",
        aws_access_key_id="x", aws_secret_access_key="x",
    )


def _state_machine(sfn, name, timeouts=None) -> str:
    arn = f"arn:aws:states:us-east-1:000000000000:stateMachine:{name}"
    try:
        sfn.delete_state_machine(stateMachineArn=arn)
    except Exception:
        pass
    return sfn.create_state_machine(
        name=name, definition=_definition(timeouts),
        roleArn="arn:aws:iam::000000000000:role/Dummy",
    )["stateMachineArn"]


def _incident(key) -> Incident:
    return Incident.objects.create(source="sumo", payload={}, title="e2e",
                                   dedupe_key=key, current_tier=Tier.T1)


def _token_at(incident_id, tier):
    inc = Incident.objects.get(pk=incident_id)
    return inc.current_task_token if (inc.current_tier == tier and inc.current_task_token) else None


def test_human_escalate_then_resolve_runs_to_succeeded(shim, sfn):
    inc = _incident("e2e-human")
    sm = _state_machine(sfn, "EscalationHuman")
    execution = sfn.start_execution(
        stateMachineArn=sm, name=f"h-{inc.id.hex[:10]}",
        input=json.dumps({"incidentId": str(inc.id), "tier": "T1"}),
    )["executionArn"]

    tok1 = _poll(lambda: _token_at(inc.id, Tier.T1))
    assert tok1, "record_token did not persist a T1 task token"

    # Human escalates T1 -> T2 (actor = user 99).
    sfn.send_task_success(taskToken=tok1, output=json.dumps({"outcome": "ESCALATE", "actor": "99"}))
    assert _poll(lambda: Incident.objects.get(pk=inc.id).current_tier == Tier.T2)
    assert Transition.objects.filter(incident=inc, to_tier="T2", actor="99").exists()

    tok2 = _poll(lambda: _token_at(inc.id, Tier.T2))
    assert tok2 and tok2 != tok1, "T2 re-tokenize failed"

    # Human resolves at T2.
    sfn.send_task_success(taskToken=tok2, output=json.dumps({"outcome": "RESOLVE", "actor": "99"}))
    assert _poll(lambda: Incident.objects.get(pk=inc.id).status == Status.RESOLVED)

    status = _poll(lambda: (lambda s: s if s != "RUNNING" else None)(
        sfn.describe_execution(executionArn=execution)["status"]))
    assert status == "SUCCEEDED"


def test_sla_timeout_auto_escalates_with_system_actor(shim, sfn):
    inc = _incident("e2e-timeout")
    sm = _state_machine(sfn, "EscalationTimeout", timeouts={"T1_Wait": 2})
    sfn.start_execution(
        stateMachineArn=sm, name=f"t-{inc.id.hex[:10]}",
        input=json.dumps({"incidentId": str(inc.id), "tier": "T1"}),
    )

    assert _poll(lambda: _token_at(inc.id, Tier.T1)), "record_token did not persist a T1 token"
    # Do nothing — the 2s SLA elapses and the timeout auto-escalates.
    assert _poll(lambda: Incident.objects.get(pk=inc.id).current_tier == Tier.T2, timeout=25)
    assert Transition.objects.filter(
        incident=inc, to_tier="T2", actor="system:auto-escalation"
    ).exists()
