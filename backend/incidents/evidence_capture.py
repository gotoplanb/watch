"""Status-page screenshots as evidence (ADR-060).

A supplier's status page has no timestamped permalink, so what it said, and when, is gone once it
changes. When a signal cites a status page, Watch records the link at once and the operations
worker takes a screenshot: the image, the capture time, the URL it finally landed on and a SHA-256,
filed as evidence on every assessment that cited it.

Only allow-listed hosts are loaded (StatusPage), over https. A redirect off the list fails the
capture, and the browser refuses requests to private, loopback and link-local addresses, so a
signal can't make Watch fetch internal URLs.
"""
import hashlib
import ipaddress
import logging
import re
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import assessments, operations
from .models import EvidenceAttachment, EvidenceCapture, StatusPage

logger = logging.getLogger(__name__)

CAPTURE_ACTOR = "system:capture"
MAX_BYTES = 5 * 1024 * 1024  # the evidence limit (operations.validate_evidence)
REUSE_WINDOW = timedelta(minutes=2)  # one screenshot serves a burst of fanned-out signals
URL = re.compile(r"https?://[^\s<>\"')\]]+")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def allowed(url: str) -> bool:
    """https (http too in local development) to an active allow-listed host or its subdomain."""
    parts = urlsplit(url)
    if parts.scheme != "https" and not (settings.DEBUG and parts.scheme == "http"):
        return False
    host = _host(url)
    hosts = StatusPage.objects.filter(active=True).values_list("host", flat=True)
    return bool(host) and any(host == h or host.endswith(f".{h}") for h in hosts)


def first_allowed(text: str) -> str:
    """The first allow-listed status-page link in some text (an email body), or ""."""
    return next((url for url in URL.findall(text or "") if allowed(url)), "")


def link(url: str, rows) -> EvidenceCapture | None:
    """Record the cited link on each assessment and queue a capture if the host is allowed."""
    ok = allowed(url)
    capture = None
    if ok:
        recent = timezone.now() - REUSE_WINDOW
        capture = (EvidenceCapture.objects.filter(url=url, state__in=["queued", "running"],
                                                  requested_at__gte=recent).first()
                   or EvidenceCapture.objects.create(url=url))
        capture.assessments.add(*rows)
    note = ("Status page linked; a screenshot is being taken." if ok else
            "Status page linked. Not captured: its host isn't on the status-page allow-list.")
    for row in rows:
        assessments.append(row, "evidence_linked", f"{note}\n{url}", CAPTURE_ACTOR,
                           {"url": url, "capture_id": str(capture.pk) if capture else None})
    return capture


# --- rendering (the only part that needs a browser) ----------------------------------------------

def _private(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved


def _guard(route):
    """Refuse any request from the page to an internal address."""
    if _private(_host(route.request.url)):
        return route.abort()
    return route.continue_()


def render(url: str) -> tuple[bytes, str]:  # pragma: no cover - exercised by the integration test
    """Load the page in headless Chromium and screenshot it. Returns (jpeg bytes, final URL)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            page.route("**/*", _guard)
            page.goto(url, wait_until="load", timeout=20000)
            page.wait_for_timeout(1500)  # status pages often fill in their incidents by script
            image = page.screenshot(full_page=True, type="jpeg", quality=80)
            if len(image) > MAX_BYTES:
                image = page.screenshot(full_page=False, type="jpeg", quality=80)
            return image, page.url
        finally:
            browser.close()


# --- the worker job ------------------------------------------------------------------------------

def _claim(pk):
    with transaction.atomic():
        capture = EvidenceCapture.objects.select_for_update().get(pk=pk)
        if capture.state != "queued":
            return None
        capture.state = "running"
        capture.save(update_fields=["state"])
        return capture


def _file(capture, image: bytes):
    """Attach the screenshot to every assessment that cited the page."""
    stamp = capture.captured_at.strftime("%Y%m%dT%H%M%SZ")
    filename = f"{_host(capture.final_url)}-{stamp}.jpg"
    digest = hashlib.sha256(image).hexdigest()
    uploader = operations.system_user("evidence-capture")
    for row in capture.assessments.all():
        item = EvidenceAttachment.objects.create(
            assessment=row, uploaded_by=uploader, filename=filename, content_type="image/jpeg",
            sha256=digest, content=image)
        body = (f"Screenshot of {capture.url} at {capture.captured_at.isoformat()}"
                + (f" (landed on {capture.final_url})" if capture.final_url != capture.url else ""))
        assessments.append(row, "evidence_captured", body, CAPTURE_ACTOR, {
            "url": capture.url, "final_url": capture.final_url,
            "captured_at": capture.captured_at.isoformat(), "capture_id": str(capture.pk),
            "attachments": [{"attachment_id": str(item.pk), "filename": filename,
                             "content_type": "image/jpeg", "sha256": digest, "bytes": len(image)}]})


def _fail(capture, reason: str):
    capture.state, capture.error = "failed", reason
    capture.save(update_fields=["state", "error"])
    for row in capture.assessments.all():
        assessments.append(row, "evidence_capture_failed", f"Screenshot of {capture.url} failed: {reason}",
                           CAPTURE_ACTOR, {"url": capture.url, "capture_id": str(capture.pk)})


def run(pk):
    capture = _claim(pk)
    if capture is None:
        return
    try:
        image, final_url = render(capture.url)
    # Any browser failure (timeout, DNS, a refused navigation) is recorded on the case, not raised.
    except Exception as exc:  # noqa: BLE001
        logger.warning("Status page capture failed: %s", capture.url, exc_info=True)
        with transaction.atomic():
            _fail(capture, f"{type(exc).__name__}: {str(exc)[:200]}")
        return
    with transaction.atomic():
        if not allowed(final_url):
            _fail(capture, f"the page redirected off the allow-list, to {_host(final_url) or final_url}")
            return
        if not image or len(image) > MAX_BYTES:
            _fail(capture, "the screenshot was empty or larger than 5 MB")
            return
        capture.state, capture.final_url, capture.captured_at = "done", final_url, timezone.now()
        capture.save(update_fields=["state", "final_url", "captured_at"])
        _file(capture, image)
