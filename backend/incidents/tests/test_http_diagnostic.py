"""The one-off HTTP diagnostic (ADR-063, #70): failure % over a few requests, fenced off internal
addresses. DNS and HTTP are mocked; no network is touched."""
import socket
from unittest.mock import Mock

import pytest
import requests

from incidents import assessments, diagnostics
from incidents.models import DiagnosticRule
from incidents.operations_forms import DiagnosticRuleForm

pytestmark = pytest.mark.django_db
PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
PRIVATE = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443))]


@pytest.fixture
def dns(monkeypatch):
    answers = {"addresses": PUBLIC}

    def resolve(host, port):
        if host == "nowhere.example":
            raise socket.gaierror("no such host")
        return answers["addresses"]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return answers


def _responses(monkeypatch, *outcomes):
    calls = iter(outcomes)

    def get(url, **kwargs):
        assert kwargs["allow_redirects"] is False
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return Mock(status_code=outcome)

    monkeypatch.setattr(requests, "get", get)


def test_failure_percentage_over_tries(dns, monkeypatch):
    _responses(monkeypatch, 200, 503, requests.Timeout("slow"), 301)
    value, baseline, samples = diagnostics.http_check("https://api.square.example/health", 4)
    assert (value, baseline, samples) == (50.0, None, 4.0)


def test_tries_are_capped(dns, monkeypatch):
    _responses(monkeypatch, *([200] * 10))
    assert diagnostics.http_check("https://api.square.example/", 99)[2] == 10.0


@pytest.mark.parametrize("url, addresses, message", [
    ("http://api.square.example/", PUBLIC, "must use https"),
    ("https://internal.example/", PRIVATE, "internal addresses"),
    ("https://nowhere.example/", PUBLIC, "Could not resolve"),
])
def test_fenced_targets_are_refused(dns, url, addresses, message):
    dns["addresses"] = addresses
    with pytest.raises(diagnostics.DiagnosticError, match=message):
        diagnostics.http_check(url, 1)


def test_http_allowed_locally(dns, monkeypatch, settings):
    settings.DEBUG = True
    _responses(monkeypatch, 200)
    assert diagnostics.http_check("http://api.square.example/", 1)[0] == 0.0


def _rule(**kwargs):
    fields = {"name": "Square API", "provider": "http", "query": "https://api.square.example/health",
              "comparator": "above", "threshold": 0, "minimum_samples": 3, "lookback_minutes": 1}
    return DiagnosticRule.objects.create(**{**fields, **kwargs})


def test_runs_as_a_diagnostic_and_records_a_breach(dns, monkeypatch):
    row = assessments.receive(title="t", source="s", signal="x", customer="A", service="checkout")[0]
    _responses(monkeypatch, 200, 503, 200)
    run = diagnostics.execute(row, _rule())
    assert run.state == "done"
    assert run.result["breached"] is True
    assert round(run.result["value"], 1) == 33.3


def test_template_in_the_host_is_refused_at_run_time(dns):
    row = assessments.receive(title="t", source="s", signal="x", customer="evil.example/")[0]
    run = diagnostics.execute(row, _rule(query="https://${customer}/health"))
    assert run.state == "failed"
    assert "aren't allowed in an HTTP check's host" in run.error


def test_rule_form_validates_http_checks():
    base = {"name": "Square", "provider": "http", "minimum_samples": 3, "comparator": "above",
            "threshold": 0, "lookback_minutes": 1}
    assert DiagnosticRuleForm({**base, "query": "https://api.square.example/health"}).is_valid()
    bad = DiagnosticRuleForm({**base, "query": "https://${customer}.example/", "minimum_samples": 11,
                              "comparator": "deviation"})
    assert not bad.is_valid()
    assert {"query", "minimum_samples", "comparator"} <= set(bad.errors)
    assert "query" in DiagnosticRuleForm({**base, "query": "not a url"}).errors
