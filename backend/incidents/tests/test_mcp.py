"""MCP server tests (ADR-038), hermetic: the derived wm_ key, bearer resolution (key + OAuth
token), the auth middleware (401 + resource-metadata discovery), and the tools — uniform
surface, tier-or-higher enforced server-side, T3-gated mode changes, actions attributed to
the human."""
import hashlib
from base64 import urlsafe_b64encode

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import Group, User

from incidents import apikeys, escalation, mcp_server, oauth
from incidents.models import Incident, OperatingModeWindow, Status, Tier

VERIFIER = "verifier-for-mcp-tests"
CHALLENGE = urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


def _mk_user(name, tier=None):
    user = User.objects.create_user(name, password="pw")
    if tier:
        group, _ = Group.objects.get_or_create(name=tier)
        user.groups.add(group)
    return user


def _mk_incident(tier=Tier.T1, **kwargs):
    return Incident.objects.create(
        source="test", title=f"test {tier}", dedupe_key=f"k-{tier}-{Incident.objects.count()}",
        current_tier=tier, **kwargs
    )


@pytest.fixture
def as_user():
    """Set the middleware contextvar the way the ASGI gate does; yields a setter."""
    tokens = []

    def _set(user):
        tokens.append(mcp_server._principal.set(user.pk))
        return user

    yield _set
    for token in reversed(tokens):
        mcp_server._principal.reset(token)


# --- derived MCP key ---

@pytest.mark.django_db
def test_mcp_key_is_separate_but_rotates_with_the_seed(settings):
    settings.API_KEY_SECRET = "s3cr3t"
    user = _mk_user("t1a", Tier.T1)
    wk, wm = apikeys.api_key_for(user), apikeys.mcp_key_for(user)
    assert wk.startswith("wk_") and wm.startswith("wm_") and wk.split("_")[2] != wm.split("_")[2]
    assert apikeys.user_for_mcp_key(wm) == user
    assert apikeys.user_for_mcp_key(wk) is None      # ingest key never grants MCP
    assert apikeys.user_for_key(wm) is None          # and vice versa
    apikeys.rotate(user)
    assert apikeys.user_for_mcp_key(wm) is None      # one rotate rolls both
    assert apikeys.mcp_key_for(user) != wm


# --- bearer resolution (both credential forms) ---

@pytest.mark.django_db
def test_resolve_bearer_key_and_oauth_token(settings):
    settings.API_KEY_SECRET = "s3cr3t"
    user = _mk_user("t2a", Tier.T2)
    assert mcp_server._resolve_bearer_sync(apikeys.mcp_key_for(user)) == user
    client, _ = oauth.create_client("c", ["https://cb.example/x"])
    code = oauth.issue_authorization_code(
        client=client, user=user, redirect_uri="https://cb.example/x",
        code_challenge=CHALLENGE, code_challenge_method="S256", scope="",
    )
    _, raw_access, _ = oauth.redeem_authorization_code(
        client=client, code=code, redirect_uri="https://cb.example/x", code_verifier=VERIFIER
    )
    assert mcp_server._resolve_bearer_sync(raw_access) == user
    assert mcp_server._resolve_bearer_sync("garbage") is None
    assert mcp_server._resolve_bearer_sync("") is None


# --- ASGI middleware ---

def _call_middleware(path, headers):
    captured = []

    async def inner(scope, receive, send):  # the wrapped app — only reached when authorized
        captured.append("app")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():  # pragma: no cover - the middleware never reads the body
        return {"type": "http.request"}

    gate = mcp_server.AuthMiddleware(inner)
    async_to_sync(gate)({"type": "http", "path": path, "headers": headers}, receive, send)
    return captured, sent


@pytest.mark.django_db
def test_middleware_401_carries_discovery_and_metadata_is_open(settings):
    captured, sent = _call_middleware("/mcp", [])
    assert not captured and sent[0]["status"] == 401
    assert b"oauth-protected-resource" in dict(sent[0]["headers"])[b"www-authenticate"]
    # resource metadata is served unauthenticated on this origin (RFC 9728)
    captured, sent = _call_middleware("/.well-known/oauth-protected-resource", [])
    assert not captured and sent[0]["status"] == 200 and b"authorization_servers" in sent[1]["body"]


@pytest.mark.django_db
def test_middleware_passes_valid_key_through(settings):
    settings.API_KEY_SECRET = "s3cr3t"
    user = _mk_user("t1a", Tier.T1)
    bearer = f"Bearer {apikeys.mcp_key_for(user)}".encode()
    captured, sent = _call_middleware("/mcp", [(b"authorization", bearer)])
    assert captured == ["app"] and sent[0]["status"] == 200


# --- tools: read ---

@pytest.mark.django_db
def test_list_and_get_incident(as_user):
    as_user(_mk_user("t1a", Tier.T1))
    _mk_incident(Tier.T1)
    incident = _mk_incident(Tier.T2)
    rows = mcp_server.list_incidents()
    assert len(rows) == 2 and rows[0]["tier"] == Tier.T2  # newest first
    assert mcp_server.list_incidents(tier="T2")[0]["number"] == incident.number
    detail = mcp_server.get_incident(incident.number.lower())  # case-normalized
    assert detail["number"] == incident.number
    assert "transitions" in detail and "timeline" in detail and "triage_decisions" in detail
    assert mcp_server.get_incident("INC-9999") == {"error": "no incident 'INC-9999'"}


@pytest.mark.django_db
def test_tools_require_principal():
    mcp_server._principal.set(None)
    with pytest.raises(ValueError, match="not authenticated"):
        mcp_server.list_incidents()


# --- tools: act, tier-or-higher enforced server-side ---

@pytest.mark.django_db
def test_t1_user_cannot_act_on_t2_incident_and_error_teaches(as_user):
    as_user(_mk_user("t1a", Tier.T1))
    incident = _mk_incident(Tier.T2)
    result = mcp_server.resolve(incident.number)
    assert "requires T2-or-higher" in result["error"] and "you hold T1" in result["error"]
    incident.refresh_from_db()
    assert incident.status == Status.OPEN


@pytest.mark.django_db
def test_ack_escalate_resolve_attributed_to_the_user(as_user, settings):
    settings.ESCALATION_LOCAL_MODE = True
    user = as_user(_mk_user("t2a", Tier.T2))
    incident = _mk_incident(Tier.T1)
    assert mcp_server.acknowledge(incident.number)["acknowledged"] is True
    result = mcp_server.escalate(incident.number, reason="beyond T1")
    assert result["tier"] == Tier.T2
    result = mcp_server.resolve(incident.number, reason="fixed")
    assert result["status"] == Status.RESOLVED
    actors = set(incident.transitions.values_list("actor", flat=True))
    assert actors == {str(user.pk)}  # attributed exactly like web-UI actions
    assert mcp_server.resolve(incident.number) == {
        "error": f"{incident.number} is already resolved"
    }
    assert mcp_server.escalate("INC-0000") == {"error": "no incident 'INC-0000'"}


@pytest.mark.django_db
def test_user_with_no_tier_role_denied(as_user):
    as_user(_mk_user("norole"))
    incident = _mk_incident(Tier.T1)
    assert "you hold no tier role" in mcp_server.acknowledge(incident.number)["error"]


# --- tools: session check ---

@pytest.mark.django_db
def test_run_session_check_requires_subject_and_runs(as_user, monkeypatch):
    from incidents import trace_store

    class Fake:
        def find_error_spans(self, *a):
            return []

    trace_store.set_provider_for_tests(Fake())
    as_user(_mk_user("t1a", Tier.T1))
    assert mcp_server.run_session_check() == {"error": "provide session_id or user_id"}
    result = mcp_server.run_session_check(session_id="corr-42")
    assert result["verdict"] == "clean" and "incident" not in result
    trace_store.set_provider_for_tests(None)


# --- tools: mode (T3-gated — same tools, different powers) ---

@pytest.mark.django_db
def test_mode_tools_gated_to_t3(as_user):
    as_user(_mk_user("t2a", Tier.T2))
    assert "requires the T3" in mcp_server.open_race_window("release")["error"]
    assert mcp_server.get_operating_mode() == {"mode": "highway"}


@pytest.mark.django_db
def test_t3_opens_and_closes_race_window(as_user):
    as_user(_mk_user("t3a", Tier.T3))
    assert mcp_server.close_race_window()["note"] == "no race window was open"
    result = mcp_server.open_race_window("v0.11 release")
    assert result["mode"] == "race"
    assert mcp_server.get_operating_mode() == {"mode": "race"}
    assert OperatingModeWindow.objects.get().actor == "t3a"
    assert mcp_server.close_race_window()["mode"] == "highway"


# --- transport plumbing ---

def test_bearer_parsing_and_transport_security(settings):
    assert mcp_server._bearer_from_scope({"headers": [(b"authorization", b"Bearer abc")]}) == "abc"
    assert mcp_server._bearer_from_scope({"headers": [(b"authorization", b"Basic abc")]}) == ""
    assert mcp_server._bearer_from_scope({"headers": []}) == ""
    security = mcp_server._transport_security()
    assert "localhost:8011" in security.allowed_hosts[0]


@pytest.mark.django_db
def test_build_mcp_app_wraps_gate():
    assert isinstance(mcp_server.build_mcp_app(), mcp_server.AuthMiddleware)
