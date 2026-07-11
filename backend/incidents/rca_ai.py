"""
Pluggable AI-RCA provider seam (ADR-033 → ADR-034, watch#41).

The RCA assembly (`services.rca_markdown`) is the clean, reviewable input a human edits. The
AI draft consumes that same text and writes the narrative sections. This module is the ONE
place that decides *which backend* runs and hands it a single, shared instruction prompt — so
the instruction is byte-identical no matter who drafts:

  - `stub`    — a deterministic, clearly-marked local draft (no network); default, and what
                hermetic tests use.
  - `bedrock` — Claude Sonnet on Amazon Bedrock (ADR-033); the deployed/cloud backend, inside
                the account IAM/VPC/CloudWatch boundary.
  - `conduct` — local models via the `conduct` project (watch#41); a synchronous HTTP call, the
                local-dev backend, no AWS or model-access grant needed.

`SYSTEM_PROMPT` lives here (not in a provider) precisely so both backends receive the identical
string with no drift — Bedrock puts it in the Converse `system` block, Conduct in `system_prompt`.
Provider modules are imported lazily inside `draft()` to keep this module free of a boto3/requests
import cost (and of import cycles — the providers import `DraftError`/`DraftResult` from here).
"""
import logging
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)


class DraftError(RuntimeError):
    """Any provider failure (unreachable / not-authorized / malformed / empty). Callers degrade
    gracefully — the view flashes a message and leaves the existing document untouched."""


@dataclass(frozen=True)
class DraftResult:
    """A finished draft plus the provenance the timeline event records — the provider that ran
    and the model it actually used (Conduct/Bedrock report the concrete model back)."""
    text: str
    provider: str
    model: str


# Single source of truth for the RCA instruction — handed identically to every backend.
SYSTEM_PROMPT = (
    "You are a senior site-reliability engineer writing a concise, factual root-cause analysis "
    "(RCA) from an incident's assembled timeline. Return GitHub-flavoured Markdown with these "
    "sections, in order: `## Summary`, `## Timeline` (tight, only the load-bearing events), "
    "`## Root cause`, `## Contributing factors`, `## Follow-ups` (concrete, actionable items). "
    "Ground every statement in the provided timeline — do NOT invent facts, causes, metrics, or "
    "names that are not present. Where the evidence is thin, say so explicitly rather than "
    "speculating. Keep a blameless, matter-of-fact tone."
)


def _stub(source_markdown: str) -> DraftResult:
    """Deterministic local/test draft — clearly marked so it's never mistaken for a real one."""
    text = (
        "## Summary\n\n"
        "_AI draft generated locally (RCA_AI_PROVIDER=stub) — point RCA_AI_PROVIDER at `conduct` "
        "(local models) or `bedrock` for a real model draft._\n\n"
        "## Timeline\n\n"
        "See the assembled timeline below.\n\n"
        "## Root cause\n\n_(to be completed)_\n\n"
        "## Contributing factors\n\n_(to be completed)_\n\n"
        "## Follow-ups\n\n_(to be completed)_\n\n"
        "---\n\n"
        "### Source assembly\n\n" + source_markdown
    )
    return DraftResult(text=text, provider="stub", model="local-stub")


def draft(source_markdown: str) -> DraftResult:
    """Draft an RCA from the assembled timeline via the configured provider. Raises DraftError on
    any failure so callers can degrade to the existing document."""
    provider = (settings.RCA_AI_PROVIDER or "stub").strip().lower()
    if provider == "stub":
        return _stub(source_markdown)
    if provider == "bedrock":
        from . import bedrock
        return bedrock.draft(SYSTEM_PROMPT, source_markdown)
    if provider == "conduct":
        from . import conduct
        return conduct.draft(SYSTEM_PROMPT, source_markdown)
    raise DraftError(f"unknown RCA_AI_PROVIDER {provider!r} (expected stub|bedrock|conduct)")
