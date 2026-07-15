"""
Hermetic structural check of the escalation ASL (ADR-007): every transition target
exists and the routing matches the decision contract — each tier waits, a Choice
routes the outcome, and a commit task is the single writer. Complements the Step
Functions Local integration test (which validates execution against AWS's own engine).
"""
import json
from pathlib import Path

ASL_PATH = Path(__file__).resolve().parents[3] / "escalation" / "statemachine.asl.json"
ASL = json.loads(ASL_PATH.read_text())
STATES = ASL["States"]


def _targets(state: dict):
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


def test_resolve_routes_through_commit_to_succeed():
    for tier in ("T1", "T2", "T3"):
        choice = STATES[f"{tier}_Choice"]
        resolve = [c for c in choice["Choices"] if c.get("StringEquals") == "RESOLVE"]
        assert resolve and resolve[0]["Next"] == "ResolveCommit", f"{tier}_Choice RESOLVE rule"
    assert STATES["ResolveCommit"]["Next"] == "Resolved"
    assert STATES["Resolved"]["Type"] == "Succeed"


def test_escalate_advances_tiers_via_commit_and_top_tier_fails():
    # ESCALATE (Choice default) -> per-tier commit -> next tier's wait.
    assert STATES["T1_Choice"]["Default"] == "T1_EscalateCommit"
    assert STATES["T1_EscalateCommit"]["Next"] == "T2_Wait"
    assert STATES["T2_Choice"]["Default"] == "T2_EscalateCommit"
    assert STATES["T2_EscalateCommit"]["Next"] == "T3_Wait"
    # Nothing above T3: both its Choice default and SLA timeout end in the Fail state.
    assert STATES["T3_Choice"]["Default"] == "EscalationExhausted"
    assert STATES["T3_Wait"]["Catch"][0]["Next"] == "EscalationExhausted"
    assert STATES["EscalationExhausted"]["Type"] == "Fail"


def test_timeouts_auto_commit_below_top_tier():
    for tier in ("T1", "T2"):
        catch = STATES[f"{tier}_Wait"]["Catch"][0]
        assert catch["ErrorEquals"] == ["States.Timeout"]
        assert catch["Next"] == f"{tier}_AutoCommit"
    # Auto-commit uses the system actor.
    payload = STATES["T1_AutoCommit"]["Parameters"]["Payload"]
    assert payload["actor"] == "system:auto-escalation"
    assert payload["action"] == "ESCALATE"


def test_waits_preserve_input_with_resultpath():
    # ResultPath on each wait keeps $.incidentId alive for later tiers (the bug the
    # SFN Local test caught).
    for tier in ("T1", "T2", "T3"):
        assert STATES[f"{tier}_Wait"]["ResultPath"] == "$.decision"


def test_manual_commits_forward_the_human_reason():
    # The reason the human typed (ADR-041/042) rides the SendTaskSuccess output into $.decision,
    # but it only reaches the commit Lambda if the commit Payload forwards it. It used to not:
    # send_outcome sent the reason, the ASL dropped it, and every manual transition landed with
    # the "escalated" default — invisible in the UI and the RCA (found by driving staging).
    for name in ("T1_EscalateCommit", "T2_EscalateCommit", "ResolveCommit"):
        payload = STATES[name]["Parameters"]["Payload"]
        assert payload.get("reason.$") == "$.decision.reason", f"{name} must forward the reason"
        assert payload.get("actor.$") == "$.decision.actor", f"{name} must forward the actor"
