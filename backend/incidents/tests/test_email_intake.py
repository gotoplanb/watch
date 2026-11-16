"""Inbound email (ADR-058): new messages become scoped signals, replies thread onto their assessment,
and automatic mail is ignored with a reason."""
from email.message import EmailMessage

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from rest_framework.test import APIClient

from incidents import communications, email_intake
from incidents.models import Assessment, EmailRoute, EvidenceAttachment, OutboxMessage, ServiceDependency

pytestmark = pytest.mark.django_db
PNG = b"\x89PNG\r\n\x1a\nexample"
SECRET = "email-secret"


def mail(subject="Card payments failing", body="Since 9:40 every card payment fails.",
         sender="ops@customer.example", mid="<m1@customer.example>", **headers) -> EmailMessage:
    message = EmailMessage()
    message["From"] = f"Ops <{sender}>"
    message["To"] = "support@local.example.test"
    message["Subject"] = subject
    message["Date"] = "Mon, 16 Nov 2026 09:45:00 +0000"
    if mid:
        message["Message-ID"] = mid
    for name, value in headers.items():
        message[name.replace("_", "-")] = value
    message.set_content(body)
    return message


def receive(message):
    return email_intake.receive(bytes(message))


# --- new signals ---------------------------------------------------------------------------------

def test_unrouted_email_opens_an_unverified_signal():
    result = receive(mail())
    assert (result.outcome, result.reason) == ("signal", "new signal")
    row = Assessment.objects.get()
    assert (row.title, row.source, row.customer) == ("Card payments failing", "email", "")
    assert row.signal == "Since 9:40 every card payment fails."
    assert row.payload["email"]["from"] == "ops@customer.example"
    assert row.signal_at.hour == 9
    note = row.history.get(kind="comment")
    assert "Sender not verified: DMARC none" in note.body


def test_routes_scope_the_signal_and_an_exact_address_wins():
    EmailRoute.objects.create(sender="@Customer.example", customer="Customer A", service="checkout")
    EmailRoute.objects.create(sender="cfo@customer.example", customer="Customer A Finance")
    receive(mail())
    receive(mail(sender="cfo@customer.example", mid="<m2@customer.example>"))
    customers = sorted(Assessment.objects.values_list("customer", flat=True))
    assert customers == ["Customer A", "Customer A Finance"]
    assert EmailRoute.objects.get(customer="Customer A").sender == "customer.example"
    assert str(EmailRoute.objects.get(customer="Customer A")) == "customer.example → Customer A / checkout"


def test_supplier_mail_fans_out_like_any_supplier_signal():
    EmailRoute.objects.create(sender="status.square.example", supplier="Square")
    for customer in ("A", "B"):
        ServiceDependency.objects.create(customer=customer, service="checkout", supplier="Square",
                                         active=True)
    result = receive(mail(sender="noreply@status.square.example", subject="Degraded payments"))
    assert sorted(r.customer for r, _ in result.assessments) == ["A", "B"]


def test_verified_sender_has_no_warning_and_repeat_is_deduped():
    message = mail(Authentication_Results="amazonses.com; spf=pass; dkim=pass; dmarc=pass")
    receive(message)
    row = Assessment.objects.get()
    assert not row.history.filter(kind="comment").exists()
    again = receive(message)
    assert (again.outcome, again.reason) == ("signal", "already received")
    assert Assessment.objects.count() == 1


def test_attachments_kept_or_skipped_with_reasons():
    message = mail()
    message.add_attachment(PNG, maintype="image", subtype="png", filename="screen.png")
    message.add_attachment(b"\x00\x01binary", maintype="application", subtype="octet-stream",
                           filename="tool.exe")
    message.add_attachment(b"%PDF-1.4 example", maintype="application", subtype="pdf",
                           filename="report.pdf")
    receive(message)
    row = Assessment.objects.get()
    event = row.history.get(kind="comment")
    assert [a["filename"] for a in event.data["attachments"]] == ["screen.png", "report.pdf"]
    assert "tool.exe: Supported evidence" in event.body
    # The workspace renders it: images inline, the PDF as a download link only.
    viewer = User.objects.create_user("viewer")
    viewer.groups.add(Group.objects.get_or_create(name="T1")[0])
    client = Client()
    client.force_login(viewer)
    page = client.get(f"/ui/assessments/{row.pk}/").content.decode()
    assert page.count("watch-evidence-image") == 1
    assert "Download report.pdf" in page
    attachment = EvidenceAttachment.objects.filter(filename="screen.png").get()
    assert attachment.uploaded_by.username == email_intake.INTAKE_USERNAME
    assert not attachment.uploaded_by.is_active
    assert not attachment.uploaded_by.has_usable_password()


def test_attachment_limit():
    message = mail()
    for i in range(email_intake.MAX_ATTACHMENTS + 1):
        message.add_attachment(PNG, maintype="image", subtype="png", filename=f"s{i}.png")
    receive(message)
    assert EvidenceAttachment.objects.count() == email_intake.MAX_ATTACHMENTS
    assert "more than 8 attachments" in Assessment.objects.get().history.get(kind="comment").body


def test_html_only_mail_and_missing_id_and_subject():
    message = EmailMessage()
    message["From"] = "ops@customer.example"
    message.set_content("<p>Payments <b>down</b> &amp; slow</p><script>x()</script>", subtype="html")
    raw = bytes(message)
    result = email_intake.receive(raw)
    row = result.assessments[0][0]
    assert row.title == "(no subject)"
    assert "Payments" in row.signal
    assert "down" in row.signal
    assert "& slow" in row.signal
    assert "x()" not in row.signal
    assert row.payload["email"]["message_id"].startswith("sha256-")
    assert email_intake.receive(raw).reason == "already received"


# --- replies -------------------------------------------------------------------------------------

def _reply(**headers):
    return mail(subject="Re: Card payments failing",
                body="Still failing for us.\n\nOn Mon, Watch wrote:\n> We are investigating.",
                mid="<r1@customer.example>", **headers)


def test_reply_to_our_outbound_mail_threads_and_drops_the_quote():
    receive(mail())
    row = Assessment.objects.get()
    draft = communications.draft(row.pk, actor="t1a", subject="Investigating", body="We are on it.",
                                 recipients=["ops@customer.example"])
    communications.send(draft.pk, actor="t1a", expected_version=1)
    receipt = OutboxMessage.objects.get()
    assert f"<{receipt.pk}@local.example.test>".encode() in communications.message_bytes(receipt)
    result = receive(_reply(In_Reply_To=f"<{receipt.pk}@local.example.test>"))
    assert (result.outcome, result.reason) == ("reply", f"appended to {row.number}")
    note = row.history.filter(kind="comment").last()
    assert note.body.startswith("Still failing for us.")
    assert "investigating" not in note.body.lower()
    assert note.actor == "ops@customer.example"
    assert receive(_reply(In_Reply_To=f"<{receipt.pk}@local.example.test>")).reason == \
        "already received"


def test_reply_threads_on_the_original_mail_an_earlier_reply_or_a_token():
    receive(mail())
    row = Assessment.objects.get()
    assert receive(_reply(References="<m1@customer.example>")).outcome == "reply"
    later = mail(subject="Re: Re: Card payments", mid="<r2@customer.example>",
                 In_Reply_To="<r1@customer.example>")
    assert receive(later).outcome == "reply"
    tokened = mail(subject=f"Update please [{row.number.lower()}]", mid="<r3@customer.example>")
    assert receive(tokened).reason == f"appended to {row.number}"
    assert Assessment.objects.count() == 1


def test_unknown_references_open_a_new_signal():
    stray = mail(mid="<x@customer.example>", In_Reply_To="<abc@local.example.test>",
                 References="<00000000-0000-0000-0000-000000000000@local.example.test>")
    assert receive(stray).outcome == "signal"


# --- ignored -------------------------------------------------------------------------------------

@pytest.mark.parametrize("headers, sender, reason", [
    ({"Auto_Submitted": "auto-replied"}, "ops@customer.example", "automatic message"),
    ({"Precedence": "bulk"}, "ops@customer.example", "bulk or auto-reply"),
    ({"X_Autoreply": "yes"}, "ops@customer.example", "auto-reply"),
    ({}, "MAILER-DAEMON@mx.example", "delivery report"),
    ({}, "watch@local.example.test", "sent by Watch itself"),
])
def test_automatic_and_own_mail_is_ignored(headers, sender, reason):
    result = receive(mail(sender=sender, **headers))
    assert result.outcome == "ignored"
    assert result.reason.startswith(reason)
    assert not Assessment.objects.exists()


def test_bounce_report_and_missing_sender_are_ignored():
    report = EmailMessage()
    report["From"] = "ops@customer.example"
    report.set_content("x")
    report.make_mixed()
    report.replace_header("Content-Type", "multipart/report; report-type=delivery-status")
    assert email_intake.receive(bytes(report)).reason == "delivery report (bounce)"
    anonymous = EmailMessage()
    anonymous.set_content("who?")
    assert email_intake.receive(bytes(anonymous)).reason == "no sender address"


def test_quote_helpers():
    assert email_intake.without_quoted_reply("> only quoted") == "> only quoted"
    assert email_intake.without_quoted_reply("new\n-----Original Message-----\nold") == "new"
    empty = EmailMessage()
    empty["From"] = "a@b.example"
    assert email_intake.text_of(empty) == ""


# --- endpoint ------------------------------------------------------------------------------------

@pytest.fixture
def intake(settings):
    settings.INTAKE_WEBHOOK_SECRET = SECRET
    client = APIClient()
    client.credentials(HTTP_X_WATCH_WEBHOOK_SECRET=SECRET)
    return client


def _post(client, raw):
    return client.post("/api/email/inbound", data=raw, content_type="message/rfc822")


def test_endpoint_creates_and_reports(intake):
    created = _post(intake, bytes(mail()))
    assert created.status_code == 201
    body = created.json()
    assert (body["outcome"], body["assessments"][0]["created"]) == ("signal", True)
    ignored = _post(intake, bytes(mail(Precedence="bulk", mid="<b@customer.example>")))
    assert (ignored.status_code, ignored.json()["outcome"]) == (200, "ignored")


def test_endpoint_refuses_bad_secret_empty_and_oversized(intake, monkeypatch):
    assert _post(APIClient(), bytes(mail())).status_code == 401
    assert _post(intake, b"   ").status_code == 400
    monkeypatch.setattr("incidents.assessment_api.RawEmailParser.max_bytes", 10)
    assert _post(intake, bytes(mail())).status_code == 400
    assert not User.objects.filter(username=email_intake.INTAKE_USERNAME).exists()

