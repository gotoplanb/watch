"""Unit tests for the AI-RCA provider seam (ADR-034): provider dispatch + the deterministic
stub + the single shared prompt. Provider backends are mocked; their own units live in
test_bedrock.py / test_conduct.py."""
from unittest import mock

import pytest

from incidents import rca_ai
from incidents.rca_ai import DraftError, DraftResult


def test_stub_provider_is_deterministic_and_marked(settings):
    settings.RCA_AI_PROVIDER = "stub"
    r = rca_ai.draft("# assembly\n\n- boom")
    assert r.provider == "stub" and r.model == "local-stub"
    assert "RCA_AI_PROVIDER=stub" in r.text  # clearly labelled
    assert "## Root cause" in r.text and "boom" in r.text  # source carried through


def test_dispatch_to_bedrock_passes_shared_prompt(settings):
    settings.RCA_AI_PROVIDER = "bedrock"
    sentinel = DraftResult(text="x", provider="bedrock", model="m")
    with mock.patch("incidents.bedrock.draft", return_value=sentinel) as d:
        assert rca_ai.draft("SRC") is sentinel
    d.assert_called_once_with(rca_ai.SYSTEM_PROMPT, "SRC")


def test_dispatch_to_conduct_passes_shared_prompt(settings):
    settings.RCA_AI_PROVIDER = "conduct"
    sentinel = DraftResult(text="x", provider="conduct", model="gemma4:e4b")
    with mock.patch("incidents.conduct.draft", return_value=sentinel) as d:
        assert rca_ai.draft("SRC") is sentinel
    d.assert_called_once_with(rca_ai.SYSTEM_PROMPT, "SRC")


def test_provider_is_case_insensitive_and_trimmed(settings):
    settings.RCA_AI_PROVIDER = "  STUB  "
    assert rca_ai.draft("s").provider == "stub"


def test_unknown_provider_raises(settings):
    settings.RCA_AI_PROVIDER = "nope"
    with pytest.raises(DraftError):
        rca_ai.draft("s")
