"""
Trace-store seam (ADR-022 / ADR-026) — find the error spans for a session/user within a time window.

Provider-swappable like `flags`: `none` (no backend) or a vendor. Each provider queries its backend
for error spans carrying the subject attribute in the window and normalizes the result to the span
dict shape `ErrorSpan` stores. A backend that can't answer raises `TraceStoreError` so the caller
marks the check *indeterminate* rather than a false "clean".

Providers:
  none          — no backend wired (raises).
  tempo         — in-VPC Tempo query-frontend, TraceQL, no auth (staging obs plane, ADR-016 §2).
  grafana_cloud — Grafana Cloud Tempo: same TraceQL, HTTPS + basic auth (managed vendor).
  datadog       — Datadog APM v2 spans search (POST /api/v2/spans/events/search).
  sumologic     — Sumo Logic tracing via the async Search Job API (create → poll → messages).

Querying a trace store is not per-query billed anywhere; ingest/retention/indexing is. Session
Check can only find spans the backend actually kept (see the tail-sampling "authenticated" policy).
"""
import logging
import time
from datetime import datetime, timezone

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# subject_kind -> the span attribute set by the session_tagging middleware.
_ATTR = {"session": "session.id", "user": "session.user"}

_JSON = "application/json"


class TraceStoreError(Exception):
    """The backend could not answer (misconfigured / query failed) — check is indeterminate."""


class NoneProvider:
    """No trace backend wired — every query is indeterminate (never a false clean)."""

    def find_error_spans(self, subject_kind, subject_hash, window_from, window_to):
        raise TraceStoreError("no trace backend configured (set TRACE_STORE_PROVIDER)")


# --- Tempo / Grafana Cloud (TraceQL) -----------------------------------------

class TempoProvider:
    """Query a Tempo query-frontend via TraceQL for error spans carrying the subject attribute.
    `_auth` is None for the in-VPC Tempo; Grafana Cloud subclasses with basic auth."""

    _auth = None

    def __init__(self):
        self._base = settings.TEMPO_QUERY_URL.rstrip("/")

    def find_error_spans(self, subject_kind, subject_hash, window_from, window_to):
        attr = _ATTR.get(subject_kind)
        if not attr or not subject_hash:
            return []
        # select() pulls attributes the search response otherwise omits (Tempo returns only the
        # queried attrs): parse_search reads name/http.status_code, and triage (ADR-036) needs the
        # status code to tell a server-side fault from noise.
        query = '{ span.%s = "%s" && status = error } | select(span.http.status_code, name)' % (
            attr, subject_hash
        )
        params = {"q": query, "limit": 200}
        if window_from:
            params["start"] = int(window_from.timestamp())
        if window_to:
            params["end"] = int(window_to.timestamp())
        try:
            resp = requests.get(self._base + "/api/search", params=params, timeout=5, auth=self._auth)
            resp.raise_for_status()
            return parse_search(resp.json())
        except requests.RequestException as exc:
            logger.warning("trace_store: tempo query failed: %s", exc)
            raise TraceStoreError("tempo query failed") from exc


class GrafanaCloudProvider(TempoProvider):
    """Grafana Cloud Tempo — identical TraceQL + response to `tempo`, but HTTPS + HTTP basic auth
    (username = the Tempo instance id, password = an access-policy token). Prod's managed vendor."""

    def __init__(self):
        self._base = settings.GRAFANA_CLOUD_TEMPO_URL.rstrip("/")
        self._auth = (settings.GRAFANA_CLOUD_TEMPO_USER, settings.GRAFANA_CLOUD_TEMPO_TOKEN)


def parse_search(data: dict) -> list[dict]:
    """Flatten a Tempo search response into error-span dicts (pure — unit-tested directly)."""
    spans = []
    for trace in data.get("traces") or []:
        trace_id = trace.get("traceID", "")
        root_service = trace.get("rootServiceName", "")
        for span_set in trace.get("spanSets") or []:
            for span in span_set.get("spans") or []:
                attrs = {a.get("key"): _attr_value(a.get("value")) for a in span.get("attributes") or []}
                spans.append(
                    {
                        "trace_id": trace_id,
                        "span_id": span.get("spanID", ""),
                        "name": span.get("name") or attrs.get("name", ""),
                        "service": attrs.get("service.name") or root_service,
                        "status": "ERROR",
                        "http_status": _as_int(attrs.get("http.status_code")),
                        "ts": _as_ts(span.get("startTimeUnixNano")),
                    }
                )
    return spans


# --- Datadog APM (v2 spans search) -------------------------------------------

class DatadogProvider:
    """Datadog APM spans search: POST /api/v2/spans/events/search with a query filter. Only spans
    kept by a retention filter (indexed) are searchable — the Datadog analogue of tail-sampling."""

    # Datadog tag syntax for a custom span attribute is `@key:value`.
    _ATTR = {"session": "@session.id", "user": "@session.user"}

    def __init__(self):
        self._url = "https://api.%s/api/v2/spans/events/search" % settings.DATADOG_SITE.strip("/")
        self._headers = {
            "DD-API-KEY": settings.DATADOG_API_KEY,
            "DD-APPLICATION-KEY": settings.DATADOG_APP_KEY,
            "Content-Type": _JSON,
        }

    def find_error_spans(self, subject_kind, subject_hash, window_from, window_to):
        attr = self._ATTR.get(subject_kind)
        if not attr or not subject_hash:
            return []
        body = {
            "data": {
                "type": "search_request",
                "attributes": {
                    "filter": {
                        "query": '%s:%s status:error' % (attr, subject_hash),
                        "from": _iso(window_from) or "now-1h",
                        "to": _iso(window_to) or "now",
                    },
                    "sort": "-timestamp",
                    "page": {"limit": 200},
                },
            }
        }
        try:
            resp = requests.post(self._url, json=body, headers=self._headers, timeout=8)
            resp.raise_for_status()
            return parse_datadog(resp.json())
        except requests.RequestException as exc:
            logger.warning("trace_store: datadog query failed: %s", exc)
            raise TraceStoreError("datadog query failed") from exc


def parse_datadog(data: dict) -> list[dict]:
    """Normalize a Datadog spans-search response (`data[].attributes`) into error-span dicts."""
    spans = []
    for item in data.get("data") or []:
        a = item.get("attributes") or {}
        custom = a.get("custom") or {}
        http = custom.get("http") if isinstance(custom.get("http"), dict) else {}
        spans.append(
            {
                "trace_id": str(a.get("trace_id") or ""),
                "span_id": str(a.get("span_id") or item.get("id") or ""),
                "name": a.get("resource_name") or a.get("name") or "",
                "service": a.get("service") or "",
                "status": "ERROR",
                "http_status": _as_int(http.get("status_code") or custom.get("http.status_code")),
                "ts": _as_ts_ms(a.get("start_timestamp") or a.get("timestamp")),
            }
        )
    return spans


# --- Sumo Logic (async Search Job API) ---------------------------------------

class SumoLogicProvider:
    """Sumo Logic tracing via the Search Job API: create a job, poll until it stops gathering, fetch
    messages. Basic auth (accessId:accessKey). The span query targets Sumo's tracing fields — the
    field names (`operation`, `service`, `status`) follow Sumo's span schema; validate against a real
    Sumo tracing account before prod (documented assumption, ADR-026)."""

    _ATTR = {"session": "session.id", "user": "session.user"}

    def __init__(self):
        self._base = settings.SUMO_API_ENDPOINT.rstrip("/")
        self._auth = (settings.SUMO_ACCESS_ID, settings.SUMO_ACCESS_KEY)

    def find_error_spans(self, subject_kind, subject_hash, window_from, window_to):
        attr = self._ATTR.get(subject_kind)
        if not attr or not subject_hash:
            return []
        # Sumo tracing span query: filter by the subject field + error status. `_view=spans` is Sumo's
        # tracing view; kept as a template so the field names can be tuned without code changes.
        query = '_view=spans | where %s = "%s" and status = "error"' % (attr, subject_hash)
        payload = {
            "query": query,
            "from": _iso(window_from) or "",
            "to": _iso(window_to) or "",
            "timeZone": "UTC",
        }
        try:
            job = requests.post(
                self._base + "/api/v1/search/jobs", json=payload, auth=self._auth, timeout=8,
                headers={"Content-Type": _JSON, "Accept": _JSON},
            )
            job.raise_for_status()
            job_id = job.json().get("id")
            if not job_id:
                raise TraceStoreError("sumo: no search job id")
            self._await_job(job_id)
            msgs = requests.get(
                "%s/api/v1/search/jobs/%s/messages" % (self._base, job_id),
                params={"offset": 0, "limit": 200}, auth=self._auth, timeout=8,
                headers={"Accept": _JSON},
            )
            msgs.raise_for_status()
            return parse_sumo(msgs.json())
        except requests.RequestException as exc:
            logger.warning("trace_store: sumologic query failed: %s", exc)
            raise TraceStoreError("sumologic query failed") from exc

    def _await_job(self, job_id, tries=20):
        """Poll the search job until it stops gathering results (bounded — the worker isn't request-
        bound, but never spin forever)."""
        url = "%s/api/v1/search/jobs/%s" % (self._base, job_id)
        for _ in range(tries):
            r = requests.get(url, auth=self._auth, timeout=8, headers={"Accept": _JSON})
            r.raise_for_status()
            state = r.json().get("state", "")
            if state in ("DONE GATHERING RESULTS", "CANCELLED", "FORCE PAUSED"):
                return
            time.sleep(1)
        raise TraceStoreError("sumo: search job did not complete in time")


def parse_sumo(data: dict) -> list[dict]:
    """Normalize a Sumo Logic search-job messages response (`messages[].map`) into error-span dicts."""
    spans = []
    for msg in data.get("messages") or []:
        m = msg.get("map") or {}
        spans.append(
            {
                "trace_id": m.get("trace_id") or m.get("traceId") or "",
                "span_id": m.get("span_id") or m.get("spanId") or "",
                "name": m.get("operation") or m.get("name") or "",
                "service": m.get("service") or "",
                "status": "ERROR",
                "http_status": _as_int(m.get("http_status_code") or m.get("status_code")),
                "ts": _as_ts_ms(m.get("_messagetime") or m.get("timestamp")),
            }
        )
    return spans


# --- shared normalizers ------------------------------------------------------

def _attr_value(value):
    if isinstance(value, dict):
        return value.get("stringValue") or value.get("intValue") or value.get("value")
    return value


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_ts(nano):
    try:
        return datetime.fromtimestamp(int(nano) / 1e9, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _as_ts_ms(millis):
    try:
        return datetime.fromtimestamp(int(millis) / 1e3, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(dt):
    """A datetime as ISO-8601 (vendor time filters accept it), or "" when absent."""
    return dt.isoformat() if dt else ""


# --- provider registry -------------------------------------------------------

_PROVIDERS = {
    "none": NoneProvider,
    "tempo": TempoProvider,
    "grafana_cloud": GrafanaCloudProvider,
    "datadog": DatadogProvider,
    "sumologic": SumoLogicProvider,
}

_provider = None


def _get_provider():
    global _provider
    if _provider is None:
        cls = _PROVIDERS.get(settings.TRACE_STORE_PROVIDER, NoneProvider)
        _provider = cls()
    return _provider


def find_error_spans(subject_kind, subject_hash, window_from=None, window_to=None) -> list[dict]:
    return _get_provider().find_error_spans(subject_kind, subject_hash, window_from, window_to)


def set_provider_for_tests(provider) -> None:
    global _provider
    _provider = provider
