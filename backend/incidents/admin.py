from django.contrib import admin

from .models import (
    Annotation,
    ErrorSpan,
    Incident,
    OnCallShift,
    SessionCheck,
    TimelineEvent,
    Transition,
    WebhookDelivery,
    WebhookSubscription,
)


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


class ErrorSpanInline(admin.TabularInline):
    model = ErrorSpan
    extra = 0
    readonly_fields = [f.name for f in ErrorSpan._meta.fields]
    can_delete = False


@admin.register(SessionCheck)
class SessionCheckAdmin(admin.ModelAdmin):
    list_display = ["id", "subject_kind", "status", "verdict", "source", "created_at"]
    list_filter = ["subject_kind", "status", "source"]
    search_fields = ["subject_hash"]
    inlines = [ErrorSpanInline]


@admin.register(WebhookSubscription)
class WebhookSubscriptionAdmin(admin.ModelAdmin):
    list_display = ["id", "url", "active", "description", "created_at"]
    list_filter = ["active"]
    search_fields = ["url", "description"]


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ["event_type", "subscription", "status", "status_code", "attempts", "created_at"]
    list_filter = ["status", "event_type"]
    readonly_fields = [f.name for f in WebhookDelivery._meta.fields]
