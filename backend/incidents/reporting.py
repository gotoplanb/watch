"""Separate signal/assessment cohorts from confirmed incident timing and communication SLAs."""
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import mean

from django.utils import timezone

from .models import Assessment, Communication, RemediationTask, Transition


def _average(values):
    return round(mean(values) / 60, 2) if values else None


def summarize(*, days=30, customer="", service="", supplier="", severity="", include_examples=False):
    start, end = timezone.now() - timedelta(days=days), timezone.now()
    scope = Assessment.objects.all()
    for key, value in (("customer", customer), ("service", service), ("supplier", supplier), ("severity", severity)):
        if value:
            scope = scope.filter(**{key: value})
    if not include_examples:
        scope = scope.exclude(pk__in=Assessment.objects.filter(payload__fixture=True).values("pk")).exclude(pk__in=Assessment.objects.filter(payload__original_payload__fixture=True).values("pk"))
    assessment_cohort = list(scope.filter(created_at__gte=start, created_at__lte=end).prefetch_related("history"))
    incident_cohort = list(scope.filter(incident__isnull=False, promoted_at__gte=start, promoted_at__lte=end).select_related("incident"))
    ids = [row.incident_id for row in incident_cohort]
    acks, resolutions = {}, {}
    for item in Transition.objects.filter(incident_id__in=ids).order_by("at", "pk"):
        if item.from_tier == item.to_tier and item.from_status == item.to_status == "OPEN":
            acks.setdefault(item.incident_id, item.at)
        if item.to_status == "RESOLVED":
            resolutions.setdefault(item.incident_id, item.at)
    ack_seconds, resolve_seconds, invalid_clocks = [], [], 0
    by_supplier = defaultdict(lambda: {"incidents": 0, "durations": []})
    timeline = Counter()
    severity_counts = Counter()
    recurrence = Counter()
    for row in incident_cohort:
        severity_counts[row.severity] += 1
        timeline[row.promoted_at.date().isoformat()] += 1
        by_supplier[row.supplier or "Unassigned"]["incidents"] += 1
        if row.recurrence_key:
            recurrence[row.recurrence_key] += 1
        for event_times, values in ((acks, ack_seconds), (resolutions, resolve_seconds)):
            at = event_times.get(row.incident_id)
            if at:
                seconds = (at - row.promoted_at).total_seconds()
                if seconds >= 0:
                    values.append(seconds)
                    if event_times is resolutions:
                        by_supplier[row.supplier or "Unassigned"]["durations"].append(seconds)
                else:
                    invalid_clocks += 1
    outcomes = Counter(row.status for row in assessment_cohort)
    auto_no_issue, human_reviewed = 0, 0
    for row in assessment_cohort:
        human = any(not event.actor.startswith("system") for event in row.history.all())
        human_reviewed += human
        if row.status == "no_issue" and not human:
            auto_no_issue += 1
    mails = list(Communication.objects.filter(assessment__in=scope, created_at__gte=start, created_at__lte=end))
    sent = [mail for mail in mails if mail.state == "sent"]
    overdue = sum(mail.state in {"review", "expired"} and mail.sla_deadline < end for mail in mails)
    corrective = RemediationTask.objects.filter(assessment__in=scope)
    final_rcas = scope.filter(pk__in=[row.pk for row in incident_cohort], rca_projection__state="final").count()
    for value in by_supplier.values():
        value["mttr_minutes"] = _average(value.pop("durations"))
    total = len(assessment_cohort)
    return {
        "days": days, "start": start.isoformat(), "end": end.isoformat(), "assessments": total,
        "incidents": len(incident_cohort), "outcomes": dict(outcomes),
        "conversion_percent": round(outcomes["promoted"] / total * 100, 1) if total else None,
        "automatic_no_issue": auto_no_issue, "human_reviewed": human_reviewed,
        "automation_percent": round(auto_no_issue / total * 100, 1) if total else None,
        "mtta_minutes": _average(ack_seconds), "mttr_minutes": _average(resolve_seconds),
        "acknowledged_sample": len(ack_seconds), "resolved_sample": len(resolve_seconds),
        "invalid_clock_records": invalid_clocks, "severity_counts": dict(severity_counts),
        "suppliers": dict(by_supplier), "daily_incidents": dict(sorted(timeline.items())),
        "communications_sent": len(sent), "auto_sent": sum(mail.is_automatic for mail in sent),
        "communication_sla_percent": round(sum(bool(mail.sla_met) for mail in sent) / len(sent) * 100, 1) if sent else None,
        "communications_overdue": overdue,
        "remediations": corrective.count(), "remediations_complete": corrective.filter(state="done").count(),
        "remediations_overdue": corrective.exclude(state="done").filter(due_date__lt=end.date()).count(),
        "final_rcas": final_rcas, "recurrences": sum(n - 1 for n in recurrence.values() if n > 1),
        "source_counts": dict(Counter(row.source for row in incident_cohort)),
        "service_counts": dict(Counter(row.service or "Unassigned" for row in incident_cohort)),
        "customer_counts": dict(Counter(row.customer or "Unassigned" for row in incident_cohort)),
        "inconclusive": outcomes["monitoring"],
        "communication_auto_send_percent": round(sum(mail.is_automatic for mail in sent) / len(sent) * 100, 1) if sent else None,
        "records": [{"id": str(row.pk), "number": row.number, "title": row.title, "customer": row.customer,
                     "service": row.service, "source": row.source, "status": row.status,
                     "incident_number": row.incident.number if row.incident_id else None} for row in sorted({r.pk: r for r in assessment_cohort + incident_cohort}.values(), key=lambda r: r.created_at, reverse=True)[:100]],
        "bridge_cases": scope.filter(pk__in=[row.pk for row in incident_cohort], history__kind="bridge_opened").distinct().count(),
        "include_examples": include_examples,
    }
