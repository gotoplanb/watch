"""The API surface the `wt` CLI drives (ADR-056): user bearer credentials on /api/, records by
human number, list filters, /api/me, and the local cli_keys bootstrap."""
import json
import stat

import pytest
from django.contrib.auth.models import Group, User
from django.core.management import CommandError, call_command
from rest_framework.test import APIClient

from incidents import apikeys, assessments, oauth
from incidents.management.commands.cli_keys import merge_into
from incidents.models import Incident, Tier
from incidents.tests.test_oauth import CHALLENGE, REDIRECT, VERIFIER


def _user(username, *tiers):
    user = User.objects.create(username=username)
    for tier in tiers:
        user.groups.add(Group.objects.get_or_create(name=tier)[0])
    return user


def _incident(tier=Tier.T1):
    n = Incident.objects.count()
    return Incident.objects.create(source="sumo", payload={}, title=f"t{n}",
                                   dedupe_key=f"k-{n}", current_tier=tier)


def _bearer(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


# --- bearer auth ---

@pytest.mark.django_db
def test_wm_key_authenticates_as_its_user():
    user = _user("t2a", "T2")
    body = _bearer(apikeys.mcp_key_for(user)).get("/api/me").json()
    assert body == {"id": user.pk, "username": "t2a", "tiers": ["T2"], "is_staff": False}


@pytest.mark.django_db
def test_forged_wm_key_is_rejected():
    user = _user("t1a", "T1")
    response = _bearer(f"wm_{user.pk}_{'0' * 32}").get("/api/me")
    # 403, not 401: DRF takes the challenge header from the first class (session), which has none.
    assert response.status_code == 403
    assert "Invalid or expired" in response.json()["detail"]


@pytest.mark.django_db
def test_ingest_key_gets_no_api_powers():
    user = _user("t1a", "T1")
    response = _bearer(apikeys.api_key_for(user)).get("/api/incidents/")
    assert response.status_code == 403


@pytest.mark.django_db
def test_other_authorization_schemes_fall_through():
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Basic dTpw")
    assert client.get("/api/me").status_code == 403


@pytest.mark.django_db
def test_oauth_access_token_authenticates():
    user = User.objects.create_user("t3a")
    client, _ = oauth.create_client("wt", [REDIRECT])
    code = oauth.issue_authorization_code(client=client, user=user, redirect_uri=REDIRECT,
                                          code_challenge=CHALLENGE, code_challenge_method="S256",
                                          scope="")
    _, access, _ = oauth.redeem_authorization_code(client=client, code=code, redirect_uri=REDIRECT,
                                                   code_verifier=VERIFIER)
    assert _bearer(access).get("/api/me").json()["username"] == "t3a"


@pytest.mark.django_db
def test_bearer_actions_keep_tier_authz():
    incident = _incident(Tier.T2)
    t1 = _bearer(apikeys.mcp_key_for(_user("t1a", "T1")))
    denied = t1.post(f"/api/incidents/{incident.number}/escalate/", {}, format="json")
    assert denied.status_code == 403
    t2 = _bearer(apikeys.mcp_key_for(_user("t2a", "T2")))
    done = t2.post(f"/api/incidents/{incident.number}/escalate/", {"reason": "cli"}, format="json")
    assert done.status_code == 200
    assert done.json()["current_tier"] == "T3"
    assert done.json()["transitions"][-1]["actor_name"] == "t2a"


# --- lookup + filters ---

@pytest.mark.django_db
def test_incident_by_number_or_uuid():
    incident = _incident()
    client = _bearer(apikeys.mcp_key_for(_user("viewer")))
    by_number = client.get(f"/api/incidents/{incident.number.lower()}/").json()
    by_uuid = client.get(f"/api/incidents/{incident.id}/").json()
    assert by_number["id"] == by_uuid["id"] == str(incident.id)
    assert by_number["number"] == incident.number
    assert client.get("/api/incidents/INC-9999/").status_code == 404


@pytest.mark.django_db
def test_incident_list_filters():
    _incident(Tier.T1)
    t2 = _incident(Tier.T2)
    client = _bearer(apikeys.mcp_key_for(_user("viewer")))
    rows = client.get("/api/incidents/?current_tier=t2&status=open").json()
    assert [r["number"] for r in rows] == [t2.number]
    assert len(client.get("/api/incidents/").json()) == 2


@pytest.mark.django_db
def test_assessment_by_number_and_status_filter():
    assessment, _ = assessments.receive(title="Latency", source="sumo", signal="p99 up")
    client = _bearer(apikeys.mcp_key_for(_user("viewer")))
    assert client.get(f"/api/assessments/{assessment.number}/").json()["id"] == str(assessment.id)
    assert client.get(f"/api/assessments/?status={assessment.status.upper()}").json()[0]["number"] == \
        assessment.number
    assert client.get("/api/assessments/?status=NO-SUCH").json() == []


# --- cli_keys bootstrap ---

@pytest.mark.django_db
def test_cli_keys_writes_private_file_and_keeps_other_servers(tmp_path):
    path = tmp_path / "wt" / "keys.json"
    merge_into(path, "https://elsewhere", {"x": "wm_1_a"})
    user = _user("t1a", "T1")
    call_command("cli_keys", write=str(path), server="http://localhost:8010/")
    data = json.loads(path.read_text())
    assert data["servers"]["https://elsewhere"] == {"x": "wm_1_a"}
    assert data["servers"]["http://localhost:8010"]["t1a"] == apikeys.mcp_key_for(user)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.django_db
def test_cli_keys_refuses_without_secret(tmp_path, settings):
    _user("t1a")
    settings.API_KEY_SECRET = ""
    with pytest.raises(CommandError):
        call_command("cli_keys", write=str(tmp_path / "keys.json"))
