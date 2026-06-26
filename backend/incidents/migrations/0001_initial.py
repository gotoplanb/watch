import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

STATUS_CHOICES = [("OPEN", "Open"), ("RESOLVED", "Resolved")]
TIER_CHOICES = [("T1", "Tier 1"), ("T2", "Tier 2"), ("T3", "Tier 3")]


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Incident",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("source", models.CharField(max_length=128)),
                ("payload", models.JSONField(default=dict)),
                ("title", models.CharField(max_length=512)),
                ("dedupe_key", models.CharField(max_length=128)),
                ("status", models.CharField(choices=STATUS_CHOICES, default="OPEN",
                                            max_length=16)),
                ("current_tier", models.CharField(choices=TIER_CHOICES, default="T1",
                                                  max_length=8)),
                ("acknowledged_at", models.DateTimeField(blank=True, null=True)),
                ("sla_deadline_at", models.DateTimeField(blank=True, null=True)),
                ("escalation_execution_arn", models.CharField(blank=True, default="",
                                                              max_length=256)),
                ("current_task_token", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assignee", models.ForeignKey(blank=True, null=True,
                                               on_delete=django.db.models.deletion.SET_NULL,
                                               to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="Transition",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("from_status", models.CharField(choices=STATUS_CHOICES, max_length=16)),
                ("from_tier", models.CharField(choices=TIER_CHOICES, max_length=8)),
                ("to_status", models.CharField(choices=STATUS_CHOICES, max_length=16)),
                ("to_tier", models.CharField(choices=TIER_CHOICES, max_length=8)),
                ("actor", models.CharField(max_length=128)),
                ("reason", models.CharField(blank=True, default="", max_length=512)),
                ("at", models.DateTimeField(auto_now_add=True)),
                ("incident", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                               related_name="transitions",
                                               to="incidents.incident")),
            ],
            options={"ordering": ["at", "id"]},
        ),
        migrations.AddIndex(
            model_name="incident",
            index=models.Index(fields=["status", "current_tier"],
                               name="incidents_status_tier_idx"),
        ),
        migrations.AddConstraint(
            model_name="incident",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "OPEN")),
                fields=("dedupe_key",),
                name="uniq_open_dedupe_key",
            ),
        ),
    ]
