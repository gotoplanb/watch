"""Fictional, repeatable examples of the assessment-first workflow."""
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from incidents import assessments, services


class Command(BaseCommand):
    help = "Load fictional assessment examples locally without sending messages or paging anyone."

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG or not settings.ESCALATION_LOCAL_MODE:
            raise CommandError("Assessment examples require local development settings")
        try:
            operator = User.objects.get(username="t2a")
        except User.DoesNotExist as exc:
            raise CommandError("Run make seed-dev to create the demo responders first") from exc
        rows = [
            ("normal-variation", "[EXAMPLE] Checkout volume below yesterday's level", "customer", "",
             "Customer asks whether lower checkout volume indicates a service issue.",
             "Comparable traffic windows and journey success checked.",
             "Synthetic example: volume is 4% lower; this is within the example baseline band. Search and checkout journeys pass; no correlated error increase.",
             "no_issue", "No service degradation found across the recorded journey and volume checks.", "no_issue"),
            ("supplier-advisory", "[EXAMPLE] Supplier advisory without confirmed customer impact", "supplier", "Supplier A",
             "Supplier A reports intermittent errors; customer impact has not been established.",
             "Customer-specific traffic sampled after the advisory.",
             "Synthetic example: search and checkout requests currently succeed. Volume is too low to exclude intermittent impact; supplier update remains pending.",
             "unknown", "Current checks show no impact, but the observation window is insufficient for a no-issue conclusion.", "monitoring"),
            ("checkout-errors", "[EXAMPLE] Sustained checkout errors need a decision", "monitor", "Supplier A",
             "A monitor reports an increase in failed checkouts.",
             "Checkout journey outcomes compared with the pre-alert window.",
             "Synthetic example: checkout failures rise from 0.2% to 3.7% across a 15-minute sample. Search succeeds; order creation fails repeatedly.",
             "degraded", "Repeated checkout failures establish customer impact; search health does not clear the checkout path.", "review"),
            ("regional-runtime", "[EXAMPLE] Shared runtime disrupts multiple journeys", "monitor", "",
             "Several operations degrade together in one region.",
             "Gateway errors, runtime health, and unaffected paths compared.",
             "Synthetic example: multiple journeys return internal gateway errors while one region loses healthy runtime targets. Other regions remain available; initiating cause is not yet known.",
             "degraded", "Shared runtime availability loss establishes regional customer impact; initiating cause is still under investigation.", "promote"),
        ]
        for key, title, source, supplier, signal, summary, result, verdict, conclusion, outcome in rows:
            assessment, created = assessments.receive(
                title=title, source=f"example:{source}", signal=signal, customer="Example Customer",
                service="Checkout", supplier=supplier, source_event_id=f"assessment-demo-v1:{key}",
                actor=operator.username, assignee=operator, payload={"fixture": True},
            )
            if created:
                assessments.add_evidence(assessment.pk, actor=operator.username, summary=summary,
                                         query="Illustrative completed check; no live query executed.",
                                         result=result, source="synthetic fixture")
                assessments.conclude(assessment.pk, actor=operator.username, verdict=verdict,
                                     confidence="medium" if verdict == "unknown" else "high", reason=conclusion)
                if outcome in {"no_issue", "monitoring"}:
                    assessments.dispose(assessment.pk, actor=operator.username, outcome=outcome,
                                        reason=conclusion)
                elif outcome == "promote":
                    incident, _ = assessments.promote(assessment.pk, actor=operator.username,
                                                      reason=conclusion, dispatch=False)
                    services.add_note(incident, actor=operator.username,
                                      body="Exercise: correlate failures across journeys before choosing a mitigation. Keep the infrastructure cause qualified.")
                    services.acknowledge(incident.pk, actor=str(operator.pk))
            assessment.refresh_from_db()
            self.stdout.write(f"{'Created' if created else 'Preserved'} {assessment.number}: {assessment.get_status_display()} — /ui/assessments/{assessment.pk}/")
