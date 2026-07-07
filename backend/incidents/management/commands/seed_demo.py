"""
Seed a usable local environment (manual use):
- Tier groups T1 / T2 / T3 (ADR-008)
- Two users per tier (t1a/t1b, t2a/t2b, t3a/t3b) plus an admin superuser — two per tier
  so on-call scheduling (rotations/handoffs, ADR-012) can be exercised
- An optional demo incident pushed through the real intake path

    python manage.py seed_demo

Passwords come from settings (SEED_USER_PASSWORD / SEED_ADMIN_PASSWORD, default watch/admin) and are
(re)applied every run, so updating .env + re-seeding rotates them. Local/dev only — never a real env.
"""
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

from incidents import escalation
from incidents.intake import create_incident_idempotent

# Two users per tier so scheduling has someone to rotate between (ADR-012).
TIER_USERS = {
    "t1a": "T1", "t1b": "T1",
    "t2a": "T2", "t2b": "T2",
    "t3a": "T3", "t3b": "T3",
}


class Command(BaseCommand):
    help = "Create tier groups, demo users, and a demo incident for local use."

    def add_arguments(self, parser):
        parser.add_argument("--no-incident", action="store_true",
                            help="Skip creating the demo incident.")

    def handle(self, *args, **opts):
        for tier in ("T1", "T2", "T3"):
            Group.objects.get_or_create(name=tier)

        for username, tier in TIER_USERS.items():
            user, _ = User.objects.get_or_create(username=username)
            user.set_password(settings.SEED_USER_PASSWORD)  # (re)apply so .env changes rotate creds
            user.save()
            user.groups.set([Group.objects.get(name=tier)])
            self.stdout.write(f"  user {username} -> {tier}")

        admin, _ = User.objects.get_or_create(
            username="admin", defaults={"is_staff": True, "is_superuser": True}
        )
        admin.set_password(settings.SEED_ADMIN_PASSWORD)
        admin.save()
        self.stdout.write("  superuser admin (password from SEED_ADMIN_PASSWORD)")

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
