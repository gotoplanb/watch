"""Per-environment ops status + digests (ADR-028): secret-gated schema-less ingest, session-auth
reads, open env set, history, and the `special` (speci) digest flag. Hermetic via APIClient."""
import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from incidents.models import Digest, EnvStatus

SECRET = "test-ops-secret"  # config.settings_test OPS_INGEST_SECRET
STATUS_URL = "/api/environments/prod/status"
DIGEST_URL = "/api/environments/prod/digest"


@pytest.fixture
def client():
    return APIClient()


def _post(client, url, body, secret=SECRET):
    return client.post(url, body, format="json", HTTP_X_WATCH_OPS_SECRET=secret)


# --- status ingest: schema-less, verbatim ---------------------------------------------------------
@pytest.mark.django_db
def test_status_ingest_stores_arbitrary_json_verbatim(client):
    body = {"worst_state": "squall", "regions": {"us": ["a", "b"]}, "note": "anything"}
    res = _post(client, STATUS_URL, body)
    assert res.status_code == 201 and res.json()["environment"] == "prod"
    row = EnvStatus.objects.get()
    assert row.payload == body  # stored exactly, no schema coercion


@pytest.mark.django_db
def test_status_ingest_accepts_wholly_different_shapes(client):
    # "No rigid schema" — a services array and a totally different grouping both just store.
    _post(client, STATUS_URL, {"services": [{"id": "gib", "state": "calm"}]})
    _post(client, STATUS_URL, {"teams": {"payments": {"health": "green"}}, "score": 97})
    assert EnvStatus.objects.filter(environment="prod").count() == 2


@pytest.mark.django_db
def test_status_ingest_requires_secret(client):
    assert _post(client, STATUS_URL, {"x": 1}, secret="wrong").status_code == 401
    assert client.post(STATUS_URL, {"x": 1}, format="json").status_code == 401
    assert EnvStatus.objects.count() == 0


@pytest.mark.django_db
def test_status_ingest_rejects_empty_and_bad_env(client):
    assert _post(client, STATUS_URL, {}).status_code == 400          # empty object
    assert _post(client, STATUS_URL, "just a string").status_code == 400
    assert _post(client, "/api/environments/Prod_X/status", {"x": 1}).status_code == 400  # bad label


@pytest.mark.django_db
def test_environment_is_an_open_set(client):
    # A brand-new env label just works — no enum, no migration.
    assert _post(client, "/api/environments/sandbox-eu/status", {"ok": True}).status_code == 201
    assert EnvStatus.objects.filter(environment="sandbox-eu").exists()


# --- digest ingest: content + special ("speci") ---------------------------------------------------
@pytest.mark.django_db
def test_digest_ingest_with_special_flag(client):
    res = _post(client, DIGEST_URL, {"content": "## System Health", "title": "T2 spike", "special": True})
    assert res.status_code == 201 and res.json()["special"] is True
    d = Digest.objects.get()
    assert d.special is True and d.title == "T2 spike" and d.content.startswith("## ")


@pytest.mark.django_db
def test_digest_defaults_to_routine(client):
    _post(client, DIGEST_URL, {"content": "hourly note"})
    assert Digest.objects.get().special is False


@pytest.mark.django_db
def test_digest_requires_content(client):
    assert _post(client, DIGEST_URL, {"title": "no body"}).status_code == 400


@pytest.mark.django_db
def test_digest_ingest_guarded_by_secret_and_env(client):
    assert _post(client, DIGEST_URL, {"content": "x"}, secret="wrong").status_code == 401
    assert _post(client, "/api/environments/BAD_X/digest", {"content": "x"}).status_code == 400


@pytest.mark.django_db
def test_model_str(client):
    _post(client, STATUS_URL, {"n": 1})
    _post(client, DIGEST_URL, {"content": "x", "special": True})
    assert "status prod" in str(EnvStatus.objects.get())
    assert "digest prod [special]" in str(Digest.objects.get())


# --- reads: session-auth, latest + history + special filter ---------------------------------------
@pytest.mark.django_db
def test_status_read_requires_login_and_returns_latest(client):
    _post(client, STATUS_URL, {"n": 1})
    _post(client, STATUS_URL, {"n": 2})  # newest
    assert client.get("/api/environments/prod/status/latest").status_code in (401, 403)  # anon blocked
    client.force_login(User.objects.create(username="viewer"))
    res = client.get("/api/environments/prod/status/latest")
    assert res.status_code == 200 and res.json()["payload"] == {"n": 2}  # history kept, latest = current


@pytest.mark.django_db
def test_status_read_404_when_absent(client):
    client.force_login(User.objects.create(username="v2"))
    assert client.get("/api/environments/prod/status/latest").status_code == 404


@pytest.mark.django_db
def test_digests_list_and_special_filter(client):
    _post(client, DIGEST_URL, {"content": "routine"})
    _post(client, DIGEST_URL, {"content": "incident!", "special": True})
    client.force_login(User.objects.create(username="v3"))
    assert len(client.get("/api/environments/prod/digests").json()) == 2
    only_special = client.get("/api/environments/prod/digests?special=true").json()
    assert len(only_special) == 1 and only_special[0]["special"] is True
    only_routine = client.get("/api/environments/prod/digests?special=false").json()
    assert len(only_routine) == 1 and only_routine[0]["special"] is False
