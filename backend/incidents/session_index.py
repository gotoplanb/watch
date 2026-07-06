"""Per-user session revocation (ADR-008). Cache-backed sessions (Valkey) have no user→sessions
lookup, so `UserSession` indexes each login; `flush` deletes a user's sessions from the cache +
their index rows. Used by the self-service 'sign out everywhere' and the admin force-sign-out."""
from importlib import import_module

from django.conf import settings

from .models import UserSession


def remember(user, session_key: str) -> None:
    """Index a user's session at login (idempotent per key)."""
    if session_key:
        UserSession.objects.update_or_create(session_key=session_key, defaults={"user": user})


def flush(user, keep: str = "") -> int:
    """Sign the user out of all sessions (optionally keeping `keep`, e.g. the current one). Deletes the
    session from the cache and its index row. Returns how many sessions were signed out."""
    engine = import_module(settings.SESSION_ENGINE)
    rows = UserSession.objects.filter(user=user)
    signed_out = 0
    for row in rows:
        if row.session_key == keep:
            continue
        engine.SessionStore(row.session_key).delete()
        signed_out += 1
    rows.exclude(session_key=keep).delete()
    return signed_out
