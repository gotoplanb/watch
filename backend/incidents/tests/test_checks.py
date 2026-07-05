"""Hermetic tests for Session Check (ADR-022): span tagging + HMAC, trace-store parsing/providers,
the checks service (clean/errors/aged_out/indeterminate), the inbound webhook, and the /ui/ surface.
No Docker or network — the trace store is faked via set_provider_for_tests."""
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from incidents import checks as checks_svc
from incidents import trace_store
from incidents.models import CheckStatus, CheckSubjectKind, ErrorSpan, SessionCheck
from incidents.session_tagging import SessionTaggingMiddleware, hash_user_id


class FakeTraceStore:
    def __init__(self, spans=None, error=None):
        self.spans = spans or []
        self.error = error

    def find_error_spans(self, kind, subject_hash, wf, wt):
        if self.error:
            raise self.error
        return self.spans


@pytest.fixture(autouse=True)
def _reset_provider():
    yield
    trace_store.set_provider_for_tests(None)


@pytest.fixture
def client():
    return Client()


# --- span tagging + HMAC ---

def test_hash_user_id_keyed_and_empty(settings):
    settings.SESSION_USER_HMAC_KEY = ""
    assert hash_user_id("42") == ""            # no key -> disabled
    settings.SESSION_USER_HMAC_KEY = "k"
    a, b = hash_user_id("42"), hash_user_id("42")
    assert a == b and len(a) == 32             # deterministic
    assert hash_user_id("43") != a             # distinct users differ
    assert hash_user_id("") == ""              # no id


@pytest.mark.django_db
def test_middleware_mints_correlation_id_and_tags(client, settings):
    settings.SESSION_USER_HMAC_KEY = "k"       # exercise the session.user tag branch
    client.force_login(User.objects.create(username="u1"))
    client.get("/ui/incidents/")
    assert client.session.get("correlation_id")  # minted + persisted


def test_middleware_tag_is_noop_without_span():
    # _tag pulls the current (non-recording) span and set_attribute is a harmless no-op.
    SessionTaggingMiddleware._tag("cid", None)  # anonymous, no span -> must not raise


@pytest.mark.django_db
def test_session_id_shown_in_header(client):
    client.force_login(User.objects.create(username="u2"))
    resp = client.get("/ui/incidents/")
    assert b"click to copy your session id" in resp.content


# --- trace store ---

def test_parse_search_flattens_error_spans():
    data = {"traces": [{
        "traceID": "abc", "rootServiceName": "watch-backend",
        "spanSets": [{"spans": [
            {"spanID": "s1", "name": "GET /x", "startTimeUnixNano": "1700000000000000000",
             "attributes": [{"key": "http.status_code", "value": {"intValue": "500"}}]},
        ]}],
    }]}
    spans = trace_store.parse_search(data)
    assert len(spans) == 1
    s = spans[0]
    assert s["trace_id"] == "abc" and s["span_id"] == "s1" and s["status"] == "ERROR"
    assert s["http_status"] == 500 and s["ts"] is not None


def test_parse_search_empty():
    assert trace_store.parse_search({}) == []


def test_parse_search_defensive_fields():
    # non-dict attr value passthrough, unparseable http_status + timestamp -> None (no crash)
    data = {"traces": [{"traceID": "t", "spanSets": [{"spans": [
        {"spanID": "s", "startTimeUnixNano": "nope",
         "attributes": [{"key": "service.name", "value": "svc"},
                        {"key": "http.status_code", "value": {"stringValue": "x"}}]},
    ]}]}]}
    s = trace_store.parse_search(data)[0]
    assert s["service"] == "svc" and s["http_status"] is None and s["ts"] is None


def test_none_provider_raises():
    with pytest.raises(trace_store.TraceStoreError):
        trace_store.NoneProvider().find_error_spans("session", "x", None, None)


def test_tempo_provider_queries(settings, monkeypatch):
    settings.TRACE_STORE_PROVIDER = "tempo"
    settings.TEMPO_QUERY_URL = "http://tempo:3200"
    captured = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"traces": []}

    def fake_get(url, params=None, timeout=None, auth=None):
        captured["url"] = url
        captured["q"] = params["q"]
        captured["auth"] = auth
        return Resp()

    monkeypatch.setattr(trace_store.requests, "get", fake_get)
    out = trace_store.TempoProvider().find_error_spans(
        "session", "abc", timezone.now() - timedelta(hours=1), timezone.now()
    )
    assert out == []
    assert "/api/search" in captured["url"]
    assert 'span.session.id = "abc"' in captured["q"] and "status = error" in captured["q"]


def test_tempo_provider_unknown_kind_returns_empty(settings):
    settings.TRACE_STORE_PROVIDER = "tempo"
    assert trace_store.TempoProvider().find_error_spans("bogus", "x", None, None) == []


def test_tempo_provider_wraps_request_error(settings, monkeypatch):
    settings.TRACE_STORE_PROVIDER = "tempo"

    def boom(*a, **k):
        raise trace_store.requests.RequestException("down")

    monkeypatch.setattr(trace_store.requests, "get", boom)
    with pytest.raises(trace_store.TraceStoreError):
        trace_store.TempoProvider().find_error_spans("session", "x", None, None)


# --- checks service ---

@pytest.mark.django_db
def test_create_check_hashes_user_and_defaults_window(settings):
    settings.SESSION_USER_HMAC_KEY = "k"
    c = checks_svc.create_check(subject_kind=CheckSubjectKind.USER, subject_raw="42")
    assert c.subject_hash == hash_user_id("42") and c.subject_hash != "42"
    assert c.window_from and c.window_to and c.window_from < c.window_to


@pytest.mark.django_db
def test_create_check_session_uses_raw_id():
    c = checks_svc.create_check(subject_kind=CheckSubjectKind.SESSION, subject_raw="corr-123")
    assert c.subject_hash == "corr-123"  # correlation id is already non-secret


@pytest.mark.django_db
def test_run_check_clean_when_no_error_spans():
    trace_store.set_provider_for_tests(FakeTraceStore([]))
    c = checks_svc.create_and_run(subject_kind=CheckSubjectKind.SESSION, subject_raw="x")
    assert c.status == CheckStatus.DONE and c.verdict == "clean"


@pytest.mark.django_db
def test_run_check_errors_found_records_spans():
    trace_store.set_provider_for_tests(FakeTraceStore([
        {"trace_id": "t1", "span_id": "s1", "name": "boom", "service": "svc", "http_status": 500},
        {"trace_id": "t2"},
    ]))
    c = checks_svc.create_and_run(subject_kind=CheckSubjectKind.SESSION, subject_raw="x")
    assert c.status == CheckStatus.DONE and c.verdict == "errors_found:2"
    assert ErrorSpan.objects.filter(session_check=c).count() == 2


@pytest.mark.django_db
def test_run_check_rerun_is_idempotent():
    trace_store.set_provider_for_tests(FakeTraceStore([{"trace_id": "t1"}]))
    c = checks_svc.create_and_run(subject_kind=CheckSubjectKind.SESSION, subject_raw="x")
    checks_svc.run_session_check(c)  # re-run clears + re-adds
    assert ErrorSpan.objects.filter(session_check=c).count() == 1


@pytest.mark.django_db
def test_run_check_aged_out(settings):
    settings.CHECKS_TRACE_RETENTION_SECONDS = 60
    old = timezone.now() - timedelta(days=2)
    c = checks_svc.create_check(subject_kind=CheckSubjectKind.SESSION, subject_raw="x",
                                window_from=old - timedelta(hours=1), window_to=old)
    checks_svc.run_session_check(c)
    c.refresh_from_db()
    assert c.status == CheckStatus.INDETERMINATE and c.verdict == "aged_out"


@pytest.mark.django_db
def test_run_check_indeterminate_when_backend_unavailable():
    # default provider is NoneProvider -> raises -> indeterminate
    c = checks_svc.create_and_run(subject_kind=CheckSubjectKind.SESSION, subject_raw="x")
    assert c.status == CheckStatus.INDETERMINATE and c.verdict == "unavailable"


@pytest.mark.django_db
def test_run_check_no_subject_indeterminate(settings):
    settings.SESSION_USER_HMAC_KEY = ""  # user hash empty -> no subject
    c = checks_svc.create_check(subject_kind=CheckSubjectKind.USER, subject_raw="42")
    checks_svc.run_session_check(c)
    c.refresh_from_db()
    assert c.status == CheckStatus.INDETERMINATE and c.verdict == "no_subject"


@pytest.mark.django_db
def test_create_and_run_skips_run_when_not_local(settings):
    settings.CHECKS_LOCAL_MODE = False
    c = checks_svc.create_and_run(subject_kind=CheckSubjectKind.SESSION, subject_raw="x")
    assert c.status == CheckStatus.QUEUED  # cloud path enqueues; not run here


# --- webhook ---

@pytest.mark.django_db
def test_webhook_rejects_bad_secret(client, settings):
    settings.CHECKS_WEBHOOK_SECRET = "s3cret"
    resp = client.post("/api/checks/webhook", {"subject_kind": "session", "subject": "x"},
                       content_type="application/json", HTTP_X_WATCH_WEBHOOK_SECRET="nope")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_webhook_creates_and_runs(client, settings):
    settings.CHECKS_WEBHOOK_SECRET = "s3cret"
    trace_store.set_provider_for_tests(FakeTraceStore([{"trace_id": "t1"}]))
    resp = client.post("/api/checks/webhook",
                       {"subject_kind": "session", "subject": "corr-9", "source": "partner"},
                       content_type="application/json", HTTP_X_WATCH_WEBHOOK_SECRET="s3cret")
    assert resp.status_code == 201
    body = resp.json()
    assert body["verdict"] == "errors_found:1" and body["error_spans"] == 1
    assert SessionCheck.objects.filter(source="partner").count() == 1


# --- UI ---

@pytest.mark.django_db
def test_ui_check_list_run_and_detail(client):
    trace_store.set_provider_for_tests(FakeTraceStore([{"trace_id": "t1", "name": "boom"}]))
    client.force_login(User.objects.create(username="op"))
    assert client.get("/ui/checks/").status_code == 200
    # trigger a check from the UI
    resp = client.post("/ui/checks/run/", {"subject_kind": "session", "subject": "corr-1"})
    assert resp.status_code == 302
    check = SessionCheck.objects.get()
    assert check.verdict == "errors_found:1"
    detail = client.get(f"/ui/checks/{check.id}/")
    assert detail.status_code == 200 and b"boom" in detail.content


@pytest.mark.django_db
def test_ui_run_check_ignores_blank_subject(client):
    client.force_login(User.objects.create(username="op2"))
    client.post("/ui/checks/run/", {"subject_kind": "session", "subject": "  "})
    assert SessionCheck.objects.count() == 0


@pytest.mark.django_db
def test_model_strs():
    c = SessionCheck.objects.create(subject_kind="session", subject_hash="abcdef123456")
    assert "check session" in str(c)
    es = ErrorSpan.objects.create(session_check=c, trace_id="traceabc", name="boom")
    assert "boom" in str(es)
