"""ADR-031: assign INC- numbers to pre-existing incidents (oldest first) and seed the INC counter so
new incidents continue from there. No-op on a fresh/test DB (nothing to backfill)."""
from django.db import migrations


def backfill_incident_numbers(apps, schema_editor):
    Incident = apps.get_model("incidents", "Incident")
    RecordCounter = apps.get_model("incidents", "RecordCounter")
    n = 0
    for inc in Incident.objects.filter(number__isnull=True).order_by("created_at", "id"):
        n += 1
        inc.number = f"INC-{n:04d}"
        inc.save(update_fields=["number"])
    if n:
        RecordCounter.objects.update_or_create(prefix="INC", defaults={"value": n})


class Migration(migrations.Migration):
    dependencies = [("incidents", "0014_recordcounter_incident_number_problem")]
    operations = [migrations.RunPython(backfill_incident_numbers, migrations.RunPython.noop)]
