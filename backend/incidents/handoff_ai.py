"""
Tier handoff briefs (ADR-040) — the assistant writes the incoming responder's first read.

On every real tier entry via escalation, a brief lands as the NEWEST timeline event: what has
happened, why THIS tier is engaged, what's needed now — the operations manual's tier roles
framing the ask. Same provider seam discipline as rca_ai/triage_ai (ADR-034/036):
`stub | bedrock | conduct`, one watch-owned prompt, uniform soft-fail error. The stub is a
genuinely useful deterministic brief assembled from real incident data (never filler), so the
feature works end-to-end with zero model calls.

Free-text output (unlike triage_ai there is no JSON contract to hold a model to — the brief IS
prose); provenance records which provider/model wrote it.
"""
import re
from dataclasses import dataclass

from django.conf import settings


class HandoffError(RuntimeError):
    """Any provider failure. Callers soft-fail: an escalation must never wait on, or be blocked
    by, its brief."""


@dataclass(frozen=True)
class BriefResult:
    text: str
    provider: str
    model: str


# The operations manual's tier definitions ("Highway Mode, Race Mode") — they frame the
# "what's needed" section for the incoming responder.
TIER_ROLES = {
    "T1": "first responder (NOC): triage — is this safe to keep driving or does it need to "
          "pull over now; route, don't diagnose",
    "T2": "diagnostic technician (SRE): own the incident window end to end — read the codes, "
          "identify the affected system and blast radius, own any customer bridge, and make "
          "the SME escalation decision deliberately",
    "T3": "shop foreman (ops manager): strategic decisions, cross-system judgment, customer-"
          "relationship stakes — engaged because the situation is genuinely beyond T2",
}

SYSTEM_PROMPT = (
    "You are the tier-handoff assistant for an incident-response team. An incident has just been "
    "escalated, and the incoming responder is opening it for the first time. Write their briefing "
    "from the incident history provided. Use EXACTLY three short sections with these plain-text "
    "headers: 'WHAT HAS HAPPENED' (a compressed factual recap of the timeline — key events only), "
    "'WHY {tier} IS ENGAGED' (was this an SLA-clock auto-escalation or a deliberate human "
    "escalation, by whom, with what stated reason), and 'WHAT {tier} SHOULD DO NOW' (concrete "
    "next actions grounded in the current state and this tier's role: {role}). Be terse and "
    "factual; never invent events, systems, or people not present in the history; if the history "
    "is thin, say so rather than padding."
)


def _context_header(ctx: dict) -> str:
    """The escalation facts, rendered for the model alongside the timeline history."""
    how = "automatic — SLA clock expired" if ctx["auto"] else f"deliberate — by {ctx['actor_label']}"
    lines = [f"Escalated {ctx['from_tier']} -> {ctx['to_tier']} ({how})"]
    if ctx.get("reason"):
        lines.append(f"Stated reason: {ctx['reason']}")
    if ctx.get("triage"):
        lines.append(f"T1 triage classification: {ctx['triage']}")
    lines.append(f"Open for: {ctx['open_for']} · acknowledged at {ctx['to_tier']}: not yet")
    return "\n".join(lines)


def _stub(ctx: dict) -> BriefResult:
    """Deterministic brief from real data — the no-model default that still does the job."""
    tier = ctx["to_tier"]
    if ctx["auto"]:
        why = (f"The {ctx['from_tier']} SLA clock expired with the incident unresolved — the "
               f"escalation contract engaged {tier} automatically.")
    else:
        why = f"{ctx['actor_label']} deliberately escalated from {ctx['from_tier']}"
        why += f" — “{ctx['reason']}”." if ctx.get("reason") else "."
    happened = [f"Incident open for {ctx['open_for']} (source: {ctx['source']})."]
    if ctx.get("triage"):
        happened.append(f"T1 triage classified it {ctx['triage']}.")
    happened.append(f"{ctx['event_count']} timeline event(s) so far — review below.")
    if tier == "T3":
        closing = ("resolve, or drive the strategic path — engage SMEs/vendor deliberately, own "
                   "the customer-relationship call. There is no higher tier; the buck stops here.")
    else:
        closing = (f"resolve if within reach, or escalate with a stated reason if this is "
                   f"genuinely beyond {tier}.")
    text = (
        f"WHAT HAS HAPPENED\n{' '.join(happened)}\n\n"
        f"WHY {tier} IS ENGAGED\n{why}\n\n"
        f"WHAT {tier} SHOULD DO NOW\n"
        f"Your role: {TIER_ROLES.get(tier, 'responder')}. Acknowledge to signal you're engaged "
        f"(the SLA clock keeps running), read the timeline below, then act: {closing}"
    )
    return BriefResult(text=text, provider="stub", model="local-stub")


def _plain(text: str) -> str:
    """Models reach for markdown even when told not to; the timeline renders plain text, so
    literal ** / ## would show up as asterisks. Strip the emphasis markers, keep the words."""
    return re.sub(r"\*\*|__|^#{1,6}\s*", "", text, flags=re.M).strip()


def brief(ctx: dict, history_markdown: str) -> BriefResult:
    """Write the handoff brief via the configured provider. Raises HandoffError on failure."""
    provider = (settings.HANDOFF_AI_PROVIDER or "stub").strip().lower()
    if provider == "stub":
        return _stub(ctx)
    prompt = SYSTEM_PROMPT.replace("{tier}", ctx["to_tier"]).replace(
        "{role}", TIER_ROLES.get(ctx["to_tier"], "responder")
    )
    source = f"{_context_header(ctx)}\n\n--- INCIDENT HISTORY ---\n{history_markdown}"
    if provider in ("bedrock", "conduct"):
        from . import rca_ai
        backend = __import__(f"incidents.{provider}", fromlist=["draft"])
        try:
            result = backend.draft(prompt, source)
        except rca_ai.DraftError as exc:
            raise HandoffError(str(exc)) from exc
        return BriefResult(text=_plain(result.text), provider=provider, model=result.model)
    raise HandoffError(f"unknown HANDOFF_AI_PROVIDER {provider!r} (expected stub|bedrock|conduct)")
