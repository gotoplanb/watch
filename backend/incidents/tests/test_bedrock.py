"""Unit tests for the Bedrock RCA provider (ADR-033/034), boto3 mocked — covers the real
Converse call, provenance, and the DraftError failure paths. The prompt is passed in by the
seam (rca_ai), so these assert it flows through to the Converse `system` block."""
from unittest import mock

import pytest

from incidents import bedrock
from incidents.rca_ai import DraftError

PROMPT = "SYSTEM PROMPT"


def test_draft_calls_converse_and_returns_result(settings):
    settings.BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    fake = mock.Mock()
    fake.converse.return_value = {
        "output": {"message": {"content": [{"text": "## Summary\n\nreal draft"}]}}
    }
    with mock.patch.object(bedrock.boto3, "client", return_value=fake):
        result = bedrock.draft(PROMPT, "# assembly")
    assert result.text == "## Summary\n\nreal draft"
    assert result.provider == "bedrock"
    assert result.model == "us.anthropic.claude-sonnet-4-20250514-v1:0"
    kwargs = fake.converse.call_args.kwargs
    assert kwargs["modelId"] == settings.BEDROCK_MODEL_ID
    assert kwargs["system"] == [{"text": PROMPT}]  # seam's prompt, byte-identical
    assert "# assembly" in kwargs["messages"][0]["content"][0]["text"]
    assert kwargs["inferenceConfig"]["maxTokens"] == settings.BEDROCK_MAX_TOKENS


def test_draft_without_model_id_raises(settings):
    settings.BEDROCK_MODEL_ID = ""
    with pytest.raises(DraftError):
        bedrock.draft(PROMPT, "src")


def test_draft_wraps_client_error(settings):
    settings.BEDROCK_MODEL_ID = "m"
    fake = mock.Mock()
    fake.converse.side_effect = RuntimeError("AccessDeniedException")
    with mock.patch.object(bedrock.boto3, "client", return_value=fake):
        with pytest.raises(DraftError):
            bedrock.draft(PROMPT, "src")


def test_draft_rejects_empty_completion(settings):
    settings.BEDROCK_MODEL_ID = "m"
    fake = mock.Mock()
    fake.converse.return_value = {"output": {"message": {"content": [{"text": "   "}]}}}
    with mock.patch.object(bedrock.boto3, "client", return_value=fake):
        with pytest.raises(DraftError):
            bedrock.draft(PROMPT, "src")
