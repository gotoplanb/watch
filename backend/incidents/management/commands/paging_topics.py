"""
Print the current ntfy paging topics (ADR-013) so you can subscribe on your device.

Topics carry the NTFY_TOPIC_SECRET-derived suffix when it's configured — which you can't get from the
(public) source — so this command is how you discover the exact strings to subscribe to.

    python manage.py paging_topics

Local / ops helper; reads only settings + the user list.
"""
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from incidents import apikeys
from incidents.services import paging_topic


class Command(BaseCommand):
    help = "Print ntfy paging topics (per-user + per-tier fallback) for the current env + secret."

    def handle(self, *args, **opts):
        base = settings.NTFY_BASE_URL.rstrip("/")
        secret = "set" if settings.NTFY_TOPIC_SECRET else "EMPTY — topics are guessable from source"
        self.stdout.write(f"env={settings.PAGING_ENV}  base={base}  NTFY_TOPIC_SECRET={secret}")

        self.stdout.write("\ntier fallback topics (paged when the rota has a gap):")
        for tier in ("T1", "T2", "T3"):
            self.stdout.write(f"  {base}/{paging_topic('tier', tier)}")

        self.stdout.write("\nper-user topics (paged when the user is on-call):")
        for user in User.objects.order_by("id"):
            topic = paging_topic("user", user.id, seed=apikeys.seed_for(user))
            self.stdout.write(f"  {user.username:8} {base}/{topic}")
