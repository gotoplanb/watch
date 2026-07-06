"""Per-environment ops status + digests (ADR-028/029): per-user API-key ingest, schema-less verbatim
store, session-auth reads, open env set, history, `special` (speci) flag, and attribution."""
import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from incidents import apikeys
from incidents.models import Digest, EnvStatus

STATUS_URL = "/api/environments/prod/status"
DIGEST_URL = "/api/environments/prod/digest"


@pytest.fixture
def poster(db):
    return User.objects.create(username="tooling")


@pytest.fixture
def client():
    return APIClient()


def _key(user):
    return apikeys.api_key_for(user)


def _post(client, url, body, user):
    return client.post(url, body, format="json", HTTP_AUTHORIZATION=f"Bearer {_key(user)}")


# --- status ingest: schema-less, verbatim, attributed ---------------------------------------------
@pytest.mark.django_db
def test_status_ingest_stores_verbatim_and_attributes(client, poster):
    body = {"worst_state": "squall", "regions": {"us": ["a", "b"]}, "note": "anything"}
    res = _post(client, STATUS_URL, body, poster)
    assert res.status_code == 201 and res.json()["environment"] == "prod"
    row = EnvStatus.objects.get()
    assert row.payload == body and row.posted_by == poster  # verbatim + attributed to the key's user


@pytest.mark.django_db
def test_status_ingest_accepts_wholly_different_shapes(client, poster):
    _post(client, STATUS_URL, {"services": [{"id": "gib", "state": "calm"}]}, poster)
    _post(client, STATUS_URL, {"teams": {"payments": {"health": "green"}}, "score": 97}, poster)
    assert EnvStatus.objects.filter(environment="prod").count() == 2


@pytest.mark.django_db
def test_status_ingest_rejects_empty_and_bad_env(client, poster):
    assert _post(client, STATUS_URL, {}, poster).status_code == 400
    assert _post(client, STATUS_URL, "just a string", poster).status_code == 400
    assert _post(client, "/api/environments/Prod_X/status", {"x": 1}, poster).status_code == 400


@pytest.mark.django_db
def test_environment_is_an_open_set(client, poster):
    assert _post(client, "/api/environments/sandbox-eu/status", {"ok": True}, poster).status_code == 201
    assert EnvStatus.objects.filter(environment="sandbox-eu").exists()


# --- API-key auth (ADR-029) -----------------------------------------------------------------------
@pytest.mark.django_db
def test_ingest_requires_a_valid_key(client, poster):
    assert client.post(STATUS_URL, {"x": 1}, format="json").status_code in (401, 403)      # no key
    r = client.post(STATUS_URL, {"x": 1}, format="json", HTTP_AUTHORIZATION="Bearer wk_1_deadbeef")
    assert r.status_code == 403                                                              # forged
    assert EnvStatus.objects.count() == 0


@pytest.mark.django_db
def test_forged_suffix_for_real_user_is_rejected(client, poster):
    forged = f"wk_{poster.pk}_{'0' * 32}"
    r = client.post(STATUS_URL, {"x": 1}, format="json", HTTP_AUTHORIZATION=f"Bearer {forged}")
    assert r.status_code == 403 and EnvStatus.objects.count() == 0


@pytest.mark.django_db
def test_x_watch_api_key_header_also_works(client, poster):
    r = client.post(STATUS_URL, {"ok": 1}, format="json", HTTP_X_WATCH_API_KEY=_key(poster))
    assert r.status_code == 201


# --- digest ingest: content + special ("speci") ---------------------------------------------------
@pytest.mark.django_db
def test_digest_ingest_with_special_flag(client, poster):
    res = _post(client, DIGEST_URL, {"content": "## System Health", "title": "T2 spike", "special": True}, poster)
    assert res.status_code == 201 and res.json()["special"] is True
    d = Digest.objects.get()
    assert d.special is True and d.title == "T2 spike" and d.posted_by == poster


@pytest.mark.django_db
def test_digest_defaults_to_routine(client, poster):
    _post(client, DIGEST_URL, {"content": "hourly note"}, poster)
    assert Digest.objects.get().special is False


@pytest.mark.django_db
def test_digest_requires_content(client, poster):
    assert _post(client, DIGEST_URL, {"title": "no body"}, poster).status_code == 400


@pytest.mark.django_db
def test_digest_rejects_bad_env(client, poster):
    assert _post(client, "/api/environments/Prod_X/digest", {"content": "x"}, poster).status_code == 400


@pytest.mark.django_db
def test_model_str(client, poster):
    _post(client, STATUS_URL, {"n": 1}, poster)
    _post(client, DIGEST_URL, {"content": "x", "special": True}, poster)
    assert "status prod" in str(EnvStatus.objects.get())
    assert "digest prod [special]" in str(Digest.objects.get())


# --- reads: session-auth, latest + history + special filter ---------------------------------------
@pytest.mark.django_db
def test_status_read_requires_login_and_returns_latest(client, poster):
    _post(client, STATUS_URL, {"n": 1}, poster)
    _post(client, STATUS_URL, {"n": 2}, poster)  # newest
    assert client.get("/api/environments/prod/status/latest").status_code in (401, 403)
    client.force_login(User.objects.create(username="viewer"))
    res = client.get("/api/environments/prod/status/latest")
    assert res.status_code == 200 and res.json()["payload"] == {"n": 2}


@pytest.mark.django_db
def test_status_read_404_when_absent(client):
    client.force_login(User.objects.create(username="v2"))
    assert client.get("/api/environments/prod/status/latest").status_code == 404


@pytest.mark.django_db
def test_digests_list_and_special_filter(client, poster):
    _post(client, DIGEST_URL, {"content": "routine"}, poster)
    _post(client, DIGEST_URL, {"content": "incident!", "special": True}, poster)
    client.force_login(User.objects.create(username="v3"))
    assert len(client.get("/api/environments/prod/digests").json()) == 2
    only_special = client.get("/api/environments/prod/digests?special=true").json()
    assert len(only_special) == 1 and only_special[0]["special"] is True
    only_routine = client.get("/api/environments/prod/digests?special=false").json()
    assert len(only_routine) == 1 and only_routine[0]["special"] is False
