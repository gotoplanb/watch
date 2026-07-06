"""DRF authentication for the per-user ops-ingest API key (ADR-029). Reads `Authorization: Bearer
<key>` (or `X-Watch-Api-Key: <key>`), resolves it to the posting user, and sets `request.user` — so
the ingest is a first-class authenticated request and each post is attributable."""
from rest_framework import authentication, exceptions

from . import apikeys


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
