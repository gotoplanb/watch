"""
Async job queue seam (ADR-025) — the one place Watch hands work to the cloud worker.

Provider discipline mirrors `flags`/`trace_store` (ADR-003 spirit): the domain calls
`enqueue(kind, id)` and never imports boto3 directly. `local` is a no-op (local mode runs the
work inline and never enqueues); `sqs` sends `{"kind","id"}` to `WATCH_QUEUE_URL`. Enqueue is
**guarded** — a queue hiccup is logged, never raised into the domain (the durable row already
exists; a sweep/redrive can recover it).
"""
import json
import logging
import threading

from django.conf import settings

logger = logging.getLogger(__name__)

_provider_override = None  # test seam (set_provider_for_tests)

VALID_KINDS = ("check", "delivery", "handoff")


def set_provider_for_tests(provider):
    """Override the provider in tests (a callable taking (kind, id) or None to reset)."""
    global _provider_override
    _provider_override = provider


def is_async() -> bool:
    """True when something will actually pick a job up. The `local` provider is a no-op, so a
    caller whose work is *visible* while pending (the handoff brief's placeholder card, ADR-042)
    must run inline instead of enqueueing into the void."""
    return _provider_override is not None or settings.QUEUE_PROVIDER in ("sqs", "thread")


def _sqs_send(kind: str, obj_id) -> None:
    import boto3  # imported lazily so the domain/unit path never needs botocore

    client = boto3.client("sqs", region_name=settings.AWS_REGION)
    client.send_message(
        QueueUrl=settings.WATCH_QUEUE_URL,
        MessageBody=json.dumps({"kind": kind, "id": str(obj_id)}),
    )


def _thread_run(kind: str, obj_id) -> None:
    """Local-dev async (ADR-042): run the job off the request thread so `make dev` exercises the
    same enqueue → placeholder → poll UX as cloud, without SQS. **Not durable** — a crash loses the
    job — which is exactly why prod uses `sqs`."""
    def work():
        from django.db import connection
        try:
            run_job(kind, obj_id)
        except Exception:
            logger.warning("thread worker: job failed %s:%s", kind, obj_id, exc_info=True)
        finally:
            connection.close()  # each thread gets its own connection; don't leak it

    threading.Thread(target=work, daemon=True, name=f"watch-job-{kind}").start()


def enqueue(kind: str, obj_id) -> None:
    """Hand a job to the worker. No-op under the `local` provider; guarded so a send failure can
    never break the caller's transaction (the row survives for a redrive)."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown job kind: {kind}")
    try:
        if _provider_override is not None:
            _provider_override(kind, obj_id)
        elif settings.QUEUE_PROVIDER == "sqs":
            _sqs_send(kind, obj_id)
        elif settings.QUEUE_PROVIDER == "thread":
            _thread_run(kind, obj_id)
        # provider "local"/anything else: no-op — work ran (or will run) inline
    except Exception:  # pragma: no cover - defensive: enqueue must never break the domain
        logger.warning("queue.enqueue failed for %s:%s", kind, obj_id, exc_info=True)


def run_job(kind: str, obj_id) -> None:
    """Worker-side dispatch (ADR-025): route a `{kind, id}` job to the one services implementation.
    Imports are lazy to avoid a module-load cycle (checks/events import this module). Raising
    propagates to the worker loop, which leaves the SQS message for redrive → DLB."""
    from . import checks, events, services
    from .models import SessionCheck, WebhookDelivery

    if kind == "check":
        checks.run_session_check(SessionCheck.objects.get(pk=obj_id))
    elif kind == "delivery":
        events.redeliver(WebhookDelivery.objects.get(pk=obj_id))
    elif kind == "handoff":
        services.write_handoff_brief(obj_id)
    else:
        raise ValueError(f"unknown job kind: {kind}")
