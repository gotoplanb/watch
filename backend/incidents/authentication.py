"""DRF authentication for the per-user ops-ingest API key (ADR-029). Reads `Authorization: Bearer
<key>` (or `X-Watch-Api-Key: <key>`), resolves it to the posting user, and sets `request.user` — so
the ingest is a first-class authenticated request and each post is attributable."""
from rest_framework import authentication, exceptions

from . import apikeys, oauth


class ApiKeyAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def _extract(self, request) -> str:
        auth = request.headers.get("Authorization", "")
        if auth.startswith(self.keyword + " "):
            return auth[len(self.keyword) + 1:].strip()
        return request.headers.get("X-Watch-Api-Key", "").strip()

    def authenticate(self, request):
        key = self._extract(request)
        if not key:
            return None  # no key presented → fall through to permission (IsAuthenticated → 403)
        user = apikeys.user_for_key(key)
        if user is None:
            raise exceptions.AuthenticationFailed("Invalid API key.")
        return (user, key)


def resolve_user_bearer(token: str):
    """The user a presented incident-action credential authenticates, or None (ADR-038/056):
    a derived `wm_` key or an OAuth access token. Prefix-dispatched so a key never hits the token
    table and vice versa. The `wk_` ingest key is deliberately NOT accepted here."""
    if token.startswith(f"{apikeys._MCP_PREFIX}_"):
        return apikeys.user_for_mcp_key(token)
    if token.startswith(oauth.ACCESS_TOKEN_PREFIX):
        return oauth.resolve_access_token(token)
    return None


class UserBearerAuthentication(authentication.BaseAuthentication):
    """`Authorization: Bearer <wm_ key | OAuth token>` on the API (ADR-056) — how `wt` acts as a
    user. Same principal and powers as that user's session; no CSRF, since no cookie is involved."""

    keyword = "Bearer"

    def authenticate(self, request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith(self.keyword + " "):
            return None
        token = auth[len(self.keyword) + 1:].strip()
        if not token.startswith((f"{apikeys._MCP_PREFIX}_", oauth.ACCESS_TOKEN_PREFIX)):
            return None  # not ours (e.g. a wk_ ingest key) — let other classes / 401 decide
        user = resolve_user_bearer(token)
        if user is None:
            raise exceptions.AuthenticationFailed("Invalid or expired bearer credential.")
        return (user, token)

    def authenticate_header(self, request):
        return self.keyword
