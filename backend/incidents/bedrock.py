"""
Amazon Bedrock provider for the AI-drafted RCA (ADR-033, one backend of the ADR-034 seam).

Asks a Claude Sonnet model on Bedrock to draft the RCA narrative from the assembled timeline.
This is the deployed/cloud backend: the model call stays inside the account's IAM/VPC/CloudWatch
boundary (ADR-004) and needs no third-party API key — deviation from the ADR-021/031 "Anthropic
API" sketch, recorded in ADR-033. The instruction prompt is owned by the seam (`rca_ai.SYSTEM_PROMPT`)
and passed in, so it's byte-identical to the Conduct path. Selected when `RCA_AI_PROVIDER=bedrock`
(the local `stub` provider is what runs without AWS / model access). Real calls go through boto3's
`bedrock-runtime` Converse API.
"""
import logging

import boto3
from django.conf import settings

from .rca_ai import DraftError, DraftResult

logger = logging.getLogger(__name__)


def _client():
    return boto3.client("bedrock-runtime", region_name=settings.BEDROCK_REGION)


def draft(system_prompt: str, source_markdown: str) -> DraftResult:
    """Draft via Bedrock. Raises DraftError on any failure (access/throttle/malformed/empty)."""
    if not settings.BEDROCK_MODEL_ID:
        raise DraftError("BEDROCK_MODEL_ID is not set")

    try:
        resp = _client().converse(
            modelId=settings.BEDROCK_MODEL_ID,
            system=[{"text": system_prompt}],
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
        logger.exception("bedrock.draft failed model=%s", settings.BEDROCK_MODEL_ID)
        raise DraftError(str(exc)) from exc

    if not text:
        raise DraftError("Bedrock returned an empty draft")
    return DraftResult(text=text, provider="bedrock", model=settings.BEDROCK_MODEL_ID)
