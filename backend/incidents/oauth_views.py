"""
OAuth 2.0 authorization-server HTTP endpoints (ADR-038) — the Django port of conduct's
`routes/oauth.py`: discovery + authorize (login via the existing session auth, then a consent
screen) + token. Request parsing, redirects, and rendering only; the security logic lives in
`oauth.py`. Approval is the signed-in user consenting FOR THEMSELVES — the issued tokens act
as that user under normal tier authz (ADR-008).

The token endpoint is CSRF-exempt by nature (external OAuth clients POST it); everything it
accepts is authenticated by the client secret + code/PKCE instead.
"""
from base64 import b64decode
from urllib.parse import quote, urlencode, urlparse

from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import oauth

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


# --- discovery (unauthenticated) ---

@require_GET
def authorization_server_discovery(request):
    base = request.build_absolute_uri("/").rstrip("/")
    return JsonResponse(oauth.authorization_server_metadata(base))


@require_GET
def protected_resource_discovery(request):
    """Served here for convenience; the canonical copy lives on the MCP server's own origin
    (RFC 9728 discovery starts at the resource)."""
    base = request.build_absolute_uri("/").rstrip("/")
    return JsonResponse(oauth.protected_resource_metadata(settings.MCP_PUBLIC_BASE_URL, base))


# --- redirect helpers ---

def _append_query(url: str, params: dict) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urlencode({k: v for k, v in params.items() if v})}"


def _redirect_error(redirect_uri: str, error: str, state: str, description: str = ""):
    return HttpResponseRedirect(
        _append_query(redirect_uri, {"error": error, "error_description": description, "state": state})
    )


def _error_page(request, message: str):
    return render(request, "incidents/oauth_error.html", {"message": message}, status=400)


def _validated_client(request, params):
    """Client + redirect_uri + protocol checks shared by GET and POST authorize. Returns
    (client, error_response) — exactly one is set. Never redirects to an unvalidated URI."""
    client = oauth.get_active_client(params.get("client_id", ""))
    redirect_uri = params.get("redirect_uri", "")
    if client is None or not oauth.redirect_uri_allowed(client, redirect_uri):
        return None, _error_page(request, "Unknown client_id or unregistered redirect_uri.")
    return client, None


# --- authorize ---

@require_http_methods(["GET", "POST"])
def authorize(request):
    params = request.POST if request.method == "POST" else request.GET
    client, err = _validated_client(request, params)
    if err:
        return err
    state = params.get("state", "")
    redirect_uri = params.get("redirect_uri", "")
    if params.get("response_type", "code" if request.method == "POST" else "") != "code":
        return _redirect_error(redirect_uri, "unsupported_response_type", state)
    if params.get("code_challenge_method", "S256") != "S256" or not params.get("code_challenge"):
        return _redirect_error(redirect_uri, "invalid_request", state, "PKCE S256 required")

    # Consent requires the human: bounce through the existing session login, then back here.
    if not request.user.is_authenticated:
        next_url = f"{request.path}?{request.GET.urlencode()}"
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?next={quote(next_url, safe='')}")

    if request.method == "GET":
        response = render(request, "incidents/oauth_consent.html", {
            "client_name": client.name,
            "scope": params.get("scope") or oauth.DEFAULT_SCOPE,
            "fields": {
                "response_type": "code",
                "client_id": params.get("client_id", ""),
                "redirect_uri": redirect_uri,
                "code_challenge": params.get("code_challenge", ""),
                "code_challenge_method": params.get("code_challenge_method", "S256"),
                "scope": params.get("scope", ""),
                "state": state,
            },
        })
        # Browsers apply the page's CSP form-action to the whole submit chain, INCLUDING the
        # post-approve 302 to the client's callback — extend it with the (already validated)
        # redirect origin, else Chrome blocks the code delivery. Middleware honors ours.
        parsed = urlparse(redirect_uri)
        response["Content-Security-Policy"] = settings.CONTENT_SECURITY_POLICY.replace(
            "form-action 'self'", f"form-action 'self' {parsed.scheme}://{parsed.netloc}"
        )
        return response

    if params.get("decision") != "approve":
        return _redirect_error(redirect_uri, "access_denied", state)
    code = oauth.issue_authorization_code(
        client=client,
        user=request.user,
        redirect_uri=redirect_uri,
        code_challenge=params.get("code_challenge", ""),
        code_challenge_method=params.get("code_challenge_method", "S256"),
        scope=params.get("scope", ""),
    )
    return HttpResponseRedirect(_append_query(redirect_uri, {"code": code, "state": state}))


# --- token ---

def _client_creds(request) -> tuple[str, str]:
    """Pull client_id/secret from HTTP Basic auth, falling back to the form body."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = b64decode(header[6:]).decode("utf-8")
            cid, _, secret = decoded.partition(":")
            return cid, secret
        except ValueError:  # bad base64 / not UTF-8
            return "", ""
    return request.POST.get("client_id", ""), request.POST.get("client_secret", "")


@csrf_exempt
@require_POST
def token(request):
    grant_type = request.POST.get("grant_type", "")
    client_id, client_secret = _client_creds(request)
    try:
        client = oauth.authenticate_client(client_id, client_secret)
        if grant_type == "authorization_code":
            tok, raw_access, raw_refresh = oauth.redeem_authorization_code(
                client=client,
                code=request.POST.get("code", ""),
                redirect_uri=request.POST.get("redirect_uri", ""),
                code_verifier=request.POST.get("code_verifier", ""),
            )
        elif grant_type == "refresh_token":
            tok, raw_access, raw_refresh = oauth.refresh_token_grant(
                client=client, refresh_token=request.POST.get("refresh_token", "")
            )
        else:
            raise oauth.OAuthError("unsupported_grant_type", f"{grant_type!r} is not supported")
    except oauth.OAuthError as e:
        status = 401 if e.error == "invalid_client" else 400
        return JsonResponse(
            {"error": e.error, "error_description": e.description}, status=status, headers=_NO_STORE
        )

    return JsonResponse({
        "access_token": raw_access,
        "token_type": "Bearer",
        "expires_in": int(oauth.ACCESS_TOKEN_TTL.total_seconds()),
        "refresh_token": raw_refresh,
        "scope": tok.scope,
    }, headers=_NO_STORE)
