"""Saved read-only checks with explicit thresholds, samples, and preserved query results."""
import json
import math
import re
import time
from datetime import timedelta

import requests
from django.db import transaction
from django.conf import settings
from django.utils import timezone

from . import assessments
from .models import Assessment, DiagnosticRun


DIAGNOSTICS_ACTOR = "system:diagnostics"


class DiagnosticError(ValueError):
    pass


def _number(value):
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise DiagnosticError("The query did not return a numeric scalar.") from exc
    if not math.isfinite(result):
        raise DiagnosticError("The query returned a non-finite value.")
    return result


def render_query(query, assessment):
    for key in ("customer", "service", "supplier"):
        query = query.replace("${" + key + "}", json.dumps(getattr(assessment, key))[1:-1])
    if re.search(r"\$\{[^}]+\}", query):
        raise DiagnosticError("Unrecognized query template variable.")
    return query


def prometheus(query):
    response = requests.get(settings.WATCH_PROMETHEUS_URL.rstrip("/") + "/api/v1/query",
                            params={"query": query}, timeout=settings.WATCH_DIAGNOSTIC_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success" or payload.get("warnings"):
        raise DiagnosticError("Prometheus query failed or returned a partial/warning result.")
    data = payload.get("data", {})
    result = data.get("result")
    if data.get("resultType") == "scalar":
        sample = result
    elif data.get("resultType") == "vector" and isinstance(result, list) and len(result) == 1:
        sample = result[0].get("value")
    else:
        raise DiagnosticError("Expected one scalar or one time series; missing/ambiguous data is inconclusive.")
    if not isinstance(sample, list) or len(sample) != 2:
        raise DiagnosticError("Malformed Prometheus sample.")
    timestamp = _number(sample[0])
    if not -60 <= timezone.now().timestamp() - timestamp <= 300:
        raise DiagnosticError("The metric sample is stale or in the future.")
    return _number(sample[1])


def sumo(query, minutes):
    if not (settings.SUMO_API_ENDPOINT and settings.SUMO_ACCESS_ID and settings.SUMO_ACCESS_KEY):
        raise DiagnosticError("Sumo Logic credentials and endpoint are not configured.")
    now = timezone.now()
    # The v1 Search Job API requires session cookies; retain one Session per job.
    budget = time.monotonic() + 30
    def timeout():
        remaining = budget - time.monotonic()
        if remaining <= 0:
            raise DiagnosticError("Sumo search exceeded its execution budget.")
        return min(settings.WATCH_DIAGNOSTIC_TIMEOUT, remaining)
    with requests.Session() as session:
        session.auth = (settings.SUMO_ACCESS_ID, settings.SUMO_ACCESS_KEY)
        base = settings.SUMO_API_ENDPOINT.rstrip("/") + "/api/v1/search/jobs"
        response = session.post(base, json={"query": query, "from": (now - timedelta(minutes=minutes)).isoformat(),
                                            "to": now.isoformat(), "timeZone": "UTC"},
                                timeout=timeout())
        response.raise_for_status()
        job = response.json().get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9-]+", str(job)):
            raise DiagnosticError("Sumo returned an invalid job identifier.")
        for _ in range(15):
            response = session.get(f"{base}/{job}", timeout=timeout())
            response.raise_for_status()
            state = response.json().get("state")
            if state == "DONE GATHERING RESULTS":
                break
            if state not in {"GATHERING RESULTS", "NOT STARTED"}:
                raise DiagnosticError("Sumo search failed, was paused, or was cancelled.")
            time.sleep(1)
        else:
            raise DiagnosticError("Sumo search did not finish within its polling budget.")
        response = session.get(f"{base}/{job}/records", params={"offset": 0, "limit": 2},
                               timeout=timeout())
        response.raise_for_status()
        records = response.json().get("records", [])
        if len(records) != 1 or "value" not in records[0].get("map", {}):
            raise DiagnosticError("Sumo checks must return exactly one aggregate record with a numeric 'value' field.")
        return _number(records[0]["map"]["value"])


def _claim_run(assessment, rule, run_id):
    """Return (run, claimed); an existing run is only claimed while still queued."""
    if not run_id:
        return DiagnosticRun.objects.create(assessment=assessment, rule=rule, state="running", started_at=timezone.now()), True
    with transaction.atomic():
        run = DiagnosticRun.objects.select_for_update().get(pk=run_id, assessment=assessment, rule=rule)
        if run.state != "queued":
            return run, False
        run.state, run.started_at = "running", timezone.now()
        run.save(update_fields=["state", "started_at"])
    return run, True


def _snapshot(rule):
    threshold = rule.threshold if math.isfinite(rule.threshold) else str(rule.threshold)
    return {"rule": rule.name, "provider": rule.provider, "query": rule.query, "baseline_query": rule.baseline_query,
            "sample_query": rule.sample_query, "minimum_samples": rule.minimum_samples,
            "comparator": rule.comparator, "threshold": threshold, "lookback_minutes": rule.lookback_minutes}


def _validate(rule, query):
    if rule.minimum_samples < 1 or not math.isfinite(rule.threshold):
        raise DiagnosticError("A positive sample minimum and finite threshold are required.")
    if not query.strip() or not 1 <= rule.lookback_minutes <= 10080:
        raise DiagnosticError("The diagnostic query or observation window is invalid.")


def _measure_fixture(rule, assessment):
    if not (settings.DEBUG and assessment.payload.get("fixture") is True):
        raise DiagnosticError("Fixture queries are restricted to explicit local examples.")
    fixture = rule.fixture_result
    value = _number(fixture.get("value"))
    baseline = _number(fixture["baseline"]) if "baseline" in fixture else None
    samples = _number(fixture.get("samples", 0))
    return value, baseline, samples


def _measure_provider(rule, query, baseline_query, sample_query):
    provider = prometheus if rule.provider == "prometheus" else (lambda q: sumo(q, rule.lookback_minutes))
    if rule.provider not in {"prometheus", "sumo"}:
        raise DiagnosticError("Unknown diagnostic provider.")
    value = provider(query)
    baseline = provider(baseline_query) if baseline_query else None
    samples = provider(sample_query) if sample_query else None
    if samples is None:
        raise DiagnosticError("Configure a sample-count query; missing sample coverage cannot establish health.")
    return value, baseline, samples


def _measure(rule, assessment, query, baseline_query, sample_query):
    """Return (value, baseline, samples) from the fixture or the configured provider."""
    if rule.provider == "fixture":
        return _measure_fixture(rule, assessment)
    return _measure_provider(rule, query, baseline_query, sample_query)


def _breached(rule, value, baseline):
    if rule.comparator == "above":
        return value > rule.threshold
    if rule.comparator == "below":
        return value < rule.threshold
    if rule.comparator == "deviation" and baseline is not None and baseline != 0:
        return abs((value - baseline) / baseline) * 100 > rule.threshold
    raise DiagnosticError("A valid comparator and nonzero baseline are required.")


def _record_success(assessment, rule, run, snapshot, query, measurement):
    value, baseline, samples = measurement
    breached = _breached(rule, value, baseline)
    run.result = {**snapshot, "value": value, "baseline": baseline, "samples": samples,
                  "breached": breached, "window_to": timezone.now().isoformat(),
                  "window_from": (run.started_at - timedelta(minutes=rule.lookback_minutes)).isoformat()}
    run.state = "done"
    outcome = "threshold breached" if breached else "within configured bounds"
    with transaction.atomic():
        current = Assessment.objects.select_for_update().get(pk=assessment.pk)
        assessments.append(assessment, "evidence_added", f"{rule.name}: {outcome}",
                           DIAGNOSTICS_ACTOR, {"query": query, "source": rule.provider,
                                               "result": json.dumps(run.result, indent=2),
                                               "execution": "executed", "run_id": str(run.pk), **run.result})
        assessments.invalidate_conclusion(current)
        assessments.append(assessment, "query_completed", f"Completed check: {rule.name}", DIAGNOSTICS_ACTOR,
                           {"run_id": str(run.pk), "breached": breached})


def _evaluate(assessment, rule, run, snapshot):
    query, baseline_query, sample_query = [render_query(q, assessment) for q in (rule.query, rule.baseline_query, rule.sample_query)]
    snapshot.update(query=query, baseline_query=baseline_query, sample_query=sample_query)
    _validate(rule, query)
    measurement = _measure(rule, assessment, query, baseline_query, sample_query)
    if measurement[2] < rule.minimum_samples:
        raise DiagnosticError("Insufficient samples to establish customer impact or health.")
    _record_success(assessment, rule, run, snapshot, query, measurement)


def _record_failure(assessment, rule, run, snapshot, exc):
    run.state = "failed"
    # Do not persist raw HTTP exceptions: URLs can contain credentials or query material.
    run.error = str(exc) if isinstance(exc, DiagnosticError) else f"{rule.provider} query failed ({type(exc).__name__})."
    assessments.append(assessment, "query_failed", run.error, DIAGNOSTICS_ACTOR, {"run_id": str(run.pk), **snapshot})


def execute(assessment, rule, run_id=None):
    run, claimed = _claim_run(assessment, rule, run_id)
    if not claimed:
        return run
    snapshot = _snapshot(rule)
    with transaction.atomic():
        current = Assessment.objects.select_for_update().get(pk=assessment.pk)
        assessments.invalidate_conclusion(current)
    assessments.append(assessment, "query_started", f"Started check: {rule.name}", DIAGNOSTICS_ACTOR,
                       {**snapshot, "run_id": str(run.pk)})
    try:
        _evaluate(assessment, rule, run, snapshot)
    # DiagnosticError subclasses ValueError, so it is caught here too.
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        _record_failure(assessment, rule, run, snapshot, exc)
    run.finished_at = timezone.now()
    run.save(update_fields=["state", "result", "error", "finished_at"])
    return run
