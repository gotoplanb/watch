"""
Hermetic structural check of the escalation ASL (ADR-007): every transition target
exists and the routing matches the decision contract. Catches malformed graphs the
service-layer unit tests can't see; complements the Step Functions Local integration
test (which validates execution against AWS's own engine).
"""
import json
from pathlib import Path

ASL_PATH = Path(__file__).resolve().parents[3] / "escalation" / "statemachine.asl.json"
ASL = json.loads(ASL_PATH.read_text())
STATES = ASL["States"]


def _targets(state: dict):
    """All state names this state can transition to."""
    out = []
    if "Next" in state:
        out.append(state["Next"])
    if "Default" in state:
        out.append(state["Default"])
    for choice in state.get("Choices", []):
        out.append(choice["Next"])
    for catch in state.get("Catch", []):
        out.append(catch["Next"])
    return out


def test_all_transition_targets_exist():
    for name, state in STATES.items():
        for target in _targets(state):
            assert target in STATES, f"{name} -> missing state {target}"


def test_resolve_routes_to_succeed_at_every_tier():
    for tier in ("T1", "T2", "T3"):
        decision = STATES[f"{tier}_Decision"]
        resolve_rules = [c for c in decision["Choices"] if c.get("StringEquals") == "RESOLVE"]
        assert resolve_rules, f"{tier}_Decision has no RESOLVE rule"
        assert resolve_rules[0]["Next"] == "Resolved"
    assert STATES["Resolved"]["Type"] == "Succeed"


def test_escalate_advances_tiers_and_top_tier_fails():
    # Default (ESCALATE) advances T1->T2->T3.
    assert STATES["T1_Decision"]["Default"] == "T2_Wait"
    assert STATES["T2_Decision"]["Default"] == "T3_Wait"
    # No tier above T3 — both its Choice default and SLA timeout end in the Fail state.
    assert STATES["T3_Decision"]["Default"] == "EscalationExhausted"
    assert STATES["T3_Wait"]["Catch"][0]["Next"] == "EscalationExhausted"
    assert STATES["EscalationExhausted"]["Type"] == "Fail"


def test_timeouts_auto_escalate_below_top_tier():
    assert STATES["T1_Wait"]["Catch"][0]["ErrorEquals"] == ["States.Timeout"]
    assert STATES["T1_Wait"]["Catch"][0]["Next"] == "T1_AutoEscalate"
    assert STATES["T2_Wait"]["Catch"][0]["Next"] == "T2_AutoEscalate"
