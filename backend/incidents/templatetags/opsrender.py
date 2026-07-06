"""Template filters for the schema-less ops-status renderer (ADR-028). The status payload is arbitrary
JSON — these classify a node (map / list / scalar) and apply light, optional heuristics (colour a
state/severity value, linkify a URL) so the *posted JSON* drives the groupings, with no fixed schema."""
import json

from django import template

register = template.Library()

_STATE_KEYS = {"state", "status", "severity", "worst_state", "health", "level", "condition"}
# Weather scale (hermit) + common severity words → a coarse colour bucket.
_STATE_COLORS = {
    "serene": "good", "calm": "good", "green": "good", "ok": "good", "healthy": "good",
    "nominal": "good", "up": "good", "pass": "good",
    "unsettled": "warn", "degraded": "warn", "warning": "warn", "warn": "warn",
    "yellow": "warn", "amber": "warn", "elevated": "warn",
    "squall": "crit", "storm": "crit", "critical": "crit", "crit": "crit", "red": "crit",
    "down": "crit", "error": "crit", "fail": "crit", "page": "crit",
}


@register.filter
def classify(value):
    if isinstance(value, dict):
        return "map"
    if isinstance(value, (list, tuple)):
        return "list"
    return "scalar"


@register.filter
def humanize_key(key):
    return str(key).replace("_", " ").replace("-", " ").strip().title()


@register.filter
def is_state_key(label):
    return str(label).strip().lower() in _STATE_KEYS


@register.filter
def state_class(value):
    return _STATE_COLORS.get(str(value).strip().lower(), "muted")


@register.filter
def is_url(value):
    # Linkify a value that looks like a web URL (http/https dashboard links from the tooling). We only
    # detect the scheme to render a link — we never open the connection — so this isn't S5332.
    return isinstance(value, str) and "://" in value and value.split("://", 1)[0].lower() in {"http", "https"}


@register.filter
def scalar_text(value):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value


@register.filter
def as_json(value):
    """Pretty JSON — the fallback render past the depth cap (defensive against pathological nesting)."""
    return json.dumps(value, indent=2, default=str, ensure_ascii=False)
