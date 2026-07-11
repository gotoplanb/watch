"""Triage-assistant seam tests (ADR-036): the stub heuristic, the strict JSON contract every
model provider is held to, and provider dispatch (bedrock/conduct faked — no network)."""
import pytest

from incidents import rca_ai, triage_ai
from incidents.models import FaultDomain, Responsibility, TriageVerdict
from incidents.rca_ai import DraftResult


# --- stub provider ---

def test_stub_reads_5xx_as_real_internal_software(settings):
    settings.TRIAGE_AI_PROVIDER = "stub"
    result = triage_ai.classify("- POST /orders (service: api, ERROR, HTTP 503, trace abc)")
    assert result.verdict == TriageVerdict.REAL
    assert result.responsibility == Responsibility.INTERNAL
    assert result.fault_domain == FaultDomain.SOFTWARE
    assert result.provider == "stub" and 0 < result.confidence <= 1


def test_stub_reads_no_5xx_as_false_positive(settings):
    settings.TRIAGE_AI_PROVIDER = "stub"
    result = triage_ai.classify("- GET /health (service: api, ERROR, HTTP 404, trace abc)")
    assert result.verdict == TriageVerdict.FALSE_POSITIVE
    assert result.fault_domain == FaultDomain.ENVIRONMENT


# --- the JSON contract (_parse) ---

GOOD = '{"responsibility": "vendor", "fault_domain": "environment", "verdict": "real", "confidence": 0.8, "rationale": "Upstream DNS errors."}'


def test_parse_valid_json():
    result = triage_ai._parse(GOOD, provider="conduct", model="gemma4:e4b")
    assert result.responsibility == Responsibility.VENDOR
    assert result.verdict == TriageVerdict.REAL
    assert result.confidence == 0.8
    assert result.model == "gemma4:e4b"


def test_parse_tolerates_code_fences_and_case():
    fenced = '```json\n{"responsibility": "Client", "fault_domain": "SOFTWARE", "verdict": "undetermined", "confidence": 2, "rationale": ""}\n```'
    result = triage_ai._parse(fenced, provider="bedrock", model="sonnet")
    assert result.responsibility == Responsibility.CLIENT
    assert result.verdict == TriageVerdict.UNDETERMINED
    assert result.confidence == 1.0  # clamped to [0, 1]


@pytest.mark.parametrize("text", [
    "not json at all",
    "[1, 2, 3]",  # not an object
    '{"responsibility": "us", "fault_domain": "software", "verdict": "real", "confidence": 0.5}',
    '{"responsibility": "client", "fault_domain": "hardware", "verdict": "real", "confidence": 0.5}',
    '{"responsibility": "client", "fault_domain": "software", "verdict": "maybe", "confidence": 0.5}',
    '{"responsibility": "client", "fault_domain": "software", "verdict": "real", "confidence": "high"}',
])
def test_parse_rejects_non_contract_output(text):
    with pytest.raises(triage_ai.TriageError):
        triage_ai._parse(text, provider="conduct", model="m")


# --- provider dispatch ---

def test_classify_dispatches_to_bedrock(settings, monkeypatch):
    settings.TRIAGE_AI_PROVIDER = "bedrock"
    from incidents import bedrock
    monkeypatch.setattr(
        bedrock, "draft",
        lambda prompt, source: DraftResult(text=GOOD, provider="bedrock", model="sonnet-4-6"),
    )
    result = triage_ai.classify("evidence")
    assert result.provider == "bedrock" and result.model == "sonnet-4-6"


def test_classify_dispatches_to_conduct(settings, monkeypatch):
    settings.TRIAGE_AI_PROVIDER = "conduct"
    from incidents import conduct
    monkeypatch.setattr(
        conduct, "draft",
        lambda prompt, source: DraftResult(text=GOOD, provider="conduct", model="gemma4:e4b"),
    )
    result = triage_ai.classify("evidence")
    assert result.provider == "conduct" and result.model == "gemma4:e4b"


@pytest.mark.parametrize("provider,module_name", [("conduct", "conduct"), ("bedrock", "bedrock")])
def test_classify_wraps_provider_drafterror(settings, monkeypatch, provider, module_name):
    settings.TRIAGE_AI_PROVIDER = provider
    import importlib
    module = importlib.import_module(f"incidents.{module_name}")

    def boom(prompt, source):
        raise rca_ai.DraftError(f"{provider} unreachable")

    monkeypatch.setattr(module, "draft", boom)
    with pytest.raises(triage_ai.TriageError, match="unreachable"):
        triage_ai.classify("evidence")


def test_classify_unknown_provider_raises(settings):
    settings.TRIAGE_AI_PROVIDER = "gpt"
    with pytest.raises(triage_ai.TriageError, match="unknown TRIAGE_AI_PROVIDER"):
        triage_ai.classify("evidence")
