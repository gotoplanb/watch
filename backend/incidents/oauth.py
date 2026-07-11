"""
OAuth 2.0 authorization-server core (ADR-038) — ported from conduct's `oauth_provider`.

Hand-rolled, deliberately minimal: authorization-code grant with mandatory PKCE (S256) plus
rotating refresh tokens. No external auth lib — stdlib crypto only. All secrets/codes/tokens
are stored as SHA-256 hashes; raw values exist only in transit. **The watch difference from
conduct: every token binds to the Django USER who approved consent**, not a client app —
MCP tools then act as that human under the normal tier authz (ADR-008), and transitions are
attributed to them exactly like web-UI actions.

The HTTP layer (`oauth_views.py`) handles request parsing, login, consent, and redirects;
this module owns the security-sensitive logic so it can be unit-tested directly.
"""
import hashlib
import hmac
import secrets
from base64 import urlsafe_b64encode
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import OAuthAuthorizationCode, OAuthClient, OAuthToken

CLIENT_ID_PREFIX = "wtc_"
CLIENT_SECRET_PREFIX = "wts_"
ACCESS_TOKEN_PREFIX = "wt_at_"
REFRESH_TOKEN_PREFIX = "wt_rt_"

AUTH_CODE_TTL = timedelta(minutes=5)
ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=30)

DEFAULT_SCOPE = "mcp"


class OAuthError(Exception):
    """OAuth protocol failure. `error` is the RFC 6749 code; the HTTP layer renders the
    standard JSON error body."""

    def __init__(self, error: str, description: str = ""):
        super().__init__(f"{error}: {description}")
        self.error = error
        self.description = description


# --- crypto helpers ---

def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_pkce(verifier: str, challenge: str, method: str) -> bool:
    """Only S256 is supported (plain is disallowed for security)."""
    if method != "S256" or not verifier or not challenge:
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(expected, challenge)


def create_client(name: str, redirect_uris: list[str]) -> tuple[OAuthClient, str]:
    """Mint a client pair (manual registration, conduct-style — no DCR). Returns the client and
    the RAW secret, shown exactly once."""
    raw_secret = f"{CLIENT_SECRET_PREFIX}{secrets.token_urlsafe(32)}"
    client = OAuthClient.objects.create(
        client_id=f"{CLIENT_ID_PREFIX}{secrets.token_urlsafe(16)}",
        client_secret_hash=_hash(raw_secret),
        name=name,
        redirect_uris=redirect_uris,
    )
    return client, raw_secret


# --- client + redirect validation ---

def get_active_client(client_id: str) -> OAuthClient | None:
    return OAuthClient.objects.filter(client_id=client_id, is_active=True).first()


def redirect_uri_allowed(client: OAuthClient, redirect_uri: str) -> bool:
    return redirect_uri in (client.redirect_uris or [])


def authenticate_client(client_id: str, client_secret: str) -> OAuthClient:
    """Confirm a client_id/secret pair. Raises OAuthError(invalid_client)."""
    client = get_active_client(client_id)
    if client is None or not client_secret:
        raise OAuthError("invalid_client", "unknown or inactive client")
    if not hmac.compare_digest(client.client_secret_hash, _hash(client_secret)):
        raise OAuthError("invalid_client", "bad client secret")
    return client


# --- authorization codes ---

def issue_authorization_code(
    *, client: OAuthClient, user, redirect_uri: str, code_challenge: str,
    code_challenge_method: str, scope: str
) -> str:
    """Mint and persist an auth code bound to the approving user; returns the raw code."""
    raw_code = secrets.token_urlsafe(32)
    OAuthAuthorizationCode.objects.create(
        code_hash=_hash(raw_code),
        client=client,
        user=user,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        scope=scope or DEFAULT_SCOPE,
        expires_at=timezone.now() + AUTH_CODE_TTL,
    )
    return raw_code


@transaction.atomic
def redeem_authorization_code(
    *, client: OAuthClient, code: str, redirect_uri: str, code_verifier: str
) -> tuple[OAuthToken, str, str]:
    """Validate a code + PKCE verifier and exchange it for a token pair. Single-use (locked row,
    so a concurrent double-redeem loses). Returns (token, raw_access, raw_refresh)."""
    row = OAuthAuthorizationCode.objects.select_for_update().filter(code_hash=_hash(code)).first()
    if row is None or row.used:
        raise OAuthError("invalid_grant", "code is invalid or already used")
    if row.client_id != client.id:
        raise OAuthError("invalid_grant", "code was issued to a different client")
    if row.expires_at < timezone.now():
        raise OAuthError("invalid_grant", "code has expired")
    if not hmac.compare_digest(row.redirect_uri, redirect_uri):
        raise OAuthError("invalid_grant", "redirect_uri mismatch")
    if not verify_pkce(code_verifier, row.code_challenge, row.code_challenge_method):
        raise OAuthError("invalid_grant", "PKCE verification failed")

    row.used = True
    row.save(update_fields=["used"])
    return _create_token(client, row.user, row.scope)


# --- tokens ---

def _create_token(client: OAuthClient, user, scope: str) -> tuple[OAuthToken, str, str]:
    raw_access = f"{ACCESS_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    raw_refresh = f"{REFRESH_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    now = timezone.now()
    token = OAuthToken.objects.create(
        access_token_hash=_hash(raw_access),
        refresh_token_hash=_hash(raw_refresh),
        client=client,
        user=user,
        scope=scope or DEFAULT_SCOPE,
        access_expires_at=now + ACCESS_TOKEN_TTL,
        refresh_expires_at=now + REFRESH_TOKEN_TTL,
    )
    return token, raw_access, raw_refresh


@transaction.atomic
def refresh_token_grant(*, client: OAuthClient, refresh_token: str) -> tuple[OAuthToken, str, str]:
    """Rotate a refresh token: the old pair is revoked and a new one issued."""
    row = OAuthToken.objects.select_for_update().filter(
        refresh_token_hash=_hash(refresh_token)
    ).first()
    if row is None or row.revoked:
        raise OAuthError("invalid_grant", "refresh token is invalid or revoked")
    if row.client_id != client.id:
        raise OAuthError("invalid_grant", "refresh token belongs to another client")
    if row.refresh_expires_at < timezone.now():
        raise OAuthError("invalid_grant", "refresh token has expired")

    row.revoked = True
    row.save(update_fields=["revoked"])
    return _create_token(client, row.user, row.scope)


def resolve_access_token(raw_access: str):
    """Resource-server entry point: map a bearer access token to its USER, or None if
    missing/expired/revoked — or if the client was deactivated (the kill switch for all its
    tokens) or the user is inactive. Used by the MCP server's auth middleware."""
    if not raw_access:
        return None
    row = (
        OAuthToken.objects.select_related("client", "user")
        .filter(access_token_hash=_hash(raw_access))
        .first()
    )
    if row is None or row.revoked or row.access_expires_at < timezone.now():
        return None
    if not row.client.is_active or not row.user.is_active:
        return None
    return row.user


# --- discovery metadata ---

def authorization_server_metadata(base: str) -> dict:
    base = base.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "scopes_supported": [DEFAULT_SCOPE],
    }


def protected_resource_metadata(mcp_base: str, auth_base: str) -> dict:
    return {
        "resource": f"{mcp_base.rstrip('/')}/mcp",
        "authorization_servers": [auth_base.rstrip("/")],
        "scopes_supported": [DEFAULT_SCOPE],
    }
