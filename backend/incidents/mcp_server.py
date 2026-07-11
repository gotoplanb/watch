"""
Watch MCP server (ADR-038) — the AI-assistant tool surface, modeled on conduct's.

A FastMCP (streamable HTTP, stateless) app run as a SEPARATE process from the same image
(`manage.py mcp`, the ADR-025/032 same-image-different-command pattern). The ASGI middleware
below resolves the bearer to a Django USER — either a `wm_` MCP key (ADR-029/030-derived,
Claude Code/Desktop) or an OAuth access token (claude.ai connectors, `oauth.py`) — and stashes
the user id in a contextvar every tool reads.

**Uniform tools, server-side authz:** every user sees the same tool list; what a call may DO
is decided per object at call time by the same `permissions`/`services` the web UI uses
(tier-or-higher, ADR-008), with instructive denials. Actions are attributed to the human —
transitions from MCP are indistinguishable from web-UI ones in the audit trail. Mode changes
are T3-gated (ADR-035): same tools, different powers.

Tools are sync functions (FastMCP runs them in a worker thread, where the Django ORM is fine);
only the auth middleware is async.
"""
import contextvars
import functools
from urllib.parse import urlparse

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth.models import User
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import apikeys, checks, escalation, oauth, permissions, services
from .models import Incident, Status, Tier

# Set per-request by the auth middleware; read by the tools.
_principal: contextvars.ContextVar[int | None] = contextvars.ContextVar("mcp_user_id", default=None)


def _transport_security() -> TransportSecuritySettings:
    """The SDK's DNS-rebinding guard defaults to localhost-only, which 421s every request once
    we're behind a public/tunnel host. Allow our public origin plus localhost (conduct's fix)."""
    host = urlparse(settings.MCP_PUBLIC_BASE_URL.rstrip("/")).netloc or "localhost:8011"
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, f"{host}:*", "localhost:*", "127.0.0.1:*", "[::1]:*"],
        allowed_origins=[settings.MCP_PUBLIC_BASE_URL.rstrip("/"),
                         "http://localhost:*", "http://127.0.0.1:*"],
    )


mcp = FastMCP(
    "Watch",
    stateless_http=True,
    streamable_http_path="/mcp",
    json_response=False,
    transport_security=_transport_security(),
)


def _tool(fn):
    """Register a SYNC tool body with FastMCP behind an async wrapper that runs it in a worker
    thread — FastMCP executes sync functions inline on the event loop, where the Django ORM
    refuses to run. functools.wraps carries the real signature/doc through for the tool schema;
    the sync function itself is returned so tests call it directly."""
    @functools.wraps(fn)
    async def wrapper(**kwargs):
        return await sync_to_async(fn, thread_sensitive=True)(**kwargs)

    mcp.tool()(wrapper)
    return fn


def _user() -> User:
    user_id = _principal.get()
    user = User.objects.filter(pk=user_id, is_active=True).first() if user_id else None
    if user is None:
        raise ValueError("not authenticated")
    return user


def _summary(incident: Incident) -> dict:
    return {
        "number": incident.number,
        "title": incident.title,
        "status": incident.status,
        "tier": incident.current_tier,
        "acknowledged": incident.acknowledged_at is not None,
        "assignee": incident.assignee.username if incident.assignee else None,
        "source": incident.source,
        "sla_deadline_at": incident.sla_deadline_at.isoformat() if incident.sla_deadline_at else None,
        "triage": {
            "responsibility": incident.triage_responsibility,
            "fault_domain": incident.triage_fault_domain,
            "verdict": incident.triage_verdict,
        },
        "created_at": incident.created_at.isoformat(),
    }


def _get(number: str) -> Incident | None:
    return Incident.objects.filter(number=number.strip().upper()).first()


def _denied(user, incident) -> dict:
    tier = permissions.user_tier_rank(user)
    held = f"you hold {['T1', 'T2', 'T3'][tier]}" if tier >= 0 else "you hold no tier role"
    return {"error": f"acting on {incident.number} requires {incident.current_tier}-or-higher; "
                     f"{held}. Ask someone at that tier, or escalate from your own tier."}


def _act(number: str, outcome: str, actor_user, reason: str) -> dict:
    """Shared ack-path validation + the engine outcome/local-mode pattern the UI uses."""
    incident = _get(number)
    if incident is None:
        return {"error": f"no incident {number!r}"}
    if not permissions.can_act_on(actor_user, incident):
        return _denied(actor_user, incident)
    if incident.status != Status.OPEN:
        return {"error": f"{incident.number} is already resolved"}
    actor = str(actor_user.pk)
    if outcome == "ack":
        services.acknowledge(incident.id, actor=actor, reason=reason)
    else:
        escalation.send_outcome(incident, outcome, actor=actor)
        if settings.ESCALATION_LOCAL_MODE:
            if outcome == escalation.OUTCOME_RESOLVE:
                services.resolve(incident.id, actor=actor, reason=reason)
            else:
                services.escalate(incident.id, actor=actor, reason=reason)
    incident.refresh_from_db()
    return _summary(incident)


# --- tools: read ---

@_tool
def list_incidents(status: str = "OPEN", tier: str = "", limit: int = 20) -> list[dict]:
    """List incidents, newest first. status: OPEN | RESOLVED | ALL. tier: T1 | T2 | T3 to filter."""
    _user()
    qs = Incident.objects.order_by("-created_at")
    if status.upper() != "ALL":
        qs = qs.filter(status=status.upper())
    if tier:
        qs = qs.filter(current_tier=tier.upper())
    return [_summary(i) for i in qs[: max(1, min(limit, 100))]]


@_tool
def get_incident(number: str) -> dict:
    """Full detail for one incident (e.g. INC-0042): summary, transitions, timeline, triage
    decisions, and linked records."""
    _user()
    incident = _get(number)
    if incident is None:
        return {"error": f"no incident {number!r}"}
    detail = _summary(incident)
    detail["transitions"] = [
        {"from": f"{t.from_status}/{t.from_tier}", "to": f"{t.to_status}/{t.to_tier}",
         "actor": t.actor, "reason": t.reason, "at": t.at.isoformat()}
        for t in incident.transitions.all()
    ]
    detail["timeline"] = [
        {"type": e.type, "actor": e.actor, "body": e.body, "at": e.occurred_at.isoformat()}
        for e in incident.events.all()[:50]
    ]
    detail["triage_decisions"] = [
        {"verdict": d.verdict, "responsibility": d.responsibility, "fault_domain": d.fault_domain,
         "disposition": d.disposition, "mode": d.mode, "confidence": d.confidence,
         "provider": d.provider, "rationale": d.rationale, "actor": d.actor,
         "at": d.created_at.isoformat()}
        for d in incident.triage_decisions.all()
    ]
    detail["links"] = [
        {"kind": row["kind_label"], "direction": row["direction"], "other": row["other_label"]}
        for row in services.links_for(incident)
    ]
    return detail


@_tool
def get_operating_mode() -> dict:
    """The current operating mode (ADR-035): highway (default) or race (release window)."""
    from . import modes
    _user()
    return {"mode": str(modes.current_mode())}


# --- tools: act (tier-or-higher enforced server-side, ADR-008) ---

@_tool
def acknowledge(number: str, reason: str = "") -> dict:
    """Acknowledge an incident at its current tier. Does NOT stop the SLA clock (ADR-007)."""
    return _act(number, "ack", _user(), reason)


@_tool
def escalate(number: str, reason: str = "") -> dict:
    """Escalate an incident one tier (requires the incident's current tier or higher)."""
    return _act(number, escalation.OUTCOME_ESCALATE, _user(), reason)


@_tool
def resolve(number: str, reason: str = "") -> dict:
    """Resolve an incident (requires the incident's current tier or higher)."""
    return _act(number, escalation.OUTCOME_RESOLVE, _user(), reason)


@_tool
def run_session_check(session_id: str = "", user_id: str = "") -> dict:
    """Run a Session Check (ADR-022): look for error spans for a session correlation id (or a
    customer/user id) in the trace backend. A failing check may open an incident via the T0
    bridge (ADR-036)."""
    _user()
    if not session_id and not user_id:
        return {"error": "provide session_id or user_id"}
    kind = "session" if session_id else "user"
    check = checks.create_and_run(
        subject_kind=kind, subject_raw=session_id or user_id, source="manual"
    )
    result = {"check_id": str(check.id), "status": check.status, "verdict": check.verdict}
    bridged = Incident.objects.filter(
        dedupe_key=f"check:{check.subject_kind}:{check.subject_hash}", status=Status.OPEN
    ).first()
    if bridged:
        result["incident"] = _summary(bridged)
    return result


# --- tools: operating mode (T3-gated — same tools, different powers) ---

def _require_t3(user) -> dict | None:
    if not user.groups.filter(name=Tier.T3).exists():
        return {"error": "changing the operating mode requires the T3 (ops manager) role"}
    return None


@_tool
def open_race_window(reason: str = "") -> dict:
    """Declare race mode (ADR-035) — release-window posture; internal faults auto-escalate
    (ADR-037). T3 only."""
    from . import modes
    user = _user()
    denied = _require_t3(user)
    if denied:
        return denied
    window = modes.open_race_window(user.username, reason=reason)
    return {"mode": "race", "window_id": str(window.id), "started_at": window.started_at.isoformat()}


@_tool
def close_race_window() -> dict:
    """Declare the all-clear: close the open race window (ADR-035). T3 only."""
    from . import modes
    user = _user()
    denied = _require_t3(user)
    if denied:
        return denied
    window = modes.close_race_window(user.username)
    if window is None:
        return {"mode": "highway", "note": "no race window was open"}
    return {"mode": "highway", "window_id": str(window.id), "ended_at": window.ended_at.isoformat()}


# --- auth middleware + app ---

def _bearer_from_scope(scope: dict) -> str:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            header = value.decode("latin-1")
            if header.lower().startswith("bearer "):
                return header[7:].strip()
    return ""


def _resolve_bearer_sync(token: str):
    """Either credential form resolves to a user: a derived `wm_` MCP key, or an OAuth access
    token. Prefix-dispatched so a key never hits the token table and vice versa."""
    if token.startswith(f"{apikeys._MCP_PREFIX}_"):
        return apikeys.user_for_mcp_key(token)
    if token.startswith(oauth.ACCESS_TOKEN_PREFIX):
        return oauth.resolve_access_token(token)
    return None


# thread_sensitive: the auth lookup shares the main sync thread (fast, and keeps sqlite tests
# from cross-thread lock errors); tool bodies run in FastMCP's own worker threads regardless.
_resolve_bearer = sync_to_async(_resolve_bearer_sync, thread_sensitive=True)


async def _send_unauthorized(send) -> None:
    meta = f"{settings.MCP_PUBLIC_BASE_URL.rstrip('/')}/.well-known/oauth-protected-resource"
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"www-authenticate", f'Bearer resource_metadata="{meta}"'.encode("latin-1")),
        ],
    })
    await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})


async def _send_json(send, payload: bytes) -> None:
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": payload})


class AuthMiddleware:
    """Pure-ASGI bearer gate (kept pure so SSE streaming isn't buffered — conduct's pattern).
    Also serves the RFC 9728 protected-resource metadata on this origin, since discovery
    starts at the resource server."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"].rstrip("/") == "/.well-known/oauth-protected-resource":
            import json
            meta = oauth.protected_resource_metadata(
                settings.MCP_PUBLIC_BASE_URL, settings.WATCH_PUBLIC_BASE_URL
            )
            await _send_json(send, json.dumps(meta).encode())
            return

        user = await _resolve_bearer(_bearer_from_scope(scope))
        if user is None:
            await _send_unauthorized(send)
            return
        reset = _principal.set(user.pk)
        try:
            await self.app(scope, receive, send)
        finally:
            _principal.reset(reset)


def build_mcp_app():
    """The ASGI app `manage.py mcp` serves: FastMCP's streamable-HTTP transport (at /mcp)
    behind the bearer gate, plus resource-metadata discovery."""
    return AuthMiddleware(mcp.streamable_http_app())
