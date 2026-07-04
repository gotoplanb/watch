"""
Session correlation id + span tagging (ADR-022).

Stamps a NON-SECRET per-session correlation id (`session.id`) and a keyed HMAC of the user id
(`session.user`) onto the active OTel span, so a Session Check can look up traces by either. The
correlation id is safe to display/share (unlike the session auth key) and is surfaced in the /ui/
header for users to self-report. Guarded so it is a no-op when OTel isn't active (hermetic tests).
"""
import hashlib
import hmac
import uuid

from django.conf import settings


def hash_user_id(user_id) -> str:
    """Keyed HMAC of a user/customer id -> the `session.user` span value. Deterministic (same id ->
    same hash, so lookups work) and keyed (not brute-force reversible). Empty when no key/id."""
    key = settings.SESSION_USER_HMAC_KEY
    if not key or not user_id:
        return ""
    return hmac.new(key.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()[:32]


def _current_span():
    try:
        from opentelemetry import trace

        return trace.get_current_span()
    except Exception:  # pragma: no cover - OTel package absent
        return None


class SessionTaggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        correlation_id = ""
        user = getattr(request, "user", None)
        session = getattr(request, "session", None)
        # Mint a correlation id only for real sessions (authenticated, or an already-established
        # session) — don't create sessions for anonymous API/webhook calls.
        if session is not None and (getattr(user, "is_authenticated", False) or session.session_key):
            correlation_id = session.get("correlation_id") or uuid.uuid4().hex
            if session.get("correlation_id") != correlation_id:
                session["correlation_id"] = correlation_id
        self._tag(correlation_id, user)
        return self.get_response(request)

    @staticmethod
    def _tag(correlation_id, user):
        span = _current_span()
        if span is None:  # pragma: no cover - OTel package absent
            return
        if correlation_id:
            span.set_attribute("session.id", correlation_id)
        if getattr(user, "is_authenticated", False):
            hashed = hash_user_id(str(user.pk))
            if hashed:
                span.set_attribute("session.user", hashed)


def session_id(request):
    """Context processor: expose the session correlation id to /ui/ templates (ADR-022)."""
    session = getattr(request, "session", None)
    return {"session_correlation_id": session.get("correlation_id") if session else ""}
