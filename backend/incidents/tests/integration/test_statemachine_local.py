"""
Integration: ADR-001/007 — the real ASL loads into Step Functions Local (AWS's own
validator) and routes correctly. Uses mocked Lambda integrations (escalation/test/
MockConfigFile.json) so per-tier waitForTaskToken outputs are simulated and the
Choice routing (RESOLVE -> Succeed, ESCALATE -> next tier) is exercised end-to-end
without real Lambdas.
"""
import json
import time
from pathlib import Path

import boto3
import pytest

from ._reach import require

pytestmark = pytest.mark.integration

SFN_ENDPOINT = "http://localhost:8083"
ASL_PATH = Path(__file__).resolve().parents[4] / "escalation" / "statemachine.asl.json"


def _concrete_asl() -> str:
    """Substitute the function-ARN placeholders so the ASL is valid for SFN Local."""
    raw = ASL_PATH.read_text()
    return (
        raw.replace("${record_token_function_arn}",
                    "arn:aws:lambda:us-east-1:000000000000:function:record_token")
           .replace("${auto_escalate_function_arn}",
                    "arn:aws:lambda:us-east-1:000000000000:function:auto_escalate")
    )


@pytest.fixture(scope="module")
def sfn():
    require("localhost", 8083, "Step Functions Local")
    return boto3.client(
        "stepfunctions", endpoint_url=SFN_ENDPOINT, region_name="us-east-1",
        aws_access_key_id="x", aws_secret_access_key="x",
    )


@pytest.fixture(scope="module")
def state_machine_arn(sfn):
    # Name must match the MockConfigFile "StateMachines" key ("Escalation").
    resp = sfn.create_state_machine(
        name="Escalation",
        definition=_concrete_asl(),
        roleArn="arn:aws:iam::000000000000:role/DummyRole",
    )
    arn = resp["stateMachineArn"]
    yield arn
    sfn.delete_state_machine(stateMachineArn=arn)


def _run(sfn, arn, test_case: str, name: str) -> str:
    # Mocked execution: append #<TestCaseName> to the state machine ARN.
    execution = sfn.start_execution(
        stateMachineArn=f"{arn}#{test_case}",
        name=name,
        input=json.dumps({"incidentId": "11111111-1111-1111-1111-111111111111"}),
    )
    arn_exec = execution["executionArn"]
    for _ in range(50):
        desc = sfn.describe_execution(executionArn=arn_exec)
        if desc["status"] != "RUNNING":
            return desc["status"]
        time.sleep(0.1)
    pytest.fail("execution did not finish in time")


def test_resolve_at_t1_succeeds(sfn, state_machine_arn):
    # T1 human resolves -> Choice -> Succeed.
    assert _run(sfn, state_machine_arn, "ResolveAtT1", "resolve-t1") == "SUCCEEDED"


def test_escalate_then_resolve_at_t2_succeeds(sfn, state_machine_arn):
    # T1 escalates -> T2 wait -> T2 resolves -> Succeed.
    assert _run(sfn, state_machine_arn, "EscalateThenResolveAtT2", "esc-t2") == "SUCCEEDED"
