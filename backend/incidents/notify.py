"""
Notification seam (ADR-013) — page a target via a swappable provider (ntfy). `send()` POSTs a
title + message to a topic and is **best-effort**: it returns `(ok, error)` and never raises into the
domain, so a paging failure is never an escalation failure. ntfy topics are public by default → prod
uses an access token / self-hosted server (settings.NTFY_TOKEN / NTFY_BASE_URL); the seam keeps that
swap to one class.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_provider_override = None  # test seam


class NtfyProvider:
    """POST to `<base>/<topic>` with ntfy's header protocol (Title/Priority/Tags), optional token."""

    def __init__(self):
        self._base = settings.NTFY_BASE_URL.rstrip("/")
        self._token = settings.NTFY_TOKEN

    def send(self, topic, title, message, priority="default", tags=None):
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = ",".join(tags)
        if self._token:
            headers["Authorization"] = "Bearer %s" % self._token
        resp = requests.post(
            "%s/%s" % (self._base, topic), data=message.encode("utf-8"), headers=headers, timeout=5
        )
        resp.raise_for_status()


def send(topic, title, message, priority="default", tags=None):
    """Best-effort page → (ok: bool, error: str). Never raises (ADR-013 fire-and-forget)."""
    try:
        provider = _provider_override or NtfyProvider()
        provider.send(topic, title, message, priority=priority, tags=tags)
        return True, ""
    except Exception as exc:  # noqa: BLE001 - paging must never break the caller
        logger.warning("notify: page to topic %s failed: %s", topic, exc)
        return False, str(exc)[:300]


def set_provider_for_tests(provider):
    global _provider_override
    _provider_override = provider
