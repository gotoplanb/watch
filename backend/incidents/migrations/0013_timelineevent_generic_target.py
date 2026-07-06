"""ADR-031: retarget TimelineEvent from a hard `incident` FK to a GenericForeignKey (content_type +
object_id), so the timeline is shared across record types. Backfills the GFK from `incident_id`
BEFORE dropping the FK, so no history is lost. Incident itself is untouched."""
import django.db.models.deletion
from django.db import migrations, models


def incident_fk_to_gfk(apps, schema_editor):
    TimelineEvent = apps.get_model("incidents", "TimelineEvent")
    ContentType = apps.get_model("contenttypes", "ContentType")
    rows = list(TimelineEvent.objects.values_list("pk", "incident_id"))
    if not rows:  # empty (e.g. fresh/test DB) — nothing to backfill, and the ContentType row may not
        return    # exist yet (post_migrate hasn't run)
    ct, _ = ContentType.objects.get_or_create(app_label="incidents", model="incident")
    for pk, incident_id in rows:
        TimelineEvent.objects.filter(pk=pk).update(content_type=ct.id, object_id=str(incident_id))


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("incidents", "0012_usersession"),
    ]

    operations = [
        # 1) add the GFK columns nullable so existing rows survive
        migrations.AddField(
            model_name="timelineevent",
            name="content_type",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE, to="contenttypes.contenttype"
            ),
        ),
        migrations.AddField(
            model_name="timelineevent",
            name="object_id",
            field=models.CharField(default="", max_length=64),
            preserve_default=False,
        ),
        # 2) backfill from the incident FK (before it's dropped)
        migrations.RunPython(incident_fk_to_gfk, migrations.RunPython.noop),
        # 3) tighten content_type to NOT NULL now that every row has one
        migrations.AlterField(
            model_name="timelineevent",
            name="content_type",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="contenttypes.contenttype"
            ),
        ),
        # 4) drop the incident FK + index the GFK
        migrations.RemoveField(model_name="timelineevent", name="incident"),
        migrations.AddIndex(
            model_name="timelineevent",
            index=models.Index(fields=["content_type", "object_id"], name="tlevent_record_idx"),
        ),
    ]
