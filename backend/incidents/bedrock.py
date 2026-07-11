"""
Amazon Bedrock client wrapper for the AI-drafted RCA (ADR-021/031, refined ADR-033).

The RCA assembly (`services.rca_markdown`) is the clean, reviewable input a human edits.
This module takes that same text and asks a Claude Sonnet model on Bedrock to draft the
narrative sections (summary / root cause / contributing factors / follow-ups) a human would
otherwise hand-write. It is only ever reached when the `rca_ai_draft` flag is on (ADR-003).

Why Bedrock and not the Anthropic API the original ADR sketched: the model call then stays
inside the account's IAM/VPC/CloudWatch boundary — same audit surface as the rest of the
runtime (ADR-004) — and needs no third-party API key in Secrets Manager. Deviation: ADR-033.

In BEDROCK_LOCAL_MODE (default on locally + in tests) the AWS call is skipped and a clearly
labelled deterministic stub is returned, so `make dev` and hermetic units exercise the whole
path without Bedrock credentials or in-account model access. Real calls go through boto3's
`bedrock-runtime` Converse API.
"""
import logging

import boto3
from django.conf import settings

logger = logging.getLogger(__name__)

# Raised when Bedrock is unreachable / access not granted / the response is malformed, so the
# view can surface a friendly message instead of a 500 (the human just re-drafts or hand-writes).
class DraftError(RuntimeError):
    pass


_SYSTEM_PROMPT = (
    "You are a senior site-reliability engineer writing a concise, factual root-cause analysis "
    "(RCA) from an incident's assembled timeline. Return GitHub-flavoured Markdown with these "
    "sections, in order: `## Summary`, `## Timeline` (tight, only the load-bearing events), "
    "`## Root cause`, `## Contributing factors`, `## Follow-ups` (concrete, actionable items). "
    "Ground every statement in the provided timeline — do NOT invent facts, causes, metrics, or "
    "names that are not present. Where the evidence is thin, say so explicitly rather than "
    "speculating. Keep a blameless, matter-of-fact tone."
)


def _client():
    return boto3.client("bedrock-runtime", region_name=settings.BEDROCK_REGION)


def _stub(source_markdown: str) -> str:
    """Deterministic local/test draft — clearly marked so it's never mistaken for a real one."""
    return (
        "## Summary\n\n"
        "_AI draft generated locally (BEDROCK_LOCAL_MODE) — enable Bedrock in the deployed "
        "environment for a real model draft._\n\n"
        "## Timeline\n\n"
        "See the assembled timeline below.\n\n"
        "## Root cause\n\n_(to be completed)_\n\n"
        "## Contributing factors\n\n_(to be completed)_\n\n"
        "## Follow-ups\n\n_(to be completed)_\n\n"
        "---\n\n"
        "### Source assembly\n\n" + source_markdown
    )


def draft_rca(source_markdown: str) -> str:
    """Draft an RCA narrative from the assembled timeline Markdown. Returns Markdown.

    Raises DraftError on any Bedrock failure (access/throttle/malformed) so callers can degrade
    gracefully. In BEDROCK_LOCAL_MODE, returns the deterministic stub without touching AWS.
    """
    if settings.BEDROCK_LOCAL_MODE or not settings.BEDROCK_MODEL_ID:
        logger.info("bedrock.draft_rca (local stub) model=%s", settings.BEDROCK_MODEL_ID)
        return _stub(source_markdown)

    try:
        resp = _client().converse(
            modelId=settings.BEDROCK_MODEL_ID,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[{
                "role": "user",
                "content": [{"text": (
                    "Draft the RCA from this assembled incident timeline:\n\n" + source_markdown
                )}],
            }],
            inferenceConfig={"maxTokens": settings.BEDROCK_MAX_TOKENS, "temperature": 0.2},
        )
        text = resp["output"]["message"]["content"][0]["text"].strip()
    except Exception as exc:  # boto ClientError, KeyError on a malformed body, etc.
        logger.exception("bedrock.draft_rca failed model=%s", settings.BEDROCK_MODEL_ID)
        raise DraftError(str(exc)) from exc

    if not text:
        raise DraftError("Bedrock returned an empty draft")
    return text
