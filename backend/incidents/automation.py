import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import assessments, communications, diagnostics
from .models import Assessment, AssessmentPolicy, ServiceDependency

logger = logging.getLogger(__name__)


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


def receive_signal(**data):
    """Fan supplier notices into independently evidenced customer/service assessments."""
    supplier = data.get("supplier", "")
    dependencies = ServiceDependency.objects.filter(active=True, supplier=supplier) if supplier else []
    if supplier:
        if data.get("customer"):
            dependencies = dependencies.filter(customer=data["customer"])
        if data.get("service"):
            dependencies = dependencies.filter(service=data["service"])
    scopes = [(d.customer, d.service) for d in dependencies] or [(data.get("customer", ""), data.get("service", ""))]
    results = []
    for customer, service in scopes:
        scoped = {**data, "customer": customer, "service": service}
        if data.get("source_event_id"):
            scoped["source_event_id"] = f"{data['source_event_id']}:{customer}:{service}"
        assessment, created = assessments.receive(**scoped)
        if created:
            schedule(assessment)
        results.append((assessment, created))
    return results


def run(pk):
    with transaction.atomic():
        assessment = Assessment.objects.select_for_update().get(pk=pk)
        if assessment.automation_state != "queued":
            return
        if assessment.status in {"no_issue", "promoted"}:
            assessment.automation_state = "complete"
            assessment.save(update_fields=["automation_state", "updated_at"])
            assessments.append(assessment, "automation_cancelled", "Assessment was already closed or promoted; queued disposition cancelled.", "system:automation")
            return
        assessment.automation_state = "running"
        assessment.save(update_fields=["automation_state", "updated_at"])
        policy = assessment.automation_policy
        started = assessments.append(assessment, "assessment_started", f"Automated assessment started using policy: {policy.name if policy else 'missing'}.", "system:automation")
    try:
        if not assessment.customer or not assessment.service:
            raise ValueError("Customer and service scope are required for automatic impact assessment.")
        rules = list(policy.rules.all()) if policy and policy.active else []
        if not rules or len(rules) > 12:
            raise ValueError("No active policy with diagnostic checks is configured.")
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
                assessments.append(assessment, "automation_review_required", "A responder intervened during diagnosis. Results are retained; disposition remains with the responder.", "system:automation")
                return
            if assessment.status in {"no_issue", "promoted"}:
                assessment.automation_state = "complete"
                assessment.save(update_fields=["automation_state"])
                return
            failed = any(r.state != "done" for r in runs)
            breached = any(r.result.get("breached") for r in runs if r.state == "done")
            verdict = "degraded" if breached else ("unknown" if failed else "no_issue")
            confidence = ("low" if all(r.state != "done" for r in runs) else "medium") if failed else "high"
            reason = "\n".join(f"{r.rule.name}: {r.error if r.state != 'done' else ('threshold breached' if r.result['breached'] else 'within configured bounds')}" for r in runs)
            assessments.conclude(pk, actor="system:automation", verdict=verdict, confidence=confidence, reason=reason)
            assessment.refresh_from_db()
            assessment.impact = policy.impact if breached else ("unknown" if failed else "none")
            assessment.severity = policy.severity
            assessment.automation_state = "complete"
            assessment.recurrence_key = f"policy:{policy.pk}:{assessment.customer}:{assessment.service}"[:128]
            assessment.save(update_fields=["impact", "severity", "automation_state", "recurrence_key"])
            if policy.recipients:
                subject, body = communications.draft_text(assessment)
                communications.draft(pk, actor="system:automation", subject=subject, body=body,
                                     recipients=policy.recipients, review_seconds=policy.review_seconds,
                                     sla_seconds=policy.communication_sla_seconds, auto_send=policy.auto_send,
                                     purpose="initial")
            if breached and policy.auto_promote:
                assessments.promote(pk, actor="system:automation", reason=reason)
            elif verdict == "no_issue" and policy.auto_close_no_issue and not policy.recipients:
                assessments.dispose(pk, actor="system:automation", outcome="no_issue", reason=reason)
            elif verdict == "unknown":
                assessments.dispose(pk, actor="system:automation", outcome="monitoring", reason=reason)
    except Exception as exc:
        logger.exception("Automated assessment failed: %s", pk)
        with transaction.atomic():
            assessment = Assessment.objects.select_for_update().get(pk=pk)
            assessment.automation_state = "failed"
            assessment.save(update_fields=["automation_state", "updated_at"])
            assessments.append(assessment, "automation_failed", "Assessment needs operator attention; automatic processing did not complete.",
                               "system:automation", {"error_type": type(exc).__name__})


def finalize_no_issue(pk):
    with transaction.atomic():
        row = Assessment.objects.select_for_update().get(pk=pk)
        if (row.status not in {"no_issue", "promoted"} and row.verdict == "no_issue" and row.automation_policy
                and row.automation_policy.active and row.automation_policy.auto_close_no_issue and row.communications.filter(state="sent", purpose="initial").exists()):
            assessments.dispose(pk, actor="system:automation", outcome="no_issue",
                                reason="Configured checks found no degradation; the initial communication was delivered.")
