"""
Trace-store seam (ADR-022) — find the error spans for a session/user within a time window.

Provider-swappable like `flags`: `none` (no backend wired) or `tempo` (TraceQL search). The
vendor / Grafana Cloud impl for prod is deferred. Hermetic tests inject a fake via
`set_provider_for_tests`. A backend that can't answer raises `TraceStoreError` so the caller marks
the check *indeterminate* rather than a false "clean".
"""
import logging
from datetime import datetime, timezone

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# subject_kind -> the span attribute we filter on (set by session_tagging middleware).
_ATTR = {"session": "session.id", "user": "session.user"}


class TraceStoreError(Exception):
    """The backend could not answer (misconfigured / query failed) — check is indeterminate."""


class NoneProvider:
    """No trace backend wired — every query is indeterminate (never a false clean)."""

    def find_error_spans(self, subject_kind, subject_hash, window_from, window_to):
        raise TraceStoreError("no trace backend configured (set TRACE_STORE_PROVIDER=tempo)")


class TempoProvider:
    """Query a Tempo query-frontend via TraceQL for error spans carrying the subject attribute."""

    def __init__(self):
        self._base = settings.TEMPO_QUERY_URL.rstrip("/")

    def find_error_spans(self, subject_kind, subject_hash, window_from, window_to):
        attr = _ATTR.get(subject_kind)
        if not attr or not subject_hash:
            return []
        query = '{ span.%s = "%s" && status = error }' % (attr, subject_hash)
        params = {"q": query, "limit": 200}
        if window_from:
            params["start"] = int(window_from.timestamp())
        if window_to:
            params["end"] = int(window_to.timestamp())
        try:
            resp = requests.get(self._base + "/api/search", params=params, timeout=5)
            resp.raise_for_status()
            return parse_search(resp.json())
        except requests.RequestException as exc:
            logger.warning("trace_store: tempo query failed: %s", exc)
            raise TraceStoreError("tempo query failed") from exc


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


_provider = None


def _get_provider():
    global _provider
    if _provider is None:
        _provider = TempoProvider() if settings.TRACE_STORE_PROVIDER == "tempo" else NoneProvider()
    return _provider


def find_error_spans(subject_kind, subject_hash, window_from=None, window_to=None) -> list[dict]:
    return _get_provider().find_error_spans(subject_kind, subject_hash, window_from, window_to)


def set_provider_for_tests(provider) -> None:
    global _provider
    _provider = provider
