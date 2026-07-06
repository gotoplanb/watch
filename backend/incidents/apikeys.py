"""Per-user ops-ingest API keys (ADR-029) + the per-user rotation seed (ADR-030). Keys are derived,
not stored — like the ntfy paging topic (ADR-013): a key encodes the user id (O(1), attributable)
with an HMAC suffix unguessable without API_KEY_SECRET *and* the user's rotation seed. Rotating the
seed (UserKeyring) rolls the key (and every other per-user derived credential) at once."""
import hashlib
import hmac

from django.conf import settings
from django.contrib.auth.models import User

from .models import UserKeyring

_PREFIX = "wk"


def seed_for(user) -> str:
    """The user's rotation seed (ADR-030), lazily created for pre-existing users."""
    keyring, _ = UserKeyring.objects.get_or_create(user=user)
    return keyring.secret


def rotate(user) -> None:
    """Regenerate the user's seed — rolls their API key + ntfy topic + any future derived link."""
    keyring, _ = UserKeyring.objects.get_or_create(user=user)
    keyring.rotate()


def _digest(user_id, seed: str) -> str:
    return hmac.new(
        settings.API_KEY_SECRET.encode(), f"apikey:{user_id}:{seed}".encode(), hashlib.sha256
    ).hexdigest()


def api_key_for(user) -> str:
    """This user's key: `wk_<id>_<hmac>`. Empty when API_KEY_SECRET isn't configured."""
    if not settings.API_KEY_SECRET:
        return ""
    return f"{_PREFIX}_{user.pk}_{_digest(user.pk, seed_for(user))[:32]}"


def user_for_key(key: str):
    """The active user a presented key authenticates, or None. Splits `wk_<id>_<hmac>`, looks up the
    user (for their current seed), recomputes, and constant-time compares — so a forged suffix or a
    key from before the user rotated never matches."""
    if not settings.API_KEY_SECRET or not key:
        return None
    parts = key.split("_", 2)
    if len(parts) != 3 or parts[0] != _PREFIX or not parts[1].isdigit():
        return None
    user = User.objects.filter(pk=int(parts[1]), is_active=True).first()
    if user is None:
        return None
    if not hmac.compare_digest(parts[2], _digest(user.pk, seed_for(user))[:32]):
        return None
    return user
