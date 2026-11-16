import secrets

from django.db import migrations, models


def issue_tokens(apps, schema_editor):
    """Status pages created before ADR-065 get a webhook token too."""
    StatusPage = apps.get_model("incidents", "StatusPage")
    for page in StatusPage.objects.filter(webhook_token=""):
        page.webhook_token = secrets.token_urlsafe(24)
        page.save(update_fields=["webhook_token"])


class Migration(migrations.Migration):

    dependencies = [
        ("incidents", "0032_maintenance_window"),
    ]

    operations = [
        migrations.AddField(
            model_name="maintenancewindow",
            name="external_id",
            field=models.CharField(blank=True, db_index=True, max_length=128),
        ),
        # Add without uniqueness, give every existing page its own token, then make it unique.
        migrations.AddField(
            model_name="statuspage",
            name="webhook_token",
            field=models.CharField(blank=True, default="", editable=False, max_length=64),
            preserve_default=False,
        ),
        migrations.RunPython(issue_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="statuspage",
            name="webhook_token",
            field=models.CharField(blank=True, editable=False, max_length=64, unique=True),
        ),
    ]
