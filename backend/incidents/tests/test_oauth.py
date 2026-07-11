"""OAuth AS tests (ADR-038): the security core (PKCE, single-use codes, rotating refresh,
token→user resolution with the client kill switch) and the Django HTTP layer (discovery,
authorize via session login + consent, token grants)."""
import hashlib
from base64 import urlsafe_b64encode
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from incidents import oauth
from incidents.models import OAuthAuthorizationCode, OAuthToken

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "a-test-verifier-string-of-decent-length"
CHALLENGE = urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture
def user(db):
    return User.objects.create_user("t2a", password="pw")


@pytest.fixture
def client_pair(db):
    return oauth.create_client("claude.ai", [REDIRECT])


def _issue(client, user, scope=""):
    return oauth.issue_authorization_code(
        client=client, user=user, redirect_uri=REDIRECT,
        code_challenge=CHALLENGE, code_challenge_method="S256", scope=scope,
    )


# --- core: pkce / client auth ---

def test_verify_pkce_s256_only():
    assert oauth.verify_pkce(VERIFIER, CHALLENGE, "S256")
    assert not oauth.verify_pkce(VERIFIER, CHALLENGE, "plain")
    assert not oauth.verify_pkce("wrong", CHALLENGE, "S256")
    assert not oauth.verify_pkce("", CHALLENGE, "S256")


@pytest.mark.django_db
def test_client_lifecycle_and_auth(client_pair):
    client, raw_secret = client_pair
    assert client.client_id.startswith("wtc_") and raw_secret.startswith("wts_")
    assert oauth.authenticate_client(client.client_id, raw_secret).pk == client.pk
    with pytest.raises(oauth.OAuthError, match="bad client secret"):
        oauth.authenticate_client(client.client_id, "wts_wrong")
    with pytest.raises(oauth.OAuthError, match="unknown or inactive"):
        oauth.authenticate_client("wtc_nope", raw_secret)
    client.is_active = False
    client.save()
    with pytest.raises(oauth.OAuthError, match="unknown or inactive"):
        oauth.authenticate_client(client.client_id, raw_secret)


# --- core: code redemption ---

@pytest.mark.django_db
def test_code_redeems_once_binding_the_user(client_pair, user):
    client, _ = client_pair
    code = _issue(client, user)
    tok, raw_access, raw_refresh = oauth.redeem_authorization_code(
        client=client, code=code, redirect_uri=REDIRECT, code_verifier=VERIFIER
    )
    assert tok.user == user and tok.scope == "mcp"
    assert raw_access.startswith("wt_at_") and raw_refresh.startswith("wt_rt_")
    assert oauth.resolve_access_token(raw_access) == user
    with pytest.raises(oauth.OAuthError, match="already used"):
        oauth.redeem_authorization_code(
            client=client, code=code, redirect_uri=REDIRECT, code_verifier=VERIFIER
        )


@pytest.mark.django_db
@pytest.mark.parametrize("tweak,message", [
    (dict(code="not-a-code"), "invalid or already used"),
    (dict(redirect_uri="https://evil.example/cb"), "redirect_uri mismatch"),
    (dict(code_verifier="wrong-verifier"), "PKCE verification failed"),
])
def test_code_redemption_rejects(client_pair, user, tweak, message):
    client, _ = client_pair
    kwargs = dict(client=client, code=_issue(client, user),
                  redirect_uri=REDIRECT, code_verifier=VERIFIER)
    kwargs.update(tweak)
    with pytest.raises(oauth.OAuthError, match=message):
        oauth.redeem_authorization_code(**kwargs)


@pytest.mark.django_db
def test_expired_code_and_wrong_client_rejected(client_pair, user):
    client, _ = client_pair
    code = _issue(client, user)
    OAuthAuthorizationCode.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    with pytest.raises(oauth.OAuthError, match="expired"):
        oauth.redeem_authorization_code(
            client=client, code=code, redirect_uri=REDIRECT, code_verifier=VERIFIER
        )
    other, _ = oauth.create_client("other", [REDIRECT])
    code2 = _issue(client, user)
    OAuthAuthorizationCode.objects.filter(used=False).update(expires_at=timezone.now() + timedelta(minutes=5))
    with pytest.raises(oauth.OAuthError, match="different client"):
        oauth.redeem_authorization_code(
            client=other, code=code2, redirect_uri=REDIRECT, code_verifier=VERIFIER
        )


# --- core: refresh + resolution ---

@pytest.mark.django_db
def test_refresh_rotates_and_revokes(client_pair, user):
    client, _ = client_pair
    _, old_access, old_refresh = oauth.redeem_authorization_code(
        client=client, code=_issue(client, user), redirect_uri=REDIRECT, code_verifier=VERIFIER
    )
    _, new_access, new_refresh = oauth.refresh_token_grant(client=client, refresh_token=old_refresh)
    assert new_access != old_access
    assert oauth.resolve_access_token(new_access) == user
    with pytest.raises(oauth.OAuthError, match="invalid or revoked"):
        oauth.refresh_token_grant(client=client, refresh_token=old_refresh)  # rotated away


@pytest.mark.django_db
def test_resolve_access_token_kill_switches(client_pair, user):
    client, _ = client_pair
    _, raw_access, _ = oauth.redeem_authorization_code(
        client=client, code=_issue(client, user), redirect_uri=REDIRECT, code_verifier=VERIFIER
    )
    assert oauth.resolve_access_token("") is None
    assert oauth.resolve_access_token("wt_at_forged") is None
    # expiry
    OAuthToken.objects.update(access_expires_at=timezone.now() - timedelta(minutes=1))
    assert oauth.resolve_access_token(raw_access) is None
    OAuthToken.objects.update(access_expires_at=timezone.now() + timedelta(hours=1))
    # client deactivation kills all its tokens
    client.is_active = False
    client.save()
    assert oauth.resolve_access_token(raw_access) is None
    client.is_active = True
    client.save()
    # inactive user
    user.is_active = False
    user.save()
    assert oauth.resolve_access_token(raw_access) is None


# --- HTTP layer ---

@pytest.fixture
def http():
    return Client()


def _authorize_params(client, **overrides):
    params = {
        "response_type": "code", "client_id": client.client_id, "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "scope": "mcp", "state": "xyz",
    }
    params.update(overrides)
    return params


@pytest.mark.django_db
def test_discovery_endpoints(http, settings):
    meta = http.get("/.well-known/oauth-authorization-server").json()
    assert meta["authorization_endpoint"].endswith("/oauth/authorize")
    assert meta["code_challenge_methods_supported"] == ["S256"]
    resource = http.get("/.well-known/oauth-protected-resource").json()
    assert resource["resource"] == f"{settings.MCP_PUBLIC_BASE_URL}/mcp"


@pytest.mark.django_db
def test_authorize_bad_client_never_redirects(http):
    resp = http.get("/oauth/authorize", {"client_id": "wtc_nope", "redirect_uri": REDIRECT})
    assert resp.status_code == 400 and b"Unknown client_id" in resp.content


@pytest.mark.django_db
def test_authorize_protocol_errors_redirect_back(http, client_pair):
    client, _ = client_pair
    resp = http.get("/oauth/authorize", _authorize_params(client, response_type="token"))
    assert resp.status_code == 302 and "unsupported_response_type" in resp["Location"]
    resp = http.get("/oauth/authorize", _authorize_params(client, code_challenge=""))
    assert resp.status_code == 302 and "invalid_request" in resp["Location"]


@pytest.mark.django_db
def test_authorize_full_flow_via_session_login(http, client_pair, user, settings):
    client, raw_secret = client_pair
    params = _authorize_params(client)
    # anonymous → bounced to session login with next back to authorize
    resp = http.get("/oauth/authorize", params)
    assert resp.status_code == 302 and resp["Location"].startswith(settings.LOGIN_URL)
    # signed in → consent screen; its CSP form-action admits the validated callback origin
    # (browsers apply form-action to the post-approve redirect — the code delivery)
    http.force_login(user)
    resp = http.get("/oauth/authorize", params)
    assert resp.status_code == 200 and b"Authorize claude.ai" in resp.content
    assert "form-action 'self' https://claude.ai" in resp["Content-Security-Policy"]
    # deny → access_denied redirect
    resp = http.post("/oauth/authorize", {**params, "decision": "deny"})
    assert "access_denied" in resp["Location"]
    # approve → code lands at redirect_uri with state
    resp = http.post("/oauth/authorize", {**params, "decision": "approve"})
    assert resp.status_code == 302 and "code=" in resp["Location"] and "state=xyz" in resp["Location"]
    code = resp["Location"].split("code=")[1].split("&")[0]
    # token exchange (client_secret_post) → acts as the approving user
    resp = http.post("/oauth/token", {
        "grant_type": "authorization_code", "client_id": client.client_id,
        "client_secret": raw_secret, "code": code, "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
    })
    body = resp.json()
    assert resp.status_code == 200 and body["token_type"] == "Bearer"
    assert oauth.resolve_access_token(body["access_token"]) == user
    # refresh grant over HTTP
    resp = http.post("/oauth/token", {
        "grant_type": "refresh_token", "client_id": client.client_id,
        "client_secret": raw_secret, "refresh_token": body["refresh_token"],
    })
    assert resp.status_code == 200 and resp.json()["access_token"] != body["access_token"]


@pytest.mark.django_db
def test_token_endpoint_errors(http, client_pair):
    client, raw_secret = client_pair
    resp = http.post("/oauth/token", {"grant_type": "authorization_code",
                                      "client_id": client.client_id, "client_secret": "wts_bad"})
    assert resp.status_code == 401 and resp.json()["error"] == "invalid_client"
    resp = http.post("/oauth/token", {"grant_type": "password",
                                      "client_id": client.client_id, "client_secret": raw_secret})
    assert resp.status_code == 400 and resp.json()["error"] == "unsupported_grant_type"


@pytest.mark.django_db
def test_token_basic_auth_and_bad_header(http, client_pair, user):
    import base64
    client, raw_secret = client_pair
    code = _issue(client, user)
    creds = base64.b64encode(f"{client.client_id}:{raw_secret}".encode()).decode()
    resp = http.post("/oauth/token", {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT, "code_verifier": VERIFIER,
    }, headers={"Authorization": f"Basic {creds}"})
    assert resp.status_code == 200
    resp = http.post("/oauth/token", {"grant_type": "authorization_code"},
                     headers={"Authorization": "Basic %%%not-base64%%%"})
    assert resp.status_code == 401
