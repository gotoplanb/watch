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
