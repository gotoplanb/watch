"""
Seed a usable local environment (manual use):
- Tier groups T1 / T2 / T3 (ADR-008)
- One user per tier (t1 / t2 / t3) plus an admin superuser
- An optional demo incident pushed through the real intake path

    python manage.py seed_demo

Passwords are 'watch' (admin/admin). Local only — never run against a real env.
"""
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

from incidents import escalation
from incidents.intake import create_incident_idempotent

TIER_USERS = {"t1": "T1", "t2": "T2", "t3": "T3"}


class Command(BaseCommand):
    help = "Create tier groups, demo users, and a demo incident for local use."

    def add_arguments(self, parser):
        parser.add_argument("--no-incident", action="store_true",
                            help="Skip creating the demo incident.")

    def handle(self, *args, **opts):
        for tier in ("T1", "T2", "T3"):
            Group.objects.get_or_create(name=tier)

        for username, tier in TIER_USERS.items():
            user, created = User.objects.get_or_create(username=username)
            if created:
                user.set_password("watch")
                user.save()
            user.groups.set([Group.objects.get(name=tier)])
            self.stdout.write(f"  user {username} -> {tier}")

        admin, created = User.objects.get_or_create(
            username="admin", defaults={"is_staff": True, "is_superuser": True}
        )
        if created:
            admin.set_password("admin")
            admin.save()
            self.stdout.write("  superuser admin/admin")

        if not opts["no_incident"]:
            incident, was_created = create_incident_idempotent(
                source="sumo",
                title="Demo: disk usage > 90% on web-1",
                payload={"host": "web-1", "metric": "disk", "value": 0.93},
                source_event_id="demo-alert-1",
            )
            if was_created:
                incident.escalation_execution_arn = escalation.start_escalation(incident)
                incident.save(update_fields=["escalation_execution_arn", "updated_at"])
            self.stdout.write(f"  demo incident {incident.id} [{incident.status}/{incident.current_tier}]")

        self.stdout.write(self.style.SUCCESS("seed_demo complete"))
