import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import assessments, communications, diagnostics, evidence_capture
from .models import Assessment, AssessmentCorrelation, AssessmentPolicy, ServiceDependency

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
# Still being worked: open, or promoted with its incident still open.
STILL_OPEN = ~Q(status__in=CLOSED_STATUSES) | Q(status="promoted", incident__status="OPEN")


class Routed(tuple):
    """(assessment, created), plus how the signal reached it: created, duplicate (a retried
    delivery), attached (another source's signal for a scope already being assessed, ADR-060)
    or correlated (a re-fire or recovery of the same condition, ADR-059)."""

    def __new__(cls, row, how):
        routed = super().__new__(cls, (row, how == "created"))
        routed.how = how
        return routed


def correlated(source, correlation_key):
    """Assessments still being worked for this condition, found by their own key or a key they
    took on from another source (ADR-060). Fan-out shares one key, so a supplier recovery reaches
    every scope."""
    own = Q(source=source, correlation_key=correlation_key)
    taken_on = Q(correlations__source=source, correlations__key=correlation_key)
    return list(Assessment.objects.filter(STILL_OPEN).filter(own | taken_on).distinct()
                .select_related("incident").order_by("created_at"))


def recheck(row):
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
    if resolved and recheck(row):
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


def _open_for_scope(customer, service):
    """The assessment already answering "are this customer's users of this service affected?"."""
    if not (customer and service):
        return None  # unscoped signals never merge: there's no shared question to attach to
    return (Assessment.objects.filter(STILL_OPEN, customer=customer, service=service)
            .order_by("-created_at").first())


@transaction.atomic
def _attach_related(row, data):
    """Another source's signal for a scope already being assessed is evidence on that assessment,
    not a second assessment (ADR-060). Its key is taken on, so its re-fire or recovery finds it."""
    row = Assessment.objects.select_for_update().get(pk=row.pk)
    event_id = data.get("source_event_id") or ""
    if event_id and row.history.filter(data__source_event_id=event_id).exists():
        return row
    label = f"{data['source']}" + (f" ({data['supplier']})" if data.get("supplier") else "")
    assessments.append(row, "related_signal", f"{label}: {data['title']}\n{data['signal']}", SIGNALS_ACTOR,
                       {"source": data["source"], "source_event_id": event_id,
                        "correlation_key": data.get("correlation_key", ""),
                        "supplier": data.get("supplier", ""), "payload": data.get("payload") or {}})
    if data.get("correlation_key"):
        AssessmentCorrelation.objects.get_or_create(assessment=row, source=data["source"],
                                                    key=data["correlation_key"])
    row.save(update_fields=["updated_at"])
    return row


def _route_scope(data, customer, service):
    scoped = {**data, "customer": customer, "service": service}
    if data.get("source_event_id"):
        scoped["source_event_id"] = f"{data['source_event_id']}:{customer}:{service}"
    is_retry = Assessment.objects.filter(dedupe_key=assessments.dedupe_key(**scoped)).exists()
    existing = None if is_retry else _open_for_scope(customer, service)
    if existing is not None:
        return Routed(_attach_related(existing, scoped), "attached")
    assessment, created = assessments.receive(**scoped)
    if created:
        schedule(assessment)
    return Routed(assessment, "created" if created else "duplicate")


def receive_signal(*, correlation_key="", state=ACTIVE, evidence_url="", **data):
    """Fan supplier notices into independently evidenced customer/service assessments.

    A re-fire or recovery of a condition already being assessed attaches to it (ADR-059), and a
    signal for a customer/service scope already being assessed is evidence there, not a new
    assessment (ADR-060). A cited status page is linked, and screenshotted if allow-listed."""
    from . import heartbeats

    heartbeats.seen(data["source"])  # any signal is a sign of life (ADR-062)
    if correlation_key:
        attached = correlate(correlation_key=correlation_key, state=state, **data)
        if attached or state == RESOLVED:
            results = [Routed(row, "correlated") for row in attached]
            _link_evidence(evidence_url, results)
            return results
    data["correlation_key"] = correlation_key
    results = [_route_scope(data, customer, service) for customer, service in _scopes(data)]
    _link_evidence(evidence_url, results)
    return results


def _link_evidence(url, results):
    rows = [routed[0] for routed in results if routed.how != "duplicate"]
    if url and rows:
        evidence_capture.link(url, rows)


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
