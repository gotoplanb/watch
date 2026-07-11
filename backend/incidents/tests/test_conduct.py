"""Unit tests for the Conduct RCA provider (watch#41, ADR-034), requests mocked — covers the
happy path (sync draft + model_used provenance), config guard, and every soft-fail branch."""
from unittest import mock

import pytest
import requests

from incidents import conduct
from incidents.rca_ai import DraftError

PROMPT = "SYSTEM PROMPT"


def _cfg(settings):
    settings.CONDUCT_BASE_URL = "http://conduct.test:8000"
    settings.CONDUCT_API_KEY = "cdt_test"
    settings.CONDUCT_TASK_TYPE = "tier_2"
    settings.CONDUCT_TIMEOUT = 5


def _resp(status_code=200, json_body=None, text=""):
    r = mock.Mock(status_code=status_code, text=text)
    if json_body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = json_body
    return r


def test_draft_success_returns_result_with_model_used(settings):
    _cfg(settings)
    body = {"status": "complete", "model_used": "gemma4:e4b", "response": "## Summary\n\ndraft"}
    with mock.patch.object(conduct.requests, "post", return_value=_resp(json_body=body)) as post:
        result = conduct.draft(PROMPT, "# assembly")
    assert result.text == "## Summary\n\ndraft"
    assert result.provider == "conduct" and result.model == "gemma4:e4b"
    kwargs = post.call_args.kwargs
    assert kwargs["json"] == {"task_type": "tier_2", "system_prompt": PROMPT, "prompt": "# assembly"}
    assert kwargs["headers"]["Authorization"] == "Bearer cdt_test"
    assert kwargs["timeout"] == 5


def test_draft_unconfigured_raises(settings):
    _cfg(settings)
    settings.CONDUCT_API_KEY = ""
    with pytest.raises(DraftError):
        conduct.draft(PROMPT, "src")


def test_draft_transport_error_raises(settings):
    _cfg(settings)
    with mock.patch.object(conduct.requests, "post", side_effect=requests.ConnectionError("down")):
        with pytest.raises(DraftError):
            conduct.draft(PROMPT, "src")


def test_draft_http_error_raises(settings):
    _cfg(settings)
    with mock.patch.object(conduct.requests, "post", return_value=_resp(status_code=500, text="boom")):
        with pytest.raises(DraftError):
            conduct.draft(PROMPT, "src")


def test_draft_non_json_raises(settings):
    _cfg(settings)
    with mock.patch.object(conduct.requests, "post", return_value=_resp(json_body=None)):
        with pytest.raises(DraftError):
            conduct.draft(PROMPT, "src")


def test_draft_incomplete_status_raises(settings):
    _cfg(settings)
    body = {"status": "failed", "error": "model oom", "response": ""}
    with mock.patch.object(conduct.requests, "post", return_value=_resp(json_body=body)):
        with pytest.raises(DraftError):
            conduct.draft(PROMPT, "src")


def test_draft_empty_response_raises(settings):
    _cfg(settings)
    body = {"status": "complete", "model_used": "gemma4:e4b", "response": "   "}
    with mock.patch.object(conduct.requests, "post", return_value=_resp(json_body=body)):
        with pytest.raises(DraftError):
            conduct.draft(PROMPT, "src")
