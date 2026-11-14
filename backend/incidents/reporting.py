"""Separate signal/assessment cohorts from confirmed incident timing and communication SLAs."""
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import mean

from django.utils import timezone

from .models import Assessment, Communication, RemediationTask, Transition


def _average(values):
    return round(mean(values) / 60, 2) if values else None


def _percent(part, total):
    return round(part / total * 100, 1) if total else None


def _scope(*, customer, service, supplier, severity, include_examples):
    scope = Assessment.objects.all()
    for key, value in (("customer", customer), ("service", service), ("supplier", supplier), ("severity", severity)):
        if value:
            scope = scope.filter(**{key: value})
    if not include_examples:
        scope = scope.exclude(pk__in=Assessment.objects.filter(payload__fixture=True).values("pk")).exclude(pk__in=Assessment.objects.filter(payload__original_payload__fixture=True).values("pk"))
    return scope


def _first_transitions(ids):
    """First acknowledgement and first resolution per incident."""
    acks, resolutions = {}, {}
    for item in Transition.objects.filter(incident_id__in=ids).order_by("at", "pk"):
        if item.from_tier == item.to_tier and item.from_status == item.to_status == "OPEN":
            acks.setdefault(item.incident_id, item.at)
        if item.to_status == "RESOLVED":
            resolutions.setdefault(item.incident_id, item.at)
    return acks, resolutions


def _clock_seconds(event_times, row):
    at = event_times.get(row.incident_id)
    return (at - row.promoted_at).total_seconds() if at else None


def _incident_clocks(incident_cohort, acks, resolutions, by_supplier):
    """Return (ack_seconds, resolve_seconds, invalid_clocks); feeds supplier MTTR durations."""
    ack_seconds, resolve_seconds, invalid_clocks = [], [], 0
    for row in incident_cohort:
        for event_times, values in ((acks, ack_seconds), (resolutions, resolve_seconds)):
            seconds = _clock_seconds(event_times, row)
            if seconds is None:
                continue
            if seconds < 0:
                invalid_clocks += 1
                continue
            values.append(seconds)
            if event_times is resolutions:
                by_supplier[row.supplier or "Unassigned"]["durations"].append(seconds)
    return ack_seconds, resolve_seconds, invalid_clocks


def _incident_counts(incident_cohort):
    """Return (by_supplier, timeline, severity_counts, recurrence) counters."""
    by_supplier = defaultdict(lambda: {"incidents": 0, "durations": []})
    timeline, severity_counts, recurrence = Counter(), Counter(), Counter()
    for row in incident_cohort:
        severity_counts[row.severity] += 1
        timeline[row.promoted_at.date().isoformat()] += 1
        by_supplier[row.supplier or "Unassigned"]["incidents"] += 1
        if row.recurrence_key:
            recurrence[row.recurrence_key] += 1
    return by_supplier, timeline, severity_counts, recurrence


def _assessment_review(assessment_cohort):
    """Return (auto_no_issue, human_reviewed)."""
    auto_no_issue, human_reviewed = 0, 0
    for row in assessment_cohort:
        human = any(not event.actor.startswith("system") for event in row.history.all())
        human_reviewed += human
        if row.status == "no_issue" and not human:
            auto_no_issue += 1
    return auto_no_issue, human_reviewed


def _records(assessment_cohort, incident_cohort):
    rows = sorted({r.pk: r for r in assessment_cohort + incident_cohort}.values(), key=lambda r: r.created_at, reverse=True)[:100]
    return [{"id": str(row.pk), "number": row.number, "title": row.title, "customer": row.customer,
             "service": row.service, "source": row.source, "status": row.status,
             "incident_number": row.incident.number if row.incident_id else None} for row in rows]


def summarize(*, days=30, customer="", service="", supplier="", severity="", include_examples=False):
    start, end = timezone.now() - timedelta(days=days), timezone.now()
    scope = _scope(customer=customer, service=service, supplier=supplier, severity=severity,
                   include_examples=include_examples)
    assessment_cohort = list(scope.filter(created_at__gte=start, created_at__lte=end).prefetch_related("history"))
    incident_cohort = list(scope.filter(incident__isnull=False, promoted_at__gte=start, promoted_at__lte=end).select_related("incident"))
    acks, resolutions = _first_transitions([row.incident_id for row in incident_cohort])
    by_supplier, timeline, severity_counts, recurrence = _incident_counts(incident_cohort)
    ack_seconds, resolve_seconds, invalid_clocks = _incident_clocks(incident_cohort, acks, resolutions, by_supplier)
    outcomes = Counter(row.status for row in assessment_cohort)
    auto_no_issue, human_reviewed = _assessment_review(assessment_cohort)
    mails = list(Communication.objects.filter(assessment__in=scope, created_at__gte=start, created_at__lte=end))
    sent = [mail for mail in mails if mail.state == "sent"]
    overdue = sum(mail.state in {"review", "expired"} and mail.sla_deadline < end for mail in mails)
    auto_sent = sum(mail.is_automatic for mail in sent)
    corrective = RemediationTask.objects.filter(assessment__in=scope)
    incident_pks = [row.pk for row in incident_cohort]
    final_rcas = scope.filter(pk__in=incident_pks, rca_projection__state="final").count()
    for value in by_supplier.values():
        value["mttr_minutes"] = _average(value.pop("durations"))
    total = len(assessment_cohort)
    return {
        "days": days, "start": start.isoformat(), "end": end.isoformat(), "assessments": total,
        "incidents": len(incident_cohort), "outcomes": dict(outcomes),
        "conversion_percent": _percent(outcomes["promoted"], total),
        "automatic_no_issue": auto_no_issue, "human_reviewed": human_reviewed,
        "automation_percent": _percent(auto_no_issue, total),
        "mtta_minutes": _average(ack_seconds), "mttr_minutes": _average(resolve_seconds),
        "acknowledged_sample": len(ack_seconds), "resolved_sample": len(resolve_seconds),
        "invalid_clock_records": invalid_clocks, "severity_counts": dict(severity_counts),
        "suppliers": dict(by_supplier), "daily_incidents": dict(sorted(timeline.items())),
        "communications_sent": len(sent), "auto_sent": auto_sent,
        "communication_sla_percent": _percent(sum(bool(mail.sla_met) for mail in sent), len(sent)),
        "communications_overdue": overdue,
        "remediations": corrective.count(), "remediations_complete": corrective.filter(state="done").count(),
        "remediations_overdue": corrective.exclude(state="done").filter(due_date__lt=end.date()).count(),
        "final_rcas": final_rcas, "recurrences": sum(n - 1 for n in recurrence.values() if n > 1),
        "source_counts": dict(Counter(row.source for row in incident_cohort)),
        "service_counts": dict(Counter(row.service or "Unassigned" for row in incident_cohort)),
        "customer_counts": dict(Counter(row.customer or "Unassigned" for row in incident_cohort)),
        "inconclusive": outcomes["monitoring"],
        "communication_auto_send_percent": _percent(auto_sent, len(sent)),
        "records": _records(assessment_cohort, incident_cohort),
        "bridge_cases": scope.filter(pk__in=incident_pks, history__kind="bridge_opened").distinct().count(),
        "include_examples": include_examples,
    }
