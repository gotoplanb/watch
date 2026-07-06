"""Per-user ops-ingest API keys (ADR-029). Derived, not stored — like the ntfy paging topic (ADR-013):
a key encodes the user id (O(1), attributable) with an HMAC suffix unguessable without API_KEY_SECRET.
Recomputed to verify and to show in the user's settings; empty secret ⇒ keys are off."""
import hashlib
import hmac

from django.conf import settings
from django.contrib.auth.models import User

_PREFIX = "wk"


def _digest(user_id) -> str:
    return hmac.new(
        settings.API_KEY_SECRET.encode(), f"apikey:{user_id}".encode(), hashlib.sha256
    ).hexdigest()


def api_key_for(user) -> str:
    """This user's key: `wk_<id>_<hmac>`. Empty when API_KEY_SECRET isn't configured."""
    if not settings.API_KEY_SECRET:
        return ""
    return f"{_PREFIX}_{user.pk}_{_digest(user.pk)[:32]}"


def user_for_key(key: str):
    """The active user a presented key authenticates, or None. Splits `wk_<id>_<hmac>`, recomputes the
    HMAC for that id, and constant-time compares — so a forged suffix never matches."""
    if not settings.API_KEY_SECRET or not key:
        return None
    parts = key.split("_", 2)
    if len(parts) != 3 or parts[0] != _PREFIX or not parts[1].isdigit():
        return None
    uid, provided = parts[1], parts[2]
    if not hmac.compare_digest(provided, _digest(uid)[:32]):
        return None
    return User.objects.filter(pk=int(uid), is_active=True).first()
