"""
Pluggable T1 triage-assistant seam (ADR-036), mirroring the AI-RCA seam (ADR-034).

The assistant receives the triage evidence (linked check + error spans, rendered to Markdown)
and returns ONLY a classification — (responsibility, fault_domain, verdict, confidence,
rationale). It never picks the action: disposition is the pure `triage.dispose()` in code.

  - `stub`    — deterministic, no network (default; hermetic tests + `make dev` without models):
                any HTTP 5xx in the evidence → real/internal/software, else a false positive.
  - `bedrock` — Claude on Amazon Bedrock, via the existing `bedrock.draft` client (ADR-033).
  - `conduct` — local models via the conduct project, via the existing `conduct.draft` client.

`SYSTEM_PROMPT` lives here (one watch-owned instruction, byte-identical to every backend —
same no-drift rule as `rca_ai.SYSTEM_PROMPT`). Model providers return free text; `_parse`
holds them to a strict JSON contract and raises `TriageError` on any deviation — the caller
soft-fails and the deterministic SLA engine remains the backstop.
"""
import json
import re
from dataclasses import dataclass

from django.conf import settings

from .models import FaultDomain, Responsibility, TriageVerdict


class TriageError(RuntimeError):
    """Any provider failure (unreachable / malformed / non-contract output). Callers degrade:
    the incident stays open and untriaged; the SLA clock keeps running (ADR-036)."""


@dataclass(frozen=True)
class TriageResult:
    """A classification plus provenance — the provider that ran and the model it actually used."""
    responsibility: str
    fault_domain: str
    verdict: str
    confidence: float
    rationale: str
    provider: str
    model: str


SYSTEM_PROMPT = (
    "You are a Tier 1 incident triage assistant. From the evidence provided (an incident opened "
    "from a failing session check, with the error spans found in the trace backend), classify the "
    "incident. Respond with ONLY a JSON object — no prose, no code fences — with exactly these "
    "keys: \"responsibility\" (one of: client, internal, vendor — whose problem it is), "
    "\"fault_domain\" (one of: environment, software — what kind of problem), \"verdict\" (one "
    "of: real, false_positive, undetermined), \"confidence\" (a number 0 to 1), and "
    "\"rationale\" (one or two factual sentences grounded in the evidence). Do NOT invent "
    "services, errors, or facts not present in the evidence; when the evidence is thin, use "
    "verdict undetermined with low confidence rather than guessing."
)


def _stub(evidence_markdown: str) -> TriageResult:
    """Deterministic local/test classification: server-side errors (HTTP 5xx) in the evidence
    read as a real internal software problem; anything else reads as a false positive."""
    if re.search(r"HTTP 5\d\d", evidence_markdown):
        return TriageResult(
            responsibility=Responsibility.INTERNAL,
            fault_domain=FaultDomain.SOFTWARE,
            verdict=TriageVerdict.REAL,
            confidence=0.7,
            rationale="Stub heuristic (TRIAGE_AI_PROVIDER=stub): evidence contains HTTP 5xx "
                      "error spans, consistent with a real server-side fault.",
            provider="stub",
            model="local-stub",
        )
    return TriageResult(
        responsibility=Responsibility.INTERNAL,
        fault_domain=FaultDomain.ENVIRONMENT,
        verdict=TriageVerdict.FALSE_POSITIVE,
        confidence=0.7,
        rationale="Stub heuristic (TRIAGE_AI_PROVIDER=stub): no server-side (HTTP 5xx) error "
                  "spans in the evidence.",
        provider="stub",
        model="local-stub",
    )


def _parse(text: str, provider: str, model: str) -> TriageResult:
    """Hold a model's free-text reply to the JSON contract. Tolerates code fences; everything
    else non-conforming is a TriageError."""
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    try:
        data = json.loads(cleaned)
    except ValueError as exc:
        raise TriageError(f"triage reply was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TriageError("triage reply was not a JSON object")

    responsibility = str(data.get("responsibility", "")).strip().lower()
    fault_domain = str(data.get("fault_domain", "")).strip().lower()
    verdict = str(data.get("verdict", "")).strip().lower()
    if responsibility not in Responsibility.values:
        raise TriageError(f"invalid responsibility {responsibility!r}")
    if fault_domain not in FaultDomain.values:
        raise TriageError(f"invalid fault_domain {fault_domain!r}")
    if verdict not in TriageVerdict.values:
        raise TriageError(f"invalid verdict {verdict!r}")
    try:
        confidence = min(1.0, max(0.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError) as exc:
        raise TriageError("invalid confidence") from exc

    return TriageResult(
        responsibility=responsibility,
        fault_domain=fault_domain,
        verdict=verdict,
        confidence=confidence,
        rationale=str(data.get("rationale", "")).strip(),
        provider=provider,
        model=model,
    )


def classify(evidence_markdown: str) -> TriageResult:
    """Classify triage evidence via the configured provider. Raises TriageError on any failure."""
    provider = (settings.TRIAGE_AI_PROVIDER or "stub").strip().lower()
    if provider == "stub":
        return _stub(evidence_markdown)
    if provider == "bedrock":
        from . import bedrock, rca_ai
        try:
            result = bedrock.draft(SYSTEM_PROMPT, evidence_markdown)
        except rca_ai.DraftError as exc:
            raise TriageError(str(exc)) from exc
        return _parse(result.text, provider="bedrock", model=result.model)
    if provider == "conduct":
        from . import conduct, rca_ai
        try:
            result = conduct.draft(SYSTEM_PROMPT, evidence_markdown)
        except rca_ai.DraftError as exc:
            raise TriageError(str(exc)) from exc
        return _parse(result.text, provider="conduct", model=result.model)
    raise TriageError(f"unknown TRIAGE_AI_PROVIDER {provider!r} (expected stub|bedrock|conduct)")
