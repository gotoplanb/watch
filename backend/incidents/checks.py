"""
Session Check service (ADR-022) — the one decision implementation, shared by the local synchronous
path and (later) the cloud SQS worker, mirroring the escalation-services discipline (ADR-010).

Given a session correlation id or a user id, look up the trace backend for error spans in a window
and record the verdict. Stores only hashes — no plaintext PII. `run_session_check` is idempotent
enough to retry: it clears prior spans and re-derives the verdict.
"""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import events, trace_store
from .models import CheckStatus, CheckSubjectKind, ErrorSpan, SessionCheck
from .session_tagging import hash_user_id


def _subject_hash(subject_kind: str, subject_raw: str) -> str:
    """Session subjects use the (already non-secret) correlation id verbatim; user subjects are
    HMAC'd so no plaintext id is ever stored or queried (ADR-022)."""
    if subject_kind == CheckSubjectKind.USER:
        return hash_user_id(subject_raw)
    return subject_raw


def create_check(
    *, subject_kind: str, subject_raw: str, window_from=None, window_to=None, source: str = "manual"
) -> SessionCheck:
    """Create a queued SessionCheck. Defaults the window to [now - lookback, now]."""
    now = timezone.now()
    if window_to is None:
        window_to = now
    if window_from is None:
        window_from = window_to - timedelta(seconds=settings.CHECKS_DEFAULT_LOOKBACK_SECONDS)
    return SessionCheck.objects.create(
        subject_kind=subject_kind,
        subject_hash=_subject_hash(subject_kind, subject_raw),
        window_from=window_from,
        window_to=window_to,
        source=source,
    )


def _aged_out(window_to) -> bool:
    horizon = timezone.now() - timedelta(seconds=settings.CHECKS_TRACE_RETENTION_SECONDS)
    return bool(window_to) and window_to < horizon


@transaction.atomic
def run_session_check(check: SessionCheck) -> SessionCheck:
    """Query the trace backend for error spans and record the verdict. A window past retention is
    `aged_out`; a backend that can't answer is `indeterminate` — never a false `clean`."""
    check = SessionCheck.objects.select_for_update().get(pk=check.pk)
    if not check.subject_hash:
        return _finish(check, CheckStatus.INDETERMINATE, "no_subject")
    if _aged_out(check.window_to):
        return _finish(check, CheckStatus.INDETERMINATE, "aged_out")

    check.status = CheckStatus.RUNNING
    check.save(update_fields=["status", "updated_at"])
    try:
        spans = trace_store.find_error_spans(
            check.subject_kind, check.subject_hash, check.window_from, check.window_to
        )
    except trace_store.TraceStoreError:
        return _finish(check, CheckStatus.INDETERMINATE, "unavailable")

    check.error_spans.all().delete()  # idempotent re-run
    ErrorSpan.objects.bulk_create(
        [
            ErrorSpan(
                session_check=check,
                trace_id=s.get("trace_id", ""),
                span_id=s.get("span_id", ""),
                name=s.get("name", ""),
                service=s.get("service", ""),
                status=s.get("status", "ERROR"),
                http_status=s.get("http_status"),
                ts=s.get("ts"),
            )
            for s in spans
        ]
    )
    verdict = "clean" if not spans else f"errors_found:{len(spans)}"
    return _finish(check, CheckStatus.DONE, verdict)


def _finish(check: SessionCheck, status: str, verdict: str) -> SessionCheck:
    check.status = status
    check.verdict = verdict
    check.save(update_fields=["status", "verdict", "updated_at"])
    events.emit("check.completed", {
        "check_id": str(check.id), "subject_kind": check.subject_kind,
        "status": status, "verdict": verdict,
    })
    return check


def create_and_run(**kwargs) -> SessionCheck:
    """Create a check and, in local mode, run it synchronously (the cloud path enqueues instead)."""
    check = create_check(**kwargs)
    if settings.CHECKS_LOCAL_MODE:
        run_session_check(check)
        check.refresh_from_db()
    return check
