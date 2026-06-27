"""Unit tests for the Step Functions client wrapper (ADR-001/007/010), with boto3
mocked — covers local-mode no-ops and the real-mode start/send paths."""
from types import SimpleNamespace
from unittest import mock

from incidents import escalation


def _incident(**kw):
    kw.setdefault("current_task_token", "")
    return SimpleNamespace(id="11111111", current_tier="T1", **kw)


def test_start_escalation_local(settings):
    settings.ESCALATION_LOCAL_MODE = True
    arn = escalation.start_escalation(_incident())
    assert arn.startswith("local:execution:")


def test_start_escalation_real(settings):
    settings.ESCALATION_LOCAL_MODE = False
    settings.ESCALATION_STATE_MACHINE_ARN = "arn:aws:states:::stateMachine:Escalation"
    fake = mock.Mock()
    fake.start_execution.return_value = {"executionArn": "arn:exec:1"}
    with mock.patch.object(escalation.boto3, "client", return_value=fake):
        arn = escalation.start_escalation(_incident())
    assert arn == "arn:exec:1"
    fake.start_execution.assert_called_once()


def test_client_uses_endpoint_url_and_unsigned_for_sfn_local(settings):
    settings.ESCALATION_ENDPOINT_URL = "http://localhost:8083"
    with mock.patch.object(escalation.boto3, "client") as client:
        escalation._client()
    kwargs = client.call_args.kwargs
    assert kwargs["endpoint_url"] == "http://localhost:8083"
    # Unsigned requests — no long-term credentials.
    assert kwargs["config"].signature_version == escalation.UNSIGNED
    assert "aws_access_key_id" not in kwargs


def test_send_outcome_local_noop(settings):
    settings.ESCALATION_LOCAL_MODE = True
    escalation.send_outcome(_incident(current_task_token="tok"), "ESCALATE", actor="9")


def test_send_outcome_no_token_noop(settings):
    settings.ESCALATION_LOCAL_MODE = False
    escalation.send_outcome(_incident(current_task_token=""), "ESCALATE", actor="9")


def test_send_outcome_real_calls_send_task_success(settings):
    settings.ESCALATION_LOCAL_MODE = False
    fake = mock.Mock()
    with mock.patch.object(escalation.boto3, "client", return_value=fake):
        escalation.send_outcome(_incident(current_task_token="tok"), "RESOLVE", actor="42")
    fake.send_task_success.assert_called_once()
    assert '"actor": "42"' in fake.send_task_success.call_args.kwargs["output"]


def test_send_outcome_swallows_already_consumed_token(settings):
    settings.ESCALATION_LOCAL_MODE = False

    class TaskDoesNotExist(Exception):
        pass

    fake = mock.Mock()
    fake.exceptions.TaskDoesNotExist = TaskDoesNotExist
    fake.send_task_success.side_effect = TaskDoesNotExist()
    with mock.patch.object(escalation.boto3, "client", return_value=fake):
        escalation.send_outcome(_incident(current_task_token="tok"), "RESOLVE", actor="9")
