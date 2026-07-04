from django.contrib import admin

from .models import Annotation, Incident, OnCallShift, TimelineEvent, Transition


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


@admin.register(TimelineEvent)
class TimelineEventAdmin(admin.ModelAdmin):
    list_display = ["incident", "type", "actor", "occurred_at"]
    list_filter = ["type"]
    search_fields = ["incident__id", "actor", "body"]


@admin.register(Annotation)
class AnnotationAdmin(admin.ModelAdmin):
    list_display = ["content_type", "object_id", "tag", "author", "created_at"]
    list_filter = ["tag", "content_type"]
    search_fields = ["body"]


@admin.register(OnCallShift)
class OnCallShiftAdmin(admin.ModelAdmin):
    list_display = ["tier", "user", "starts_at", "ends_at"]
    list_filter = ["tier", "user"]
