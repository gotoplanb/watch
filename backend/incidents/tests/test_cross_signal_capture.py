"""Cross-signal correlation and status-page screenshots (ADR-060)."""
from email.message import EmailMessage

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from rest_framework.test import APIClient

from incidents import automation, email_intake, evidence_capture
from incidents.management.commands import operations_tick
from incidents.models import (
    Assessment, AssessmentCorrelation, EmailRoute, EvidenceAttachment, EvidenceCapture,
    ServiceDependency, StatusPage,
)

pytestmark = pytest.mark.django_db
JPEG = b"\xff\xd8\xff" + b"x" * 64
STATUS = "https://status.square.example/incidents/abc"


@pytest.fixture
def square():
    StatusPage.objects.create(supplier="Square", host="square.example")
    for customer in ("A", "B"):
        ServiceDependency.objects.create(customer=customer, service="checkout", supplier="Square",
                                         active=True)


def sumo(**extra):
    return automation.receive_signal(title="checkout 5xx", source="sumo", signal="5xx at 4%",
                                     customer="A", service="checkout", source_event_id="s1",
                                     correlation_key="sumo-alert-9", **extra)


def square_signal(state="active", event="q1", **extra):
    return automation.receive_signal(title="Square degraded", source="statuspage", supplier="Square",
                                     signal=f"payments {state}", source_event_id=event,
                                     correlation_key="sq-incident-1", state=state, **extra)


# --- cross-signal correlation --------------------------------------------------------------------

def test_supplier_signal_attaches_to_the_open_scope_and_opens_the_rest(square):
    [(ours, _)] = sumo()
    routed = {r[0].customer: r for r in square_signal()}
    assert (routed["A"][0].pk, routed["A"].how) == (ours.pk, "attached")
    assert routed["B"].how == "created"
    related = ours.history.get(kind="related_signal")
    assert related.body.startswith("statuspage (Square): Square degraded")
    assert AssessmentCorrelation.objects.filter(assessment=ours, source="statuspage",
                                                key="sq-incident-1").exists()


def test_the_attached_suppliers_recovery_finds_our_assessment(square):
    [(ours, _)] = sumo()
    square_signal()
    recovered = square_signal(state="resolved", event="q2")
    assert sorted(r[0].customer for r in recovered) == ["A", "B"]
    assert {r.how for r in recovered} == {"correlated"}
    assert ours.history.filter(kind="signal_resolved").exists()


def test_a_retried_delivery_is_a_duplicate_not_a_second_attachment(square):
    square_signal()
    sumo()  # A/checkout already open (from Square), so our alert attaches to it
    again = square_signal()
    assert {r.how for r in again} == {"correlated"}  # same key: a re-fire, recorded once each
    row = Assessment.objects.get(customer="A")
    assert row.history.filter(kind="related_signal").count() == 1


def test_retry_of_the_original_event_is_a_duplicate():
    [(first, _)] = automation.receive_signal(title="t", source="sumo", signal="s", customer="A",
                                             service="checkout", source_event_id="dup")
    [routed] = automation.receive_signal(title="t", source="sumo", signal="s", customer="A",
                                         service="checkout", source_event_id="dup")
    assert (routed[0].pk, routed.how) == (first.pk, "duplicate")
    assert not first.history.filter(kind="related_signal").exists()


# --- status-page links and the allow-list --------------------------------------------------------

def test_allow_list(square, settings):
    assert evidence_capture.allowed("https://square.example/x")
    assert evidence_capture.allowed(STATUS)
    assert not evidence_capture.allowed("https://evil-square.example/x")
    assert not evidence_capture.allowed("http://status.square.example/x")
    settings.DEBUG = True
    assert evidence_capture.allowed("http://status.square.example/x")
    StatusPage.objects.update(active=False)
    assert not evidence_capture.allowed(STATUS)
    assert str(StatusPage(supplier="GitHub", host="githubstatus.com")) == "GitHub: githubstatus.com"


def test_host_is_normalized():
    page = StatusPage.objects.create(supplier="S", host=" https://Status.Example.com/path ")
    assert page.host == "status.example.com"


def test_link_queues_one_capture_for_the_whole_fan_out(square):
    rows = [r[0] for r in square_signal(evidence_url=STATUS)]
    capture = EvidenceCapture.objects.get()
    assert set(capture.assessments.all()) == set(rows)
    assert all(r.history.filter(kind="evidence_linked").exists() for r in rows)
    evidence_capture.link(STATUS, rows[:1])  # a burst reuses the queued capture
    assert EvidenceCapture.objects.count() == 1


def test_unlisted_link_is_recorded_but_not_captured():
    [(row, _)] = sumo(evidence_url="https://elsewhere.example/status")
    assert not EvidenceCapture.objects.exists()
    assert "Not captured" in row.history.get(kind="evidence_linked").body


def test_private_hosts_are_refused_by_the_page_guard():
    for host in ("localhost", "127.0.0.1", "10.0.0.5", "169.254.169.254", "[::1]"):
        assert evidence_capture._private(host)
    assert not evidence_capture._private("status.square.example")

    class Route:
        def __init__(self, url):
            self.request = type("R", (), {"url": url})()
            self.done = None

        def abort(self):
            self.done = "abort"

        def continue_(self):
            self.done = "continue"

    blocked, passed = Route("http://169.254.169.254/latest"), Route(STATUS)
    evidence_capture._guard(blocked)
    evidence_capture._guard(passed)
    assert (blocked.done, passed.done) == ("abort", "continue")


# --- the worker job ------------------------------------------------------------------------------

def _queued(square):
    return [r[0] for r in square_signal(evidence_url=STATUS)], EvidenceCapture.objects.get()


def test_capture_files_one_screenshot_on_every_citing_assessment(square, monkeypatch):
    rows, capture = _queued(square)
    monkeypatch.setattr(evidence_capture, "render", lambda url: (JPEG, url + "?v=2"))
    operations_tick.run_job("capture", capture.pk)
    capture.refresh_from_db()
    assert (capture.state, capture.final_url) == ("done", STATUS + "?v=2")
    for row in rows:
        event = row.history.get(kind="evidence_captured")
        assert event.data["captured_at"] == capture.captured_at.isoformat()
        assert "landed on" in event.body
        attachment = EvidenceAttachment.objects.get(pk=event.data["attachments"][0]["attachment_id"])
        assert attachment.content_type == "image/jpeg"
        assert not attachment.uploaded_by.is_active
    evidence_capture.run(capture.pk)  # already done: nothing happens
    assert EvidenceAttachment.objects.count() == 2


@pytest.mark.parametrize("render, reason", [
    (lambda url: (_ for _ in ()).throw(TimeoutError("slow")), "TimeoutError: slow"),
    (lambda url: (JPEG, "https://phish.example/"), "redirected off the allow-list"),
    (lambda url: (b"", STATUS), "empty or larger than 5 MB"),
])
def test_capture_failures_are_recorded(square, monkeypatch, render, reason):
    rows, capture = _queued(square)
    monkeypatch.setattr(evidence_capture, "render", render)
    evidence_capture.run(capture.pk)
    capture.refresh_from_db()
    assert capture.state == "failed"
    assert reason in capture.error
    assert all(reason in r.history.get(kind="evidence_capture_failed").body for r in rows)
    assert not EvidenceAttachment.objects.exists()


def test_worker_picks_up_queued_captures(square):
    _, capture = _queued(square)
    assert ("capture", capture.pk) in operations_tick._queued_candidates()


def test_workspace_shows_the_screenshot_as_evidence(square, monkeypatch):
    rows, capture = _queued(square)
    monkeypatch.setattr(evidence_capture, "render", lambda url: (JPEG, url))
    evidence_capture.run(capture.pk)
    viewer = User.objects.create_user("viewer")
    viewer.groups.add(Group.objects.get_or_create(name="T1")[0])
    client = Client()
    client.force_login(viewer)
    page = client.get(f"/ui/assessments/{rows[0].pk}/?tab=evidence").content.decode()
    assert "watch-evidence-image" in page
    assert "Screenshot of" in page
    assert 'href="https://status.square.example/incidents/abc"' in page  # the body's URL is linked


# --- API and email -------------------------------------------------------------------------------

def test_signal_api_takes_an_evidence_url(square, settings):
    settings.INTAKE_WEBHOOK_SECRET = "s"
    client = APIClient()
    client.credentials(HTTP_X_WATCH_WEBHOOK_SECRET="s")
    sumo()
    reply = client.post("/api/signals/intake", {
        "title": "Square degraded", "source": "statuspage", "signal": "payments",
        "supplier": "Square", "customer": "A", "service": "checkout", "evidence_url": STATUS},
        format="json").json()
    assert (reply["outcome"], reply["assessments"][0]["how"]) == ("attached", "attached")
    assert EvidenceCapture.objects.filter(url=STATUS).exists()
    single = client.post("/api/assessments/intake", {
        "title": "x", "source": "s", "signal": "y", "evidence_url": STATUS}, format="json")
    assert single.status_code == 201


def test_supplier_email_links_its_status_page_and_attaches_to_open_scope(square):
    sumo()
    EmailRoute.objects.create(sender="square.example", supplier="Square")
    message = EmailMessage()
    message["From"] = "noreply@square.example"
    message["Subject"] = "Degraded payments"
    message["Message-ID"] = "<sq-1@square.example>"
    message.set_content(f"We're investigating elevated errors. Details: {STATUS}")
    message.add_attachment(JPEG, maintype="image", subtype="jpeg", filename="banner.jpg")
    result = email_intake.receive(bytes(message))
    assert result.reason == "new signal"  # B/checkout is new; A/checkout attached
    row_a = Assessment.objects.get(customer="A")
    assert row_a.history.filter(kind="related_signal").exists()
    assert EvidenceAttachment.objects.filter(assessment=row_a, filename="banner.jpg").exists()
    assert EvidenceCapture.objects.get().url == STATUS


def test_email_added_to_an_open_assessment_says_so():
    automation.receive_signal(title="t", source="sumo", signal="s", customer="A", service="checkout")
    EmailRoute.objects.create(sender="customer-a.example", customer="A", service="checkout")
    message = EmailMessage()
    message["From"] = "ops@customer-a.example"
    message["Subject"] = "Payments failing"
    message["Message-ID"] = "<c-1@customer-a.example>"
    message.set_content("Card payments failing.")
    assert email_intake.receive(bytes(message)).reason.startswith("added to an open assessment")
