"""
Local stand-in for the SQS intake consumer (spec §4.1).

In prod the consumer is an SQS-triggered worker; locally this command lets you push
a payload through the same idempotent create + execution-start path without a queue.

    python manage.py consume_intake --source sumo --title "Disk full" \\
        --event-id alert-123 --payload '{"host": "web-1"}'
"""
import json

from django.core.management.base import BaseCommand

from incidents import escalation
from incidents.intake import create_incident_idempotent


class Command(BaseCommand):
    help = "Create an incident from a payload via the idempotent intake path."

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True)
        parser.add_argument("--title", required=True)
        parser.add_argument("--event-id", default="")
        parser.add_argument("--payload", default="{}")

    def handle(self, *args, **opts):
        incident, created = create_incident_idempotent(
            source=opts["source"],
            title=opts["title"],
            payload=json.loads(opts["payload"]),
            source_event_id=opts["event_id"] or None,
        )
        if created:
            incident.escalation_execution_arn = escalation.start_escalation(incident)
            incident.save(update_fields=["escalation_execution_arn", "updated_at"])
        verb = "created" if created else "deduped (no-op)"
        self.stdout.write(self.style.SUCCESS(f"{verb}: {incident.id} [{incident.status}]"))
