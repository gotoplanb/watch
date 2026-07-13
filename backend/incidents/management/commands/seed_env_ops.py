"""
Seed the environments screen (ADR-043) with a realistic ops feed — hourly status snapshots that
degrade and recover, plus the digests that narrate them.

Shaped like the hermit-watch SRE agent's POSTs (`worst_state` + `triage` + `services[]`), because
that is the shape the screen is built to make legible. Nothing here changes the ingest contract:
ADR-028 stores whatever JSON arrives, and this posts through the same door.

    python manage.py seed_env_ops            # ~24h of history for prod + nonprod
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from incidents.models import Digest, EnvStatus

SERVICES = ["checkout", "payments", "search", "platform"]
# The arc a real incident traces: quiet, a wobble, a proper mess, then recovery. Index = hours ago.
ARC = ["serene", "serene", "calm", "calm", "unsettled", "squall", "storm", "squall", "unsettled",
       "calm", "calm", "serene"]
# Error counts per state — plausible, and deterministic on purpose: the demo must look identical on
# every rebuild (and a seeded PRNG here is a security hotspot Sonar is right to ask about).
ERRS = {"serene": 3, "calm": 14, "unsettled": 62, "squall": 143, "storm": 344}
QUIET = ["serene", "calm", "serene", "serene"]  # what the bystander services are doing, per index


def _services(worst, hour):
    """Payments carries the day's weather; the rest stay boring. Real status pages are mostly boring,
    and a seed that makes everything red teaches the eye nothing."""
    rows = []
    for i, name in enumerate(SERVICES):
        state = worst if name == "payments" else QUIET[(i + hour) % len(QUIET)]
        reqs = 500 + (i * 470 + hour * 137) % 1700  # a plausible spread, no PRNG
        errs = ERRS[state]
        pct = 100 * (reqs - errs) / reqs
        rows.append({
            "id": name, "display_name": name.title(), "state": state,
            "message": f"{name}: {reqs} reqs, {errs} errs ({pct:.1f}% success)",
        })
    return rows


TRIAGE = {
    "storm": "Checkout is failing for most users — payments SDK timeouts, error rate above 15%.",
    "squall": "Payments error rate climbing; checkout latency doubled in the last 10 minutes.",
    "unsettled": "Elevated payment errors — within SLO, but the trend is the wrong direction.",
}

DIGESTS = [
    (2, False, "System Health — 06:00 UTC",
     "Recovered. Payments error rate is back to baseline (0.4%) after the SDK was pinned to 4.2.1 "
     "and redeployed at 05:12 UTC.\n\nCheckout success is 99.8% over the last hour. Search and "
     "platform were unaffected throughout.\n\nWatch for a repeat when the vendor ships 4.3.0 — the "
     "regression was in their retry handling, and we have not yet heard back on a fix."),
    (5, True, "SPECIAL — payments degradation",
     "Payments is in storm: 18% of checkout attempts are failing on vendor SDK timeouts, starting "
     "04:35 UTC and worsening.\n\nBlast radius is checkout only; search and platform are clean. "
     "Rollback of the 4.3.0 SDK bump is in progress.\n\nIncident INC-0068 is open at T2."),
    (9, False, "System Health — 00:00 UTC",
     "Quiet hour. All four services nominal, error counts in the single digits.\n\nVolume is "
     "tracking the usual overnight curve. No monitor alerts fired.\n\nNothing to watch."),
]


class Command(BaseCommand):
    help = "Seed ~24h of environment status snapshots + digests (ADR-043 demo data)."

    def add_arguments(self, parser):
        parser.add_argument("--environments", nargs="+", default=["prod", "nonprod"])

    def handle(self, *args, **opts):
        now = timezone.now()

        for env in opts["environments"]:
            EnvStatus.objects.filter(environment=env).delete()
            Digest.objects.filter(environment=env).delete()

            for hours_ago, worst in enumerate(ARC):
                payload = {
                    "worst_state": worst,
                    "type": "manual" if worst == "storm" else "scheduled",
                    "services": _services(worst, hours_ago),
                }
                if worst in TRIAGE:
                    payload["triage"] = TRIAGE[worst]
                row = EnvStatus.objects.create(environment=env, payload=payload)
                # created_at is auto_now_add, so backdate it after the fact to build a real history.
                EnvStatus.objects.filter(pk=row.pk).update(
                    created_at=now - timedelta(hours=hours_ago, minutes=hours_ago * 3 % 10)
                )

            for hours_ago, special, title, content in DIGESTS:
                d = Digest.objects.create(
                    environment=env, title=title, content=content, special=special
                )
                Digest.objects.filter(pk=d.pk).update(created_at=now - timedelta(hours=hours_ago))

            self.stdout.write(f"  {env}: {len(ARC)} snapshots, {len(DIGESTS)} digests")

        self.stdout.write(self.style.SUCCESS("seed_env_ops complete"))
