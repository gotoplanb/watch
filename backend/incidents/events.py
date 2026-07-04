"""
Outbound event fan-out (ADR-023) — the single place Watch pushes domain events to registered
receivers. The outbound counterpart of intake (ADR-009): builds a canonical envelope, records a
WebhookDelivery per matching subscription, and (in local mode) POSTs it HMAC-signed.

Called from `services` (ADR-010's one decision implementation) so the same events fire whether a
human or the auto-escalation Lambda drove the change. **Never raises into the domain** — a bad
subscriber can't roll back an escalation.
"""
import hashlib
import hmac
import json
import logging
import uuid

import requests
from django.conf import settings
from django.utils import timezone

from .models import DeliveryStatus, WebhookDelivery, WebhookSubscription

logger = logging.getLogger(__name__)


def emit(event_type: str, data: dict) -> None:
    """Fan an event out to all active subscriptions matching `event_type`. Guarded — logs and
    swallows any error so emission can never break or roll back the caller's transaction."""
    try:
        _emit(event_type, data)
    except Exception:  # pragma: no cover - defensive: emission must never break the domain
        logger.warning("events.emit failed for %s", event_type, exc_info=True)


def _emit(event_type: str, data: dict) -> None:
    subs = [s for s in WebhookSubscription.objects.filter(active=True) if s.matches(event_type)]
    if not subs:
        return
    envelope = {
        "event": event_type,
        "id": str(uuid.uuid4()),
        "at": timezone.now().isoformat(),
        "data": data,
    }
    for sub in subs:
        _deliver(sub, envelope)


def _deliver(sub: WebhookSubscription, envelope: dict) -> WebhookDelivery:
    delivery = WebhookDelivery.objects.create(
        subscription=sub,
        event_type=envelope["event"],
        event_id=envelope["id"],
        payload=envelope,
    )
    if settings.WEBHOOKS_LOCAL_MODE:  # cloud path leaves it `pending` for the SQS worker
        _post(delivery, sub, envelope)
    return delivery


def _post(delivery: WebhookDelivery, sub: WebhookSubscription, envelope: dict) -> None:
    raw = json.dumps(envelope, separators=(",", ":")).encode()
    signature = hmac.new(sub.secret.encode(), raw, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Watch-Event": envelope["event"],
        "X-Watch-Delivery": envelope["id"],
        "X-Watch-Signature": f"sha256={signature}",
    }
    delivery.attempts += 1
    try:
        resp = requests.post(sub.url, data=raw, headers=headers, timeout=5)
        delivery.status_code = resp.status_code
        delivery.status = DeliveryStatus.DELIVERED if resp.ok else DeliveryStatus.FAILED
    except requests.RequestException as exc:
        delivery.status = DeliveryStatus.FAILED
        delivery.error = str(exc)[:500]
    delivery.save(update_fields=["attempts", "status", "status_code", "error", "updated_at"])
