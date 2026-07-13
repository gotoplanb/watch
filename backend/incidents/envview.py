"""
View model for the environments screen (ADR-043) — the render half of ADR-028's schema-less store.

ADR-028 keeps the posted status payload VERBATIM and refuses to reason about its shape. That store
is right, and it stays: this module reasons about the shape at RENDER time only, where being wrong
costs a dull row rather than a corrupted record.

Two shapes, one screen. The **service shape** (`{worst_state, triage, services:[{state, message}]}`,
as produced by the hermit-watch SRE agent) renders as the real thing: a worst-first list of rows,
each a coloured dot, a name, and one line of numbers. **Anything else** falls back to deriving a row
per top-level object, which is worse but never blank — and the raw payload stays one click away.
"""
from datetime import timedelta

from django.utils import timezone

# The hermit weather ladder, worst first, with the synonyms real tooling posts folded in. The ladder
# is what makes the list useful: the worst thing floats to the top, where the eye already is.
_LADDER = [
    ("storm", 0, "bg-red-500", {"storm", "critical", "crit", "red", "down", "error", "fail", "page"}),
    ("squall", 1, "bg-orange-500", {"squall", "major", "severe"}),
    ("unsettled", 2, "bg-amber-400", {"unsettled", "degraded", "warning", "warn", "amber",
                                      "yellow", "elevated", "minor"}),
    ("calm", 3, "bg-sky-500", {"calm", "ok", "up", "pass", "nominal", "blue"}),
    ("serene", 4, "bg-emerald-500", {"serene", "green", "healthy", "good", "operational"}),
]
_UNKNOWN = ("unknown", 9, "bg-slate-500")
_STATE_KEYS = ("state", "status", "severity", "worst_state", "health", "level", "condition")

# No update in an hour means the tooling stopped reporting — which is itself a status, and the one
# failure mode a status page must never hide behind stale green dots.
STALE_AFTER = timedelta(hours=1)


def _tone(value):
    """(canonical name, severity rank, dot class) for any state string a sender might use."""
    key = str(value or "").strip().lower()
    for name, rank, dot, synonyms in _LADDER:
        if key in synonyms:
            return name, rank, dot
    return _UNKNOWN


def _state_of(node) -> str:
    if not isinstance(node, dict):
        return ""
    for key in _STATE_KEYS:
        if isinstance(node.get(key), str):
            return node[key]
    return ""


def _summarize_scalars(node) -> str:
    """One line of numbers for a row we had to derive: the scalar leaves, minus the state we already
    show as a dot. `{"latency_p99_ms": 880, "deploys": 7}` → "latency p99 ms 880 · deploys 7"."""
    parts = []
    for key, value in node.items():
        if key in _STATE_KEYS or isinstance(value, (dict, list)):
            continue
        label = str(key).replace("_", " ").replace("-", " ").strip()
        parts.append(f"{label} {value}")
    return " · ".join(parts)


def _row(name, state, message):
    tone, rank, dot = _tone(state)
    return {"name": name, "state": tone, "rank": rank, "dot": dot, "message": message or ""}


def _service_rows(payload):
    """The service shape: an explicit list of subsystems, each with its own state and message."""
    return [
        _row(
            svc.get("display_name") or svc.get("name") or svc.get("id") or "service",
            _state_of(svc),
            svc.get("message") or svc.get("detail") or "",
        )
        for svc in payload["services"]
        if isinstance(svc, dict)
    ]


def _derived_rows(payload):
    """Fallback: a row per top-level object. Not as good as a sender that names its services — but a
    dull row beats a blank screen, and the raw payload is one disclosure away."""
    return [
        _row(str(key).replace("_", " ").strip(), _state_of(value), _summarize_scalars(value))
        for key, value in payload.items()
        if isinstance(value, dict)
    ]


def summarize(status, now=None, historical=False) -> dict:
    """The environments screen's whole view model, from one EnvStatus row (or None).

    `historical` says we're paging back through the history rather than looking at now. It matters:
    an hour-old snapshot you deliberately navigated to is not "stale" — it's the past, and greying
    it out would be a lie about what the tooling was saying at the time."""
    now = now or timezone.now()
    if status is None:
        return {"present": False, "rows": [], "stale": False, "historical": False,
                "verdict": _UNKNOWN[0], "verdict_dot": _UNKNOWN[2], "triage": "", "scheduled": True}

    payload = status.payload if isinstance(status.payload, dict) else {}
    rows = _service_rows(payload) if isinstance(payload.get("services"), list) else _derived_rows(payload)
    rows.sort(key=lambda r: (r["rank"], r["name"]))  # worst first — the eye starts at the top

    stale = not historical and (now - status.created_at) > STALE_AFTER
    declared = _state_of(payload)
    # A sender's own verdict wins (it is a judgment call — "calm despite one degraded canary" is a
    # thing a human means); otherwise roll up the worst row. Stale outranks both: we don't know.
    if stale:
        verdict, _, dot = _UNKNOWN
    elif declared:
        verdict, _, dot = _tone(declared)
    elif rows:
        verdict, _, dot = _tone(rows[0]["state"])
    else:
        verdict, _, dot = _UNKNOWN

    return {
        "present": True,
        "rows": rows,
        "stale": stale,
        "historical": historical,
        "verdict": verdict,
        "verdict_dot": dot,
        "triage": payload.get("triage") or "",
        "scheduled": str(payload.get("type") or "scheduled").lower() != "manual",
    }
