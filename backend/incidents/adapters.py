"""Vendor payloads → Watch signals (ADR-065).

Sources that can template Watch's signal shape and send its secret header (Sumo Logic) need no
adapter. These translate the ones that can't:

  uptimerobot   Uptime Robot's alert webhook, using the JSON template in the signals guide
  alertmanager  Prometheus Alertmanager / Grafana webhooks (one signal per alert)
  statuspage    Atlassian Statuspage subscriber webhooks (Square, GitHub, …): incidents and
                component changes become signals; scheduled maintenance becomes a maintenance window

Each returns the keyword arguments for automation.receive_signal (or does its own write, for
maintenance), so routing, correlation and evidence capture are exactly the core path's.
"""
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import MaintenanceWindow, StatusPage

MAX_TEXT = 10000


class AdapterError(ValueError):
    pass


def _text(value, limit=MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


# --- Uptime Robot --------------------------------------------------------------------------------

UPTIMEROBOT_FIELDS = ("monitorID", "monitorURL", "monitorFriendlyName", "alertType",
                      "alertTypeFriendlyName", "alertDetails", "alertDateTime")


def uptimerobot(payload: dict, scope: dict) -> dict:
    """alertType 1 = down, 2 = up, 3 = SSL/domain expiry (a warning: active)."""
    unfilled = [k for k in UPTIMEROBOT_FIELDS if str(payload.get(k, "")).startswith("*")]
    if unfilled:
        raise AdapterError(f"Uptime Robot didn't fill in {', '.join(unfilled)}: check the alert "
                           "contact's POST value and use its Send Test.")
    monitor = _text(payload.get("monitorID"), 64)
    kind = str(payload.get("alertType", ""))
    if not monitor or kind not in {"1", "2", "3"}:
        raise AdapterError("Expected monitorID and alertType (1 down, 2 up, 3 expiry).")
    name = _text(payload.get("monitorFriendlyName"), 200) or _text(payload.get("monitorURL"), 200)
    state = "resolved" if kind == "2" else "active"
    detail = _text(payload.get("alertDetails")) or _text(payload.get("alertTypeFriendlyName"))
    return {
        "title": f"{name} is {_text(payload.get('alertTypeFriendlyName'), 40) or state}"[:512],
        "source": "uptimerobot", "signal": f"{detail} ({_text(payload.get('monitorURL'), 500)})",
        "source_event_id": f"{monitor}:{kind}:{_text(payload.get('alertDateTime'), 40)}",
        "correlation_key": f"monitor-{monitor}", "state": state, "payload": payload,
        **{k: _text(scope.get(k), 128) for k in ("customer", "service", "supplier")},
    }


# --- Alertmanager / Grafana ----------------------------------------------------------------------

def alertmanager(payload: dict) -> list[dict]:
    """One signal per alert. Labels customer/service/supplier set the scope."""
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        raise AdapterError("Expected an Alertmanager webhook with an alerts list.")
    signals = []
    for alert in alerts:
        labels, notes = alert.get("labels") or {}, alert.get("annotations") or {}
        key = _text(alert.get("fingerprint"), 128) or _text(labels.get("alertname"), 128)
        if not key:
            raise AdapterError("Each alert needs a fingerprint or an alertname label.")
        state = "resolved" if alert.get("status") == "resolved" else "active"
        name = _text(labels.get("alertname"), 200) or "Alert"
        summary = _text(notes.get("summary"), 300)
        signals.append({
            "title": f"{name}: {summary}"[:512] if summary else name,
            "source": "alertmanager",
            "signal": _text(notes.get("description")) or summary or name,
            "source_event_id": f"{key}:{state}:{_text(alert.get('startsAt'), 40)}",
            "correlation_key": f"alert-{key}", "state": state, "payload": alert,
            **{k: _text(labels.get(k), 128) for k in ("customer", "service", "supplier")},
        })
    return signals


# --- Statuspage ----------------------------------------------------------------------------------

INCIDENT_RESOLVED = {"resolved", "postmortem"}
MAINTENANCE_DONE = {"completed"}


def _latest_update(incident: dict) -> dict:
    updates = [u for u in incident.get("incident_updates") or [] if isinstance(u, dict)]
    return max(updates, key=lambda u: str(u.get("created_at", "")), default={})


def _maintenance(page: StatusPage, incident: dict) -> MaintenanceWindow:
    starts = parse_datetime(str(incident.get("scheduled_for") or ""))
    ends = parse_datetime(str(incident.get("scheduled_until") or ""))
    if starts is None or ends is None or ends <= starts:
        raise AdapterError("Scheduled maintenance needs scheduled_for before scheduled_until.")
    if incident.get("status") in MAINTENANCE_DONE:
        ends = max(starts, min(ends, timezone.now()))
    window, _ = MaintenanceWindow.objects.update_or_create(
        external_id=f"statuspage:{incident.get('id')}", supplier=page.supplier,
        defaults={"starts_at": starts, "ends_at": ends,
                  "reason": _text(incident.get("name"), 512) or "Scheduled maintenance",
                  "created_by": f"statuspage:{page.host}"})
    return window


def _incident_signal(page: StatusPage, payload: dict, incident: dict) -> dict:
    incident_id = _text(incident.get("id"), 64)
    if not incident_id:
        raise AdapterError("The incident has no id.")
    update = _latest_update(incident)
    status = _text(incident.get("status"), 32)
    return {
        "title": f"{page.supplier}: {_text(incident.get('name'), 400)}"[:512],
        "source": "statuspage", "supplier": page.supplier,
        "signal": f"[{status}] {_text(update.get('body')) or _text(incident.get('name'))}",
        "source_event_id": f"{incident_id}:{_text(update.get('id'), 64) or status}",
        "correlation_key": f"statuspage-incident-{incident_id}",
        "state": "resolved" if status in INCIDENT_RESOLVED else "active",
        "evidence_url": f"https://{page.host}/incidents/{incident_id}", "payload": payload,
    }


def _component_signal(page: StatusPage, payload: dict, change: dict) -> dict:
    component = payload.get("component") or {}
    component_id = _text(change.get("component_id") or component.get("id"), 64)
    new = _text(change.get("new_status"), 32)
    if not component_id or not new:
        raise AdapterError("The component update needs component_id and new_status.")
    name = _text(component.get("name"), 200) or component_id
    return {
        "title": f"{page.supplier}: {name} is {new.replace('_', ' ')}"[:512],
        "source": "statuspage", "supplier": page.supplier,
        "signal": f"{name}: {_text(change.get('old_status'), 32)} → {new}",
        "source_event_id": _text(change.get("id"), 64) or f"{component_id}:{new}",
        "correlation_key": f"statuspage-component-{component_id}",
        "state": "resolved" if new == "operational" else "active",
        "evidence_url": f"https://{page.host}/", "payload": payload,
    }


def statuspage(payload: dict, page: StatusPage):
    """Returns signal kwargs, or a MaintenanceWindow for scheduled maintenance."""
    incident, change = payload.get("incident"), payload.get("component_update")
    if isinstance(incident, dict):
        if incident.get("scheduled_for"):
            return _maintenance(page, incident)
        return _incident_signal(page, payload, incident)
    if isinstance(change, dict):
        return _component_signal(page, payload, change)
    raise AdapterError("Expected a Statuspage incident or component_update webhook.")
