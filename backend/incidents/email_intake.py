"""Inbound email → signal, reply, or nothing (ADR-058). Transport-independent: the SES receipt
Lambda and `wt email-in` both hand over the same raw RFC 822 bytes.

  new message      → a signal through the routing path (`automation.receive_signal`), scoped by
                     EmailRoute, so a supplier's mail fans out like any supplier signal
  reply            → appended to the assessment it answers (In-Reply-To/References against our
                     outbound Message-IDs and earlier inbound ones, else an [ASM-0042] subject token)
  auto-reply, bounce, list mail, our own mail, a repeat → ignored, with the reason
"""
import hashlib
import html
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from io import BytesIO

from django.conf import settings
from django.db import transaction

from . import assessments, automation, evidence_capture, operations
from .models import Assessment, AssessmentEvent, EmailRoute, EvidenceAttachment, OutboxMessage

SOURCE = "email"
INTAKE_USERNAME = "email-intake"  # uploader of record for attachments; inactive, can't log in
MAX_ATTACHMENTS = 8
MAX_TEXT = 10000
TOKEN = re.compile(r"\[(ASM-\d+)\]", re.IGNORECASE)
AUTH = re.compile(r"\b(spf|dkim|dmarc)=(\w+)", re.IGNORECASE)
QUOTE_START = re.compile(r"^(On .+wrote:|-{2,}\s*Original Message\s*-{2,}|From: .+)$", re.IGNORECASE)
BULK_PRECEDENCE = {"bulk", "junk", "list", "auto_reply"}
SYSTEM_SENDERS = {"mailer-daemon", "postmaster"}


@dataclass
class Result:
    outcome: str  # "signal" | "reply" | "ignored"
    reason: str = ""
    assessments: list = field(default_factory=list)  # [(Assessment, created)]


# --- reading the message -------------------------------------------------------------------------

def parse(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parse(BytesIO(raw))


def message_id(message, raw: bytes) -> str:
    """The message's own id, or a content hash so a retry of an id-less message still dedupes."""
    found = (message.get("Message-ID") or "").strip().strip("<>")
    return found or f"sha256-{hashlib.sha256(raw).hexdigest()}"


def sender(message) -> str:
    return parseaddr(str(message.get("From", "")))[1].strip().lower()


def authentication(message) -> dict:
    """SPF/DKIM/DMARC as the receiving server recorded them (SES adds Authentication-Results).
    Missing means nobody checked, which we treat as unverified, not as a pass."""
    verdict = {"spf": "none", "dkim": "none", "dmarc": "none"}
    for header in message.get_all("Authentication-Results", []):
        for name, value in AUTH.findall(str(header)):
            verdict[name.lower()] = value.lower()
    verdict["verified"] = verdict["dmarc"] == "pass"
    return verdict


def ignore_reason(message, from_address: str) -> str:
    """Why this message must not become a signal, or "" if it should."""
    auto = str(message.get("Auto-Submitted", "no")).strip().lower()
    if auto and auto != "no":
        return f"automatic message (Auto-Submitted: {auto})"
    if str(message.get("Precedence", "")).strip().lower() in BULK_PRECEDENCE:
        return "bulk or auto-reply (Precedence)"
    if message.get("X-Autoreply") or message.get("X-Autorespond"):
        return "auto-reply"
    if message.get_content_type() == "multipart/report":
        return "delivery report (bounce)"
    local, _, domain = from_address.partition("@")
    if local in SYSTEM_SENDERS:
        return "delivery report (bounce)"
    if not from_address:
        return "no sender address"
    if domain == settings.WATCH_EMAIL_DOMAIN.lower():
        return "sent by Watch itself"
    return ""


def text_of(message) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None or not part.get_payload():
        return ""
    content = part.get_content()
    if part.get_content_type() == "text/html":
        content = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", "", content)))
    return content.strip()


def without_quoted_reply(text: str) -> str:
    """The new part of a reply: stop at the quoted original, drop `>` lines."""
    kept = []
    for line in text.splitlines():
        if QUOTE_START.match(line.strip()):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip() or text.strip()


def route_for(address: str) -> dict:
    """customer/service/supplier for this sender: an exact address beats its domain."""
    domain = address.partition("@")[2]
    routes = {r.sender: r for r in EmailRoute.objects.filter(active=True, sender__in=[address, domain])}
    route = routes.get(address) or routes.get(domain)
    if route is None:
        return {"customer": "", "service": "", "supplier": ""}
    return {"customer": route.customer, "service": route.service, "supplier": route.supplier}


# --- threading -----------------------------------------------------------------------------------

def _referenced_ids(message) -> list[str]:
    ids = []
    for name in ("In-Reply-To", "References"):
        ids += re.findall(r"<([^>]+)>", str(message.get(name, "")))
    return ids


def thread_target(message) -> Assessment | None:
    """The assessment this message answers, if any."""
    ids = _referenced_ids(message)
    ours = f"@{settings.WATCH_EMAIL_DOMAIN.lower()}"
    receipt_ids = [i.rsplit("@", 1)[0] for i in ids if i.lower().endswith(ours)]
    for receipt_id in receipt_ids:
        receipt = OutboxMessage.objects.filter(pk=receipt_id).select_related(
            "communication__assessment").first() if _is_uuid(receipt_id) else None
        if receipt:
            return receipt.communication.assessment
    if ids:
        original = Assessment.objects.filter(payload__email__message_id__in=ids).order_by("created_at").first()
        if original:
            return original
        earlier = AssessmentEvent.objects.filter(data__email__message_id__in=ids).select_related(
            "assessment").first()
        if earlier:
            return earlier.assessment
    token = TOKEN.search(str(message.get("Subject", "")))
    return Assessment.objects.filter(number=token.group(1).upper()).first() if token else None


def _is_uuid(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F-]{36}", value))


# --- attachments ---------------------------------------------------------------------------------

def _intake_user():
    return operations.system_user(INTAKE_USERNAME)


class _Part(BytesIO):
    def __init__(self, data: bytes, name: str):
        super().__init__(data)
        self.name = name


def store_attachments(row: Assessment, message) -> tuple[list[dict], list[str]]:
    """Keep what evidence rules allow; report what was skipped and why (nothing silently dropped)."""
    kept, skipped = [], []
    parts = list(message.iter_attachments())
    for index, part in enumerate(parts):
        name = part.get_filename() or f"attachment-{index + 1}"
        if index >= MAX_ATTACHMENTS:
            skipped.append(f"{name}: more than {MAX_ATTACHMENTS} attachments")
            continue
        try:
            content, content_type, filename = operations.validate_evidence(
                _Part(part.get_payload(decode=True) or b"", name))
        except ValueError as exc:
            skipped.append(f"{name}: {exc}")
            continue
        item = EvidenceAttachment.objects.create(
            assessment=row, uploaded_by=_intake_user(), filename=filename, content_type=content_type,
            sha256=hashlib.sha256(content).hexdigest(), content=content)
        kept.append({"attachment_id": str(item.pk), "filename": filename,
                     "content_type": content_type, "sha256": item.sha256, "bytes": len(content)})
    return kept, skipped


# --- the two outcomes ----------------------------------------------------------------------------

def _envelope(message, mid: str, from_address: str, auth: dict) -> dict:
    return {"message_id": mid, "from": from_address, "subject": str(message.get("Subject", "")),
            "to": [a for _, a in getaddresses(message.get_all("To", []))],
            "cc": [a for _, a in getaddresses(message.get_all("Cc", []))],
            "date": str(message.get("Date", "")), "authentication": auth}


def _note(text: str, auth: dict, skipped: list[str]) -> str:
    lines = [text[:MAX_TEXT] or "(no text)"]
    if not auth["verified"]:
        lines.append(f"\n[Sender not verified: DMARC {auth['dmarc']}, SPF {auth['spf']}, DKIM {auth['dkim']}.]")
    if skipped:
        lines.append("\n[Attachments not kept: " + "; ".join(skipped) + "]")
    return "\n".join(lines)


def _signal_at(message):
    try:
        return parsedate_to_datetime(str(message.get("Date")))
    except (TypeError, ValueError):
        return None


@transaction.atomic
def _reply(row: Assessment, message, envelope: dict) -> Result:
    if AssessmentEvent.objects.filter(assessment=row, data__email__message_id=envelope["message_id"]).exists():
        return Result("ignored", "already received", [(row, False)])
    locked = Assessment.objects.select_for_update().get(pk=row.pk)
    attachments, skipped = store_attachments(locked, message)
    body = _note(without_quoted_reply(text_of(message)), envelope["authentication"], skipped)
    data = {"email": envelope, **({"attachments": attachments} if attachments else {})}
    assessments.append(locked, "comment", body, envelope["from"], data)
    locked.save(update_fields=["updated_at"])
    return Result("reply", f"appended to {locked.number}", [(locked, False)])


@transaction.atomic
def _signal(message, envelope: dict) -> Result:
    subject = envelope["subject"].strip() or "(no subject)"
    text = text_of(message)
    rows = automation.receive_signal(
        title=subject[:512], source=SOURCE, signal=text[:MAX_TEXT] or subject,
        source_event_id=envelope["message_id"], signal_at=_signal_at(message),
        payload={"email": envelope}, evidence_url=evidence_capture.first_allowed(text),
        **route_for(envelope["from"]))
    for routed in rows:
        row = routed[0]
        if routed.how not in {"created", "attached"}:
            continue
        attachments, skipped = store_attachments(row, message)
        if attachments or skipped or not envelope["authentication"]["verified"]:
            # A comment carrying an attachments list, like a photo note: the workspace renders it
            # and counts it as evidence (an evidence_attached event holds a single attachment).
            data = {"email": envelope, **({"attachments": attachments} if attachments else {})}
            assessments.append(row, "comment", _note("Received by email.", envelope["authentication"],
                                                     skipped), envelope["from"], data)
    hows = {routed.how for routed in rows}
    if "created" in hows:
        reason = "new signal"
    elif hows & {"attached", "correlated"}:
        reason = "added to an open assessment for the same customer and service"
    else:
        reason = "already received"
    return Result("signal", reason, rows)


def receive(raw: bytes) -> Result:
    message = parse(raw)
    from_address = sender(message)
    if reason := ignore_reason(message, from_address):
        return Result("ignored", reason)
    envelope = _envelope(message, message_id(message, raw), from_address, authentication(message))
    target = thread_target(message)
    if target is not None:
        return _reply(target, message, envelope)
    return _signal(message, envelope)
