"""Per-user session revocation (ADR-008): login indexing, flush (all / keep-current), the settings
'sign out everywhere' view, and the admin force-sign-out action."""
from importlib import import_module

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client

from incidents import session_index
from incidents.models import UserSession

_engine = import_module(settings.SESSION_ENGINE)


def _new_session(user) -> str:
    """A real cache session bound to a user + indexed, as a login would leave it."""
    s = _engine.SessionStore()
    s["_auth_user_id"] = str(user.pk)
    s.create()
    session_index.remember(user, s.session_key)
    return s.session_key


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
def test_login_indexes_the_session(client):
    User.objects.create_user("li", password="pw")
    assert client.login(username="li", password="pw")  # fires user_logged_in
    assert UserSession.objects.filter(user__username="li").exists()


@pytest.mark.django_db
def test_remember_ignores_empty_key_and_str():
    u = User.objects.create_user("e")
    session_index.remember(u, "")  # no session key (e.g. token auth) -> nothing indexed
    assert not UserSession.objects.filter(user=u).exists()
    key = _new_session(u)
    assert key[:8] in str(UserSession.objects.get(user=u))


@pytest.mark.django_db
def test_flush_revokes_all_sessions():
    u = User.objects.create_user("f")
    k1, k2 = _new_session(u), _new_session(u)
    assert _engine.SessionStore().exists(k1) and _engine.SessionStore().exists(k2)
    assert session_index.flush(u) == 2
    assert not _engine.SessionStore().exists(k1) and not _engine.SessionStore().exists(k2)
    assert UserSession.objects.filter(user=u).count() == 0


@pytest.mark.django_db
def test_flush_can_keep_current():
    u = User.objects.create_user("k")
    keep, other = _new_session(u), _new_session(u)
    assert session_index.flush(u, keep=keep) == 1
    assert _engine.SessionStore().exists(keep)            # current kept
    assert not _engine.SessionStore().exists(other)       # other revoked
    assert list(UserSession.objects.filter(user=u).values_list("session_key", flat=True)) == [keep]


@pytest.mark.django_db
def test_sign_out_everywhere_view_keeps_current(client):
    User.objects.create_user("v", password="pw")
    client.login(username="v", password="pw")
    u = User.objects.get(username="v")
    other = _new_session(u)  # a second device
    resp = client.post("/ui/settings/sign-out-everywhere/")
    assert resp.status_code == 302
    assert not _engine.SessionStore().exists(other)       # other device revoked
    assert client.get("/ui/settings/").status_code == 200  # this device still signed in


@pytest.mark.django_db
def test_sign_out_everywhere_requires_login(client):
    assert client.post("/ui/settings/sign-out-everywhere/").status_code == 302  # -> login


@pytest.mark.django_db
def test_admin_force_sign_out_action(rf):
    from django.contrib.admin.sites import site
    from django.contrib.messages.storage.fallback import FallbackStorage

    from incidents.admin import WatchUserAdmin, force_sign_out
    u1, u2 = User.objects.create_user("a1"), User.objects.create_user("a2")
    _new_session(u1); _new_session(u1); _new_session(u2)
    req = rf.post("/admin/")
    req.session = _engine.SessionStore()
    req._messages = FallbackStorage(req)
    force_sign_out(WatchUserAdmin(User, site), req, User.objects.filter(username__in=["a1", "a2"]))
    assert UserSession.objects.filter(user__in=[u1, u2]).count() == 0
