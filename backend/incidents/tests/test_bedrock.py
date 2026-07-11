"""Unit tests for the Bedrock RCA-draft client wrapper (ADR-021/031/033), boto3 mocked —
covers the local stub, the real Converse call, and the DraftError failure paths."""
from unittest import mock

import pytest

from incidents import bedrock


def test_draft_local_mode_returns_marked_stub(settings):
    settings.BEDROCK_LOCAL_MODE = True
    out = bedrock.draft_rca("# RCA — thing\n\n## Timeline\n- boom")
    assert "BEDROCK_LOCAL_MODE" in out  # clearly labelled, not mistaken for a real draft
    assert "## Root cause" in out
    assert "boom" in out  # the source assembly is carried through


def test_draft_empty_model_id_falls_back_to_stub(settings):
    settings.BEDROCK_LOCAL_MODE = False
    settings.BEDROCK_MODEL_ID = ""
    out = bedrock.draft_rca("src")
    assert "BEDROCK_LOCAL_MODE" in out


def test_draft_real_mode_calls_converse(settings):
    settings.BEDROCK_LOCAL_MODE = False
    settings.BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    fake = mock.Mock()
    fake.converse.return_value = {
        "output": {"message": {"content": [{"text": "## Summary\n\nreal draft"}]}}
    }
    with mock.patch.object(bedrock.boto3, "client", return_value=fake):
        out = bedrock.draft_rca("# assembly")
    assert out == "## Summary\n\nreal draft"
    kwargs = fake.converse.call_args.kwargs
    assert kwargs["modelId"] == "us.anthropic.claude-sonnet-4-20250514-v1:0"
    assert "# assembly" in kwargs["messages"][0]["content"][0]["text"]
    assert kwargs["inferenceConfig"]["maxTokens"] == settings.BEDROCK_MAX_TOKENS


def test_draft_real_mode_wraps_client_error(settings):
    settings.BEDROCK_LOCAL_MODE = False
    settings.BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    fake = mock.Mock()
    fake.converse.side_effect = RuntimeError("AccessDeniedException")
    with mock.patch.object(bedrock.boto3, "client", return_value=fake):
        with pytest.raises(bedrock.DraftError):
            bedrock.draft_rca("src")


def test_draft_real_mode_rejects_empty_completion(settings):
    settings.BEDROCK_LOCAL_MODE = False
    settings.BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    fake = mock.Mock()
    fake.converse.return_value = {"output": {"message": {"content": [{"text": "   "}]}}}
    with mock.patch.object(bedrock.boto3, "client", return_value=fake):
        with pytest.raises(bedrock.DraftError):
            bedrock.draft_rca("src")
