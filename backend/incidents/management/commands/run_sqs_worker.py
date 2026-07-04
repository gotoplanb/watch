"""
Async job worker (ADR-025) — the cloud consumer for Session Check + webhook delivery.

Runs as the **same image, a different command** (build-once/promote-by-digest): it long-polls
`WATCH_QUEUE_URL`, dispatches each `{kind, id}` to the one services implementation via
`queue.run_job`, and `DeleteMessage`s on success. A job that raises is left un-deleted so SQS
redelivers it after the visibility timeout; after the queue's `maxReceiveCount` it redrives to the
DLQ. Idempotent consumers (ADR-025) make at-least-once safe.

    python manage.py run_sqs_worker            # long-poll forever
    python manage.py run_sqs_worker --once      # one receive batch then exit (smoke)
"""
import json
import logging
import signal

import boto3
from django.conf import settings
from django.core.management.base import BaseCommand

from incidents import queue

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Drain the async job queue (Session Check + webhook delivery) — ADR-025."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="one receive batch then exit")

    def handle(self, *args, **opts):
        if not settings.WATCH_QUEUE_URL:
            raise SystemExit("WATCH_QUEUE_URL is not set")
        client = boto3.client("sqs", region_name=settings.AWS_REGION)
        self._running = True
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        self.stdout.write(f"worker: draining {settings.WATCH_QUEUE_URL}")
        while self._running:
            resp = client.receive_message(
                QueueUrl=settings.WATCH_QUEUE_URL,
                MaxNumberOfMessages=settings.WORKER_BATCH_SIZE,
                WaitTimeSeconds=settings.WORKER_WAIT_SECONDS,
                VisibilityTimeout=settings.WORKER_VISIBILITY_SECONDS,
            )
            for msg in resp.get("Messages", []):
                self._handle(client, msg)
            if opts["once"]:
                break

    def _handle(self, client, msg):
        try:
            body = json.loads(msg["Body"])
            queue.run_job(body["kind"], body["id"])
        except Exception:
            logger.exception("worker: job failed, leaving for redrive: %s", msg.get("Body"))
            return  # do not delete → SQS redelivers → DLQ after maxReceiveCount
        client.delete_message(
            QueueUrl=settings.WATCH_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"]
        )

    def _stop(self, *_):
        self._running = False
