"""
Notification seam (ADR-013, ADR-066) — page a target via a swappable provider. `send()` delivers a
title + message and `clear()` withdraws an earlier page; both are **best-effort**: they return
`(ok, error)` and never raise into the domain, so a paging failure is never an escalation failure.

Providers, chosen by `settings.PAGING_PROVIDER`:
- `ntfy` (local default): POST to a topic. ntfy topics are public by default, so a deployed ntfy
  uses an access token / self-hosted server (NTFY_TOKEN / NTFY_BASE_URL).
- `grafana_irm` (deployed environments, ADR-066): POST to a Grafana IRM *formatted webhook*
  integration. IRM groups alerts by `alert_uid` and resolves a group when `state` is `ok`, with no
  template setup. Routing on the `tier` field sends each page to that tier's IRM schedule; IRM
  delivers (push/SMS/phone) and records the acknowledgement. Watch keeps the escalation clock.
"""
import logging
from dataclasses import dataclass, field

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_provider_override = None  # test seam
TIMEOUT = 5


@dataclass(frozen=True)
class Message:
    """One page. `topic` and `priority`/`tags` are ntfy's; `alert` (uid, tier, incident, assignee,
    link) is for providers that track a page until it is withdrawn, like Grafana IRM."""
    topic: str
    title: str
    body: str
    priority: str = "default"
    tags: list = field(default_factory=list)
    alert: dict | None = None


class NtfyProvider:
    """POST to `<base>/<topic>` with ntfy's header protocol (Title/Priority/Tags), optional token."""

    name = "ntfy"

    def __init__(self):
        self._base = settings.NTFY_BASE_URL.rstrip("/")
        self._token = settings.NTFY_TOKEN

    def send(self, page: Message):
        headers = {"Title": page.title, "Priority": page.priority}
        if page.tags:
            headers["Tags"] = ",".join(page.tags)
        if self._token:
            headers["Authorization"] = "Bearer %s" % self._token
        resp = requests.post(
            "%s/%s" % (self._base, page.topic), data=page.body.encode("utf-8"), headers=headers,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()

    def clear(self, alert, title):
        """ntfy has nothing to withdraw: a push, once sent, is just a message."""


class GrafanaIrmProvider:
    """POST to a Grafana IRM formatted-webhook integration. The integration URL carries its own
    secret, so it is configured as one (GRAFANA_IRM_WEBHOOK_URL, from SSM in deployed envs)."""

    name = "grafana_irm"

    def __init__(self):
        self._url = settings.GRAFANA_IRM_WEBHOOK_URL
        if not self._url:
            raise RuntimeError("GRAFANA_IRM_WEBHOOK_URL is not set")

    def _post(self, alert, title, message, state):
        body = {
            "alert_uid": alert["uid"],
            "title": title,
            "message": message,
            "state": state,
            "link_to_upstream_details": alert.get("link", ""),
            "image_url": "",
            # Not IRM fields: IRM keeps the whole payload, so routes and templates can use these.
            "tier": alert.get("tier", ""),
            "incident": alert.get("incident", ""),
            "assignee": alert.get("assignee", ""),
            "environment": settings.PAGING_ENV,
        }
        requests.post(self._url, json=body, timeout=TIMEOUT).raise_for_status()

    def send(self, page: Message):
        if not page.alert:
            raise ValueError("an IRM page needs an alert (uid, tier, link)")
        self._post(page.alert, page.title, page.body, "alerting")

    def clear(self, alert, title):
        self._post(alert, title, "Watch: this tier is no longer responsible.", "ok")


PROVIDERS = {p.name: p for p in (NtfyProvider, GrafanaIrmProvider)}


def provider():
    if _provider_override is not None:
        return _provider_override
    name = settings.PAGING_PROVIDER
    if name not in PROVIDERS:
        raise RuntimeError(f"unknown PAGING_PROVIDER {name!r}; expected one of {sorted(PROVIDERS)}")
    return PROVIDERS[name]()


def provider_name() -> str:
    """The configured provider's name, without constructing it (so a missing secret can't raise)."""
    return getattr(_provider_override or PROVIDERS.get(settings.PAGING_PROVIDER), "name", "custom")


def send(topic, title, message, priority="default", tags=None, alert=None):
    """Best-effort page → (ok: bool, error: str). Never raises (ADR-013 fire-and-forget)."""
    try:
        provider().send(Message(topic, title, message, priority, list(tags or []), alert))
        return True, ""
    except Exception as exc:  # noqa: BLE001 - paging must never break the caller
        logger.warning("notify: page to %s failed: %s", topic or (alert or {}).get("uid"), exc)
        return False, str(exc)[:300]


def clear(alert, title):
    """Best-effort withdrawal of an earlier page → (ok, error). Never raises."""
    try:
        provider().clear(alert, title)
        return True, ""
    except Exception as exc:  # noqa: BLE001 - paging must never break the caller
        logger.warning("notify: clearing %s failed: %s", alert.get("uid"), exc)
        return False, str(exc)[:300]


def set_provider_for_tests(p):
    global _provider_override
    _provider_override = p
