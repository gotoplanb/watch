import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import assessments, communications, diagnostics
from .models import Assessment, AssessmentPolicy, ServiceDependency

logger = logging.getLogger(__name__)

AUTOMATION_ACTOR = "system:automation"
CLOSED_STATUSES = frozenset({"no_issue", "promoted"})


def policy_for(assessment):
    choices = AssessmentPolicy.objects.filter(active=True)
    for key in ("customer", "service", "supplier"):
        choices = choices.filter(Q(**{key: ""}) | Q(**{key: getattr(assessment, key)}))
    ranked = sorted(choices, key=lambda p: (-sum(bool(getattr(p, k)) for k in ("customer", "service", "supplier")), p.pk))
    return ranked[0] if ranked else None


def schedule(assessment):
    policy = policy_for(assessment)
    if not policy:
        return False
    Assessment.objects.filter(pk=assessment.pk, automation_state="manual").update(
        automation_state="queued", automation_policy=policy,
    )
    return True


SIGNALS_ACTOR = "system:signals"
ACTIVE, RESOLVED = "active", "resolved"


def correlated(source, correlation_key):
    """Assessments still being worked for this condition: open ones, and promoted ones whose
    incident is still open. Fan-out shares one key, so a supplier recovery reaches every scope."""
    still_open = ~Q(status__in=CLOSED_STATUSES) | Q(status="promoted", incident__status="OPEN")
    return list(Assessment.objects.filter(still_open, source=source, correlation_key=correlation_key)
                .select_related("incident").order_by("created_at"))


def _recheck(row):
    """Rerun the policy's checks so our own telemetry, not the monitor's recovery, decides."""
    policy = policy_for(row)
    if row.status in CLOSED_STATUSES or policy is None or row.automation_state in {"queued", "running"}:
        return False
    row.automation_state, row.automation_policy = "queued", policy
    row.save(update_fields=["automation_state", "automation_policy", "updated_at"])
    return True


def _attach(row, state, data):
    """Record a re-fire or recovery on an assessment already working this condition (ADR-059)."""
    event_id = data.get("source_event_id") or ""
    if event_id and row.history.filter(data__source_event_id=event_id).exists():
        return  # a retried delivery of the same event
    resolved = state == RESOLVED
    note = data["signal"]
    if resolved and row.status == "promoted":
        note += f"\n[Source reports recovery. {row.incident.number} stays open until a responder resolves it.]"
    assessments.append(row, "signal_resolved" if resolved else "signal_repeated", note, SIGNALS_ACTOR,
                       {"source_event_id": event_id, "correlation_key": row.correlation_key,
                        "state": state, "payload": data.get("payload") or {}})
    if resolved and _recheck(row):
        assessments.append(row, "recheck_requested",
                           "Rechecking with the policy's diagnostics before any disposition.", SIGNALS_ACTOR)
    row.save(update_fields=["updated_at"])


@transaction.atomic
def correlate(*, correlation_key, state, **data):
    """A signal for a condition we're already assessing attaches to it instead of opening another.
    Returns the rows it attached to; a recovery with nothing open opens nothing."""
    rows = [Assessment.objects.select_for_update().get(pk=row.pk)
            for row in correlated(data["source"], correlation_key)]
    for row in rows:
        _attach(row, state, data)
    return rows


def _scopes(data):
    """The customer/service pairs a signal concerns: a supplier's dependents (narrowed by any scope
    the signal names), else the signal's own scope."""
    supplier = data.get("supplier", "")
    if not supplier:
        return [(data.get("customer", ""), data.get("service", ""))]
    dependencies = ServiceDependency.objects.filter(active=True, supplier=supplier)
    for key in ("customer", "service"):
        if data.get(key):
            dependencies = dependencies.filter(**{key: data[key]})
    return [(d.customer, d.service) for d in dependencies] or [(data.get("customer", ""), data.get("service", ""))]


def receive_signal(*, correlation_key="", state=ACTIVE, **data):
    """Fan supplier notices into independently evidenced customer/service assessments. With a
    correlation key, a re-fire or recovery of a condition already being assessed attaches to it."""
    if correlation_key:
        attached = correlate(correlation_key=correlation_key, state=state, **data)
        if attached or state == RESOLVED:
            return [(row, False) for row in attached]
    data["correlation_key"] = correlation_key
    results = []
    for customer, service in _scopes(data):
        scoped = {**data, "customer": customer, "service": service}
        if data.get("source_event_id"):
            scoped["source_event_id"] = f"{data['source_event_id']}:{customer}:{service}"
        assessment, created = assessments.receive(**scoped)
        if created:
            schedule(assessment)
        results.append((assessment, created))
    return results


def _run_reason(run):
    if run.state != "done":
        return run.error
    return "threshold breached" if run.result["breached"] else "within configured bounds"


def _summarize(runs):
    """Pure verdict computation: (verdict, confidence, reason, breached, failed)."""
    done = [r for r in runs if r.state == "done"]
    failed = len(done) != len(runs)
    breached = any(r.result.get("breached") for r in done)
    if breached:
        verdict = "degraded"
    elif failed:
        verdict = "unknown"
    else:
        verdict = "no_issue"
    if not failed:
        confidence = "high"
    elif done:
        confidence = "medium"
    else:
        confidence = "low"
    reason = "\n".join(f"{r.rule.name}: {_run_reason(r)}" for r in runs)
    return verdict, confidence, reason, breached, failed


def _impact(policy, breached, failed):
    if breached:
        return policy.impact
    return "unknown" if failed else "none"


def _claim(pk):
    """Move a queued assessment to running; returns (assessment, policy, started) or None."""
    with transaction.atomic():
        assessment = Assessment.objects.select_for_update().get(pk=pk)
        if assessment.automation_state != "queued":
            return None
        if assessment.status in CLOSED_STATUSES:
            assessment.automation_state = "complete"
            assessment.save(update_fields=["automation_state", "updated_at"])
            assessments.append(assessment, "automation_cancelled", "Assessment was already closed or promoted; queued disposition cancelled.", AUTOMATION_ACTOR)
            return None
        assessment.automation_state = "running"
        assessment.save(update_fields=["automation_state", "updated_at"])
        policy = assessment.automation_policy
        started = assessments.append(assessment, "assessment_started", f"Automated assessment started using policy: {policy.name if policy else 'missing'}.", AUTOMATION_ACTOR)
    return assessment, policy, started


def _rules(assessment, policy):
    if not assessment.customer or not assessment.service:
        raise ValueError("Customer and service scope are required for automatic impact assessment.")
    rules = list(policy.rules.all()) if policy and policy.active else []
    if not rules or len(rules) > 12:
        raise ValueError("No active policy with diagnostic checks is configured.")
    return rules


def _dispose(pk, policy, verdict, breached, reason):
    if breached and policy.auto_promote:
        assessments.promote(pk, actor=AUTOMATION_ACTOR, reason=reason)
    elif verdict == "no_issue" and policy.auto_close_no_issue and not policy.recipients:
        assessments.dispose(pk, actor=AUTOMATION_ACTOR, outcome="no_issue", reason=reason)
    elif verdict == "unknown":
        assessments.dispose(pk, actor=AUTOMATION_ACTOR, outcome="monitoring", reason=reason)


def _conclude(pk, assessment, policy, runs):
    verdict, confidence, reason, breached, failed = _summarize(runs)
    assessments.conclude(pk, actor=AUTOMATION_ACTOR, verdict=verdict, confidence=confidence, reason=reason)
    assessment.refresh_from_db()
    assessment.impact = _impact(policy, breached, failed)
    assessment.severity = policy.severity
    assessment.automation_state = "complete"
    assessment.recurrence_key = f"policy:{policy.pk}:{assessment.customer}:{assessment.service}"[:128]
    assessment.save(update_fields=["impact", "severity", "automation_state", "recurrence_key"])
    if policy.recipients:
        subject, body = communications.draft_text(assessment)
        communications.draft(pk, actor=AUTOMATION_ACTOR, subject=subject, body=body,
                             recipients=policy.recipients, review_seconds=policy.review_seconds,
                             sla_seconds=policy.communication_sla_seconds, auto_send=policy.auto_send,
                             purpose="initial")
    _dispose(pk, policy, verdict, breached, reason)


def _diagnose(pk, assessment, policy, started):
    rules = _rules(assessment, policy)
    runs = [diagnostics.execute(assessment, rule) for rule in rules]
    with transaction.atomic():
        assessment = Assessment.objects.select_for_update().get(pk=pk)
        if assessment.automation_state != "running":
            return
        policy.refresh_from_db()
        if not policy.active:
            raise ValueError("The policy was disabled during diagnosis.")
        if assessment.history.filter(pk__gt=started.pk).exclude(actor__startswith="system").exists():
            assessment.automation_state = "complete"
            assessment.save(update_fields=["automation_state"])
            assessments.append(assessment, "automation_review_required", "A responder intervened during diagnosis. Results are retained; disposition remains with the responder.", AUTOMATION_ACTOR)
            return
        if assessment.status in CLOSED_STATUSES:
            assessment.automation_state = "complete"
            assessment.save(update_fields=["automation_state"])
            return
        _conclude(pk, assessment, policy, runs)


def _record_failure(pk, exc):
    with transaction.atomic():
        assessment = Assessment.objects.select_for_update().get(pk=pk)
        assessment.automation_state = "failed"
        assessment.save(update_fields=["automation_state", "updated_at"])
        assessments.append(assessment, "automation_failed", "Assessment needs operator attention; automatic processing did not complete.",
                           AUTOMATION_ACTOR, {"error_type": type(exc).__name__})


def run(pk):
    claimed = _claim(pk)
    if claimed is None:
        return
    assessment, policy, started = claimed
    try:
        _diagnose(pk, assessment, policy, started)
    except Exception as exc:
        logger.exception("Automated assessment failed: %s", pk)
        _record_failure(pk, exc)


def finalize_no_issue(pk):
    with transaction.atomic():
        row = Assessment.objects.select_for_update().get(pk=pk)
        if (row.status not in CLOSED_STATUSES and row.verdict == "no_issue" and row.automation_policy
                and row.automation_policy.active and row.automation_policy.auto_close_no_issue and row.communications.filter(state="sent", purpose="initial").exists()):
            assessments.dispose(pk, actor=AUTOMATION_ACTOR, outcome="no_issue",
                                reason="Configured checks found no degradation; the initial communication was delivered.")
