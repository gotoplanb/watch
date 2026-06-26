from django.contrib import admin

from .models import Incident, Transition


class TransitionInline(admin.TabularInline):
    model = Transition
    extra = 0
    readonly_fields = [f.name for f in Transition._meta.fields]
    can_delete = False  # append-only audit (spec §3)


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ["id", "title", "status", "current_tier", "acknowledged_at", "created_at"]
    list_filter = ["status", "current_tier", "source"]
    search_fields = ["id", "title", "dedupe_key"]
    inlines = [TransitionInline]


@admin.register(Transition)
class TransitionAdmin(admin.ModelAdmin):
    list_display = ["incident", "from_tier", "to_tier", "to_status", "actor", "at"]
    list_filter = ["to_status", "to_tier", "actor"]
