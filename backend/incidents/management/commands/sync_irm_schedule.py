"""Copy Grafana IRM's on-call schedules into Watch now (ADR-066 §7).

The operations worker does this every IRM_SYNC_INTERVAL_SECONDS; this runs one sync on demand.
"""
import json

from django.core.management.base import BaseCommand, CommandError

from incidents import irm_schedule


class Command(BaseCommand):
    help = "Copy the IRM on-call schedules into Watch now (needs GRAFANA_IRM_URL/TOKEN, IRM_SCHEDULE_IDS)."

    def handle(self, *args, **options):
        if not irm_schedule.enabled():
            raise CommandError("IRM schedule copy is off: set GRAFANA_IRM_URL, GRAFANA_IRM_TOKEN and IRM_SCHEDULE_IDS.")
        summary = irm_schedule.sync()
        self.stdout.write(json.dumps(summary))
        if not summary["ok"]:
            raise CommandError("IRM schedule sync failed; the last copy is unchanged.")
