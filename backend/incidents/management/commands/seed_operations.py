"""Generic development scenarios; no external communication or paging."""
from datetime import timedelta
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from incidents import assessments, automation, communications, operations, services
from incidents.models import AssessmentPolicy, DiagnosticRule, ServiceDependency, SubjectArea

SUPPLIER = "Supplier A"
EXAMPLE_ACTOR = "system:example"


class Command(BaseCommand):
    help = "Seed fictional full-lifecycle scenarios, plus a saved real Watchtower health check."

    def handle(self, *args, **options):
        if not (settings.DEBUG and settings.ESCALATION_LOCAL_MODE):
            raise CommandError("Development examples require DEBUG and local escalation mode.")
        user = User.objects.filter(username="t2a").first()
        specialist = User.objects.filter(username="t3a").first()
        if not user or not specialist:
            raise CommandError("Run seed_demo first.")
        area, _ = SubjectArea.objects.get_or_create(name="Example Processing Specialists", defaults={"description": "Fictional processing expertise for local exercises."})
        area.members.add(specialist)
        healthy = self.rule("Example checkout volume · normal variability", "deviation", 20, {"value": 96.0, "baseline": 100, "samples": 1200})
        errors = self.rule("Example order errors · elevated", "above", 2, {"value": 3.7, "baseline": 0.2, "samples": 640})
        unknown = self.rule("Example low-traffic check · inconclusive", "above", 2, {"value": 0, "samples": 0})
        for customer, rule in [("Example Customer", healthy), ("Example Customer B", errors), ("Example Customer C", unknown)]:
            ServiceDependency.objects.get_or_create(customer=customer, service="Catalog", supplier=SUPPLIER)
            policy, _ = AssessmentPolicy.objects.get_or_create(name=f"Local example · {customer}", defaults={"customer": customer, "service": "Catalog", "supplier": SUPPLIER, "active": True, "auto_promote": True, "auto_close_no_issue": True, "auto_send": True, "recipients": ["operations@example.test"], "review_seconds": 300, "route_to_t2": True})
            policy.rules.set([rule])
        live, _ = DiagnosticRule.objects.get_or_create(name="Watchtower · Prometheus scrape availability", defaults={"provider": "prometheus", "query": 'up{job="prometheus"}', "sample_query": 'count(up{job="prometheus"})', "minimum_samples": 1, "comparator": "below", "threshold": 0.5})
        self.stdout.write(f"Saved live diagnostic: {live.name} (observability only, not checkout health).")
        results = automation.receive_signal(title="Supplier A advisory · customer-specific impact", source="supplier", signal="Fictional advisory: supplier reports degraded orders. Verify each customer's actual impact independently.", supplier=SUPPLIER, payload={"fixture": True}, source_event_id="operations-supplier-example-v1")
        for row, created in results:
            if created:
                automation.run(row.pk)
                row.refresh_from_db()
                mail = row.communications.first()
                if mail and row.verdict == "no_issue":
                    communications.send(mail.pk, actor=EXAMPLE_ACTOR)
                    automation.finalize_no_issue(row.pk)
            self.stdout.write(f"{row.number} → /ui/assessments/{row.pk}/")
        row, created = assessments.receive(title="Example recovery · processing backlog", source="monitor", signal="Synthetic elevated order failures in one region.", customer="Example Customer", service="Checkout", supplier=SUPPLIER, source_event_id="operations-rca-example-v1", payload={"fixture": True})
        if created:
            event = assessments.add_evidence(row.pk, actor=EXAMPLE_ACTOR, summary="Regional errors confirmed", query='order failures grouped by region; 15-minute window', result='Example region: 3.7% failures, baseline 0.2%, 640 requests. Other regions healthy.')
            assessments.conclude(row.pk, actor=EXAMPLE_ACTOR, verdict="degraded", confidence="high", reason="Regional request failures exceed the configured threshold.")
            incident, _ = assessments.promote(row.pk, actor=EXAMPLE_ACTOR, reason="Confirmed regional degradation.", dispatch=False)
            services.acknowledge(incident.pk, actor=str(user.pk), reason="Investigating regional connection failures.")
            services.escalate(incident.pk, actor=str(user.pk), reason="Tier 2 investigation required.")
            handoff = operations.engage(row.pk, user=user, area=area, reason="Synthetic processing backlog; inspect retry queue depth.", selected=[event.pk], request_id=uuid.uuid4())
            operations.respond_engagement(handoff.pk, user=specialist, action="ack")
            operations.respond_engagement(handoff.pk, user=specialist, action="complete", body="Fictional exercise: paused duplicate retries and verified queue recovery.")
            recovery = operations.record_action(row.pk, user=user, kind="recovery", body="Error rate returned to 0.2% for the observation window; orders recovered.", selected=[event.pk])
            services.resolve(incident.pk, actor=str(user.pk), reason="Retry queue drained; customer journey recovered.")
            operations.curate(row.pk, user=user, event=event, section="impact", note="Customer-scoped impact established before promotion.")
            operations.curate(row.pk, user=user, event=recovery, section="recovery")
            operations.save_rca(row.pk, user=user, summary="Fictional order-processing degradation resolved by draining duplicate retries.", impact="One example region affected.", root_cause="An invented retry loop caused a processing backlog.", contributing_factors="The fictional workload exceeded the demonstration retry budget.", detection_gaps="Add retry-queue coverage before journey errors rise.", state="final")
            operations.remediation(row.pk, user=user, title="Add retry-queue depth alert", owner=specialist, due_date=timezone.now().date() + timedelta(days=7))
        self.stdout.write(self.style.SUCCESS(f"RCA example → /ui/assessments/{row.pk}/?tab=rca"))

    def rule(self, name, comparator, threshold, result):
        rule, _ = DiagnosticRule.objects.get_or_create(name=name, defaults={"provider": "fixture", "query": "Example customer-scoped diagnostic ${customer} / ${service}", "minimum_samples": 100, "comparator": comparator, "threshold": threshold, "fixture_result": result})
        return rule
