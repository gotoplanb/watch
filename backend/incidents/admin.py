from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from . import session_index

from .models import (
    Annotation,
    ErrorSpan,
    Incident,
    OAuthClient,
    OAuthToken,
    OnCallShift,
    OperatingModeWindow,
    SessionCheck,
    TimelineEvent,
    Transition,
    TriageDecision,
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
    list_display = ["record", "type", "actor", "occurred_at"]  # 'record' is the GFK target (ADR-031)
    list_filter = ["type", "content_type"]
    search_fields = ["object_id", "actor", "body"]


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


@admin.register(OAuthClient)
class OAuthClientAdmin(admin.ModelAdmin):
    """Deactivating a client is the kill switch for every token it issued (ADR-038)."""
    list_display = ["name", "client_id", "is_active", "created_at"]
    list_filter = ["is_active"]
    readonly_fields = ["client_id", "client_secret_hash"]


@admin.register(OAuthToken)
class OAuthTokenAdmin(admin.ModelAdmin):
    list_display = ["user", "client", "scope", "revoked", "access_expires_at", "created_at"]
    list_filter = ["revoked", "client"]
    readonly_fields = [f.name for f in OAuthToken._meta.fields]


@admin.register(OperatingModeWindow)
class OperatingModeWindowAdmin(admin.ModelAdmin):
    """The v1 admin-only race-mode toggle (ADR-035): open/close windows here until the
    working surface grows a control."""
    list_display = ["mode", "actor", "reason", "started_at", "ended_at"]
    list_filter = ["mode"]


@admin.register(TriageDecision)
class TriageDecisionAdmin(admin.ModelAdmin):
    """Append-only (ADR-036) — the escalation-correctness audit trail; read-only in admin."""
    list_display = [
        "incident", "verdict", "responsibility", "fault_domain", "disposition", "mode",
        "actor", "provider", "created_at",
    ]
    list_filter = ["verdict", "responsibility", "fault_domain", "disposition", "provider"]
    readonly_fields = [f.name for f in TriageDecision._meta.fields]

    def has_delete_permission(self, request, obj=None):
        return False  # append-only audit (ADR-036)


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


@admin.action(description="Force sign-out (revoke all sessions)")
def force_sign_out(modeladmin, request, queryset):
    """Superuser force-logout (ADR-008): revoke every session of the selected users."""
    total = sum(session_index.flush(user) for user in queryset)
    modeladmin.message_user(request, f"Signed out {total} session(s) across {queryset.count()} user(s).")


admin.site.unregister(User)


@admin.register(User)
class WatchUserAdmin(UserAdmin):
    actions = [force_sign_out]
