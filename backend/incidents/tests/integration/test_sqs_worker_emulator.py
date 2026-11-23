"""The SQS worker path against the opt-in AWS Emulator (ADR-067, ADR-025).

`QUEUE_PROVIDER=sqs` and `run_sqs_worker` otherwise only run in AWS (watch#61). These tests use the
real code on both ends: `queue.enqueue` sends, the worker command receives, dispatches and deletes,
and a job that keeps failing redrives to the dead-letter queue. The job itself is replaced by a
recorder, because what's under test is the queue contract, not a particular job.

    make emulator-up && make emulator-test

Skipped unless the emulator is running. boto3 is pointed at it through environment variables set
only for each test, so nothing here can reach a real AWS account.
"""
import json
import os
import time
import uuid
from pathlib import Path

import pytest
from django.core.management import call_command

from incidents import queue

from ._reach import tcp_open

pytestmark = [pytest.mark.integration, pytest.mark.emulator]

PORT = int(os.environ.get("EMULATOR_PORT", "9297"))
FILES = Path.home() / ".aws" / "emulator" / str(PORT)


@pytest.fixture
def sqs(monkeypatch, settings):
    if not (tcp_open("127.0.0.1", PORT) and (FILES / "emulator_credentials").exists()):
        pytest.skip(f"AWS Emulator not running on 127.0.0.1:{PORT}; run `make emulator-up`")
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ENDPOINT_URL", f"https://127.0.0.1:{PORT}")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(FILES / "emulator_credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(FILES / "config"))
    monkeypatch.setenv("AWS_CA_BUNDLE", str(FILES / "ca.pem"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    settings.AWS_REGION = "us-east-1"
    settings.QUEUE_PROVIDER = "sqs"
    settings.WORKER_BATCH_SIZE, settings.WORKER_WAIT_SECONDS, settings.WORKER_VISIBILITY_SECONDS = 10, 1, 1
    import boto3

    return boto3.client("sqs", region_name="us-east-1")


def _queue(client, name, **attributes):
    return client.create_queue(QueueName=f"{name}-{uuid.uuid4().hex[:8]}", Attributes=attributes)["QueueUrl"]


def _count(client, url):
    attrs = client.get_queue_attributes(
        QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])
    return int(attrs["Attributes"]["ApproximateNumberOfMessages"]), int(
        attrs["Attributes"]["ApproximateNumberOfMessagesNotVisible"])


@pytest.fixture
def ran(monkeypatch):
    calls = []
    monkeypatch.setattr(queue, "run_job", lambda kind, obj_id: calls.append((kind, obj_id)))
    return calls


def test_enqueue_then_worker_runs_and_deletes_the_job(sqs, settings, ran):
    settings.WATCH_QUEUE_URL = _queue(sqs, "watch-work")
    queue.enqueue("check", 42)
    call_command("run_sqs_worker", "--once")
    assert ran == [("check", "42")]
    assert _count(sqs, settings.WATCH_QUEUE_URL) == (0, 0)  # deleted, not just hidden


def test_a_failing_job_redrives_to_the_dead_letter_queue(sqs, settings, monkeypatch):
    dlq = _queue(sqs, "watch-dlq")
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    settings.WATCH_QUEUE_URL = _queue(sqs, "watch-work", RedrivePolicy=json.dumps(
        {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "1"}))

    def boom(kind, obj_id):
        raise RuntimeError("job failed")

    monkeypatch.setattr(queue, "run_job", boom)
    queue.enqueue("delivery", 7)
    call_command("run_sqs_worker", "--once")  # fails: left on the queue, hidden for 1s
    assert _count(sqs, settings.WATCH_QUEUE_URL) == (0, 1)
    time.sleep(1.5)
    call_command("run_sqs_worker", "--once")  # past maxReceiveCount: moved to the DLQ
    moved = sqs.receive_message(QueueUrl=dlq, WaitTimeSeconds=2).get("Messages", [])
    assert [json.loads(m["Body"]) for m in moved] == [{"kind": "delivery", "id": "7"}]
