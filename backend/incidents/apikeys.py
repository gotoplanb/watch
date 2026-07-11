"""Per-user ops-ingest API keys (ADR-029) + the per-user rotation seed (ADR-030). Keys are derived,
not stored — like the ntfy paging topic (ADR-013): a key encodes the user id (O(1), attributable)
with an HMAC suffix unguessable without API_KEY_SECRET *and* the user's rotation seed. Rotating the
seed (UserKeyring) rolls the key (and every other per-user derived credential) at once."""
import hashlib
import hmac

from django.conf import settings
from django.contrib.auth.models import User

from .models import UserKeyring

_PREFIX = "wk"       # ops-ingest key (ADR-029)
_MCP_PREFIX = "wm"   # MCP bearer key (ADR-038) — separate credential, same rotation seed


def seed_for(user) -> str:
    """The user's rotation seed (ADR-030), lazily created for pre-existing users."""
    keyring, _ = UserKeyring.objects.get_or_create(user=user)
    return keyring.secret


def rotate(user) -> None:
    """Regenerate the user's seed — rolls their API key + ntfy topic + any future derived link."""
    keyring, _ = UserKeyring.objects.get_or_create(user=user)
    keyring.rotate()


def _digest(user_id, seed: str, purpose: str = "apikey") -> str:
    return hmac.new(
        settings.API_KEY_SECRET.encode(), f"{purpose}:{user_id}:{seed}".encode(), hashlib.sha256
    ).hexdigest()


def _key_for(user, prefix: str, purpose: str) -> str:
    if not settings.API_KEY_SECRET:
        return ""
    return f"{prefix}_{user.pk}_{_digest(user.pk, seed_for(user), purpose)[:32]}"


def api_key_for(user) -> str:
    """This user's ops-ingest key: `wk_<id>_<hmac>`. Empty when API_KEY_SECRET isn't configured."""
    return _key_for(user, _PREFIX, "apikey")


def mcp_key_for(user) -> str:
    """This user's MCP bearer key: `wm_<id>_<hmac>` (ADR-038). A SEPARATE derived credential —
    ingest keys pasted into ops tooling never gain incident-action powers — but the same rotation
    seed, so one rotate rolls both (ADR-030)."""
    return _key_for(user, _MCP_PREFIX, "mcp")


def _user_for(key: str, prefix: str, purpose: str):
    """Shared resolver: split `<prefix>_<id>_<hmac>`, look up the user (for their current seed),
    recompute, and constant-time compare — so a forged suffix or a key from before the user
    rotated never matches."""
    if not settings.API_KEY_SECRET or not key:
        return None
    parts = key.split("_", 2)
    if len(parts) != 3 or parts[0] != prefix or not parts[1].isdigit():
        return None
    user = User.objects.filter(pk=int(parts[1]), is_active=True).first()
    if user is None:
        return None
    if not hmac.compare_digest(parts[2], _digest(user.pk, seed_for(user), purpose)[:32]):
        return None
    return user


def user_for_key(key: str):
    """The active user a presented ops-ingest key (`wk_…`) authenticates, or None."""
    return _user_for(key, _PREFIX, "apikey")


def user_for_mcp_key(key: str):
    """The active user a presented MCP key (`wm_…`) authenticates, or None (ADR-038)."""
    return _user_for(key, _MCP_PREFIX, "mcp")
