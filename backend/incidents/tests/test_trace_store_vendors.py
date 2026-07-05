"""Hermetic tests for the vendor trace-store providers (ADR-026) — query construction, auth, and
response normalization per vendor, all with mocked HTTP. No network."""
import pytest

from incidents import trace_store


@pytest.fixture(autouse=True)
def _reset():
    yield
    trace_store.set_provider_for_tests(None)
    trace_store._provider = None


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise trace_store.requests.HTTPError("http %s" % self.status_code)


# --- Grafana Cloud (Tempo + basic auth) --------------------------------------

def test_grafana_cloud_queries_with_auth_and_parses(settings, monkeypatch):
    settings.GRAFANA_CLOUD_TEMPO_URL = "https://tempo-prod.grafana.net/"
    settings.GRAFANA_CLOUD_TEMPO_USER = "123456"
    settings.GRAFANA_CLOUD_TEMPO_TOKEN = "glc_secret"
    cap = {}

    def fake_get(url, params=None, timeout=None, auth=None):
        cap.update(url=url, params=params, auth=auth)
        return FakeResp({"traces": [{"traceID": "t1", "rootServiceName": "web",
            "spanSets": [{"spans": [{"spanID": "s1", "name": "GET /x",
                "attributes": [{"key": "http.status_code", "value": {"intValue": 500}}]}]}]}]})

    monkeypatch.setattr(trace_store.requests, "get", fake_get)
    spans = trace_store.GrafanaCloudProvider().find_error_spans("session", "abc", None, None)
    assert cap["url"] == "https://tempo-prod.grafana.net/api/search"
    assert cap["auth"] == ("123456", "glc_secret")
    assert '{ span.session.id = "abc" && status = error }' == cap["params"]["q"]
    assert spans[0]["trace_id"] == "t1" and spans[0]["service"] == "web" and spans[0]["http_status"] == 500


def test_grafana_cloud_user_kind_uses_session_user_attr(settings, monkeypatch):
    settings.GRAFANA_CLOUD_TEMPO_URL = "https://t/"
    cap = {}
    monkeypatch.setattr(trace_store.requests, "get",
                        lambda url, **k: (cap.update(k), FakeResp({"traces": []}))[1])
    trace_store.GrafanaCloudProvider().find_error_spans("user", "h", None, None)
    assert 'span.session.user = "h"' in cap["params"]["q"]


# --- Datadog (v2 spans search) -----------------------------------------------

def test_datadog_builds_query_and_parses(settings, monkeypatch):
    settings.DATADOG_SITE = "datadoghq.com"
    settings.DATADOG_API_KEY = "api-k"
    settings.DATADOG_APP_KEY = "app-k"
    cap = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        cap.update(url=url, body=json, headers=headers)
        return FakeResp({"data": [{"id": "sp1", "attributes": {
            "trace_id": "tr1", "span_id": "sp1", "service": "api", "resource_name": "GET /y",
            "custom": {"http": {"status_code": 503}}, "start_timestamp": 1_700_000_000_000}}]})

    monkeypatch.setattr(trace_store.requests, "post", fake_post)
    spans = trace_store.DatadogProvider().find_error_spans("session", "xyz", None, None)
    assert cap["url"] == "https://api.datadoghq.com/api/v2/spans/events/search"
    assert cap["headers"]["DD-API-KEY"] == "api-k" and cap["headers"]["DD-APPLICATION-KEY"] == "app-k"
    assert cap["body"]["data"]["attributes"]["filter"]["query"] == "@session.id:xyz status:error"
    assert spans[0]["trace_id"] == "tr1" and spans[0]["service"] == "api"
    assert spans[0]["name"] == "GET /y" and spans[0]["http_status"] == 503 and spans[0]["ts"] is not None


def test_parse_datadog_empty_and_defensive():
    assert trace_store.parse_datadog({}) == []
    s = trace_store.parse_datadog({"data": [{"attributes": {}}]})[0]
    assert s["status"] == "ERROR" and s["trace_id"] == "" and s["http_status"] is None


# --- Sumo Logic (async Search Job API) ---------------------------------------

def test_sumologic_job_flow_and_parse(settings, monkeypatch):
    settings.SUMO_API_ENDPOINT = "https://api.us2.sumologic.com/"
    settings.SUMO_ACCESS_ID = "acc-id"
    settings.SUMO_ACCESS_KEY = "acc-key"
    calls = []

    def fake_post(url, json=None, auth=None, timeout=None, headers=None):
        calls.append(("POST", url, auth, json))
        return FakeResp({"id": "job-1"})

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        calls.append(("GET", url))
        if url.endswith("/jobs/job-1"):
            return FakeResp({"state": "DONE GATHERING RESULTS"})
        return FakeResp({"messages": [{"map": {
            "trace_id": "tt", "span_id": "ss", "operation": "GET /z", "service": "svc",
            "status_code": 500, "_messagetime": 1_700_000_000_000}}]})

    monkeypatch.setattr(trace_store.requests, "post", fake_post)
    monkeypatch.setattr(trace_store.requests, "get", fake_get)
    spans = trace_store.SumoLogicProvider().find_error_spans("session", "s", None, None)
    post = next(c for c in calls if c[0] == "POST")
    assert post[1] == "https://api.us2.sumologic.com/api/v1/search/jobs"
    assert post[2] == ("acc-id", "acc-key")
    assert 'session.id = "s"' in post[3]["query"] and "error" in post[3]["query"]
    assert spans[0]["trace_id"] == "tt" and spans[0]["name"] == "GET /z" and spans[0]["http_status"] == 500


def test_sumologic_no_job_id_raises(settings, monkeypatch):
    settings.SUMO_API_ENDPOINT = "https://api/"
    monkeypatch.setattr(trace_store.requests, "post", lambda *a, **k: FakeResp({}))
    with pytest.raises(trace_store.TraceStoreError):
        trace_store.SumoLogicProvider().find_error_spans("session", "s", None, None)


def test_parse_sumo_empty():
    assert trace_store.parse_sumo({}) == []


# --- shared behaviour: registry, empty subject, error wrapping ---------------

@pytest.mark.parametrize("provider,cls", [
    ("none", trace_store.NoneProvider),
    ("tempo", trace_store.TempoProvider),
    ("grafana_cloud", trace_store.GrafanaCloudProvider),
    ("datadog", trace_store.DatadogProvider),
    ("sumologic", trace_store.SumoLogicProvider),
])
def test_registry_selects_provider(settings, provider, cls):
    settings.TRACE_STORE_PROVIDER = provider
    trace_store._provider = None
    assert isinstance(trace_store._get_provider(), cls)


def test_unknown_provider_falls_back_to_none(settings):
    settings.TRACE_STORE_PROVIDER = "bogus"
    trace_store._provider = None
    assert isinstance(trace_store._get_provider(), trace_store.NoneProvider)


@pytest.mark.parametrize("kind", ["datadog", "sumologic", "grafana_cloud"])
def test_empty_subject_returns_empty(settings, kind):
    settings.GRAFANA_CLOUD_TEMPO_URL = "https://t/"
    settings.SUMO_API_ENDPOINT = "https://s/"
    prov = {"datadog": trace_store.DatadogProvider, "sumologic": trace_store.SumoLogicProvider,
            "grafana_cloud": trace_store.GrafanaCloudProvider}[kind]()
    assert prov.find_error_spans("session", "", None, None) == []
    assert prov.find_error_spans("bogus", "x", None, None) == []


def test_datadog_wraps_request_error(settings, monkeypatch):
    def boom(*a, **k):
        raise trace_store.requests.RequestException("down")
    monkeypatch.setattr(trace_store.requests, "post", boom)
    with pytest.raises(trace_store.TraceStoreError):
        trace_store.DatadogProvider().find_error_spans("session", "x", None, None)


def test_grafana_cloud_wraps_request_error(settings, monkeypatch):
    settings.GRAFANA_CLOUD_TEMPO_URL = "https://t/"
    def boom(*a, **k):
        raise trace_store.requests.RequestException("down")
    monkeypatch.setattr(trace_store.requests, "get", boom)
    with pytest.raises(trace_store.TraceStoreError):
        trace_store.GrafanaCloudProvider().find_error_spans("session", "x", None, None)
