"""Per-user ops API key derivation + resolution (ADR-029): deterministic, round-trips, forgery/edge
cases, and off when the secret is empty."""
import pytest
from django.contrib.auth.models import User

from incidents import apikeys


@pytest.mark.django_db
def test_key_is_deterministic_and_encodes_the_id():
    u = User.objects.create(username="a")
    k = apikeys.api_key_for(u)
    assert k == apikeys.api_key_for(u)                 # stable
    assert k.startswith(f"wk_{u.pk}_") and len(k.split("_")[2]) == 32


@pytest.mark.django_db
def test_round_trip_and_distinct_per_user():
    u1 = User.objects.create(username="u1")
    u2 = User.objects.create(username="u2")
    assert apikeys.user_for_key(apikeys.api_key_for(u1)) == u1
    assert apikeys.api_key_for(u1) != apikeys.api_key_for(u2)


@pytest.mark.django_db
def test_forged_suffix_and_malformed_rejected():
    u = User.objects.create(username="u")
    assert apikeys.user_for_key(f"wk_{u.pk}_{'0' * 32}") is None   # right id, wrong hmac
    assert apikeys.user_for_key("garbage") is None
    assert apikeys.user_for_key("wk_notanint_abc") is None
    assert apikeys.user_for_key("") is None


@pytest.mark.django_db
def test_inactive_user_key_does_not_resolve():
    u = User.objects.create(username="gone", is_active=False)
    assert apikeys.user_for_key(apikeys.api_key_for(u)) is None


@pytest.mark.django_db
def test_off_when_secret_empty(settings):
    settings.API_KEY_SECRET = ""
    u = User.objects.create(username="x")
    assert apikeys.api_key_for(u) == ""
    assert apikeys.user_for_key("wk_1_whatever") is None


# --- per-user rotation seed (ADR-030) -------------------------------------------------------------
@pytest.mark.django_db
def test_keyring_created_at_user_creation():
    from incidents.models import UserKeyring
    u = User.objects.create(username="new")
    kr = UserKeyring.objects.get(user=u)  # post_save signal created it
    assert apikeys.seed_for(u) and f"keyring[{u.pk}]" == str(kr)


@pytest.mark.django_db
def test_rotate_rolls_the_key_and_invalidates_the_old(settings):
    from incidents import services
    settings.NTFY_TOPIC_SECRET = "topicsecret"  # so the topic has an HMAC the seed feeds
    u = User.objects.create(username="rot")
    old_key = apikeys.api_key_for(u)
    old_topic = services.paging_topic("user", u.id, seed=apikeys.seed_for(u))
    assert apikeys.user_for_key(old_key) == u             # old key works before rotate

    apikeys.rotate(u)

    new_key = apikeys.api_key_for(u)
    new_topic = services.paging_topic("user", u.id, seed=apikeys.seed_for(u))
    assert new_key != old_key and new_topic != old_topic  # both links rolled together
    assert apikeys.user_for_key(old_key) is None          # old key no longer authenticates
    assert apikeys.user_for_key(new_key) == u


@pytest.mark.django_db
def test_rotate_leaves_tier_topics_untouched(settings):
    from incidents import services
    u = User.objects.create(username="rot2")
    tier_before = services.paging_topic("tier", "T2")
    apikeys.rotate(u)
    assert services.paging_topic("tier", "T2") == tier_before  # shared topic, no seed
