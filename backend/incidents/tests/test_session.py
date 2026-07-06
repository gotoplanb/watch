"""Session lifetime (ADR-008 amendment): long, rolling sessions so an active user stays logged in."""
import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client


@pytest.fixture
def client():
    return Client()


def test_session_settings_are_long_and_rolling():
    assert settings.SESSION_SAVE_EVERY_REQUEST is True          # TTL slides forward on each request
    assert settings.SESSION_EXPIRE_AT_BROWSER_CLOSE is False    # persists across browser close
    assert settings.SESSION_COOKIE_AGE >= 7 * 24 * 3600         # at least a week (default 14 days)


@pytest.mark.django_db
def test_every_response_refreshes_the_session_cookie(client):
    User.objects.create_user("sess", password="pw")
    assert client.login(username="sess", password="pw")
    # A plain GET (session unmodified) still re-sets the cookie with the full max-age — rolling expiry.
    resp = client.get("/ui/incidents/")
    cookie = resp.cookies.get("sessionid")
    assert cookie is not None
    assert int(cookie["max-age"]) == settings.SESSION_COOKIE_AGE
