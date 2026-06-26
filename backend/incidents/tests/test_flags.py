"""ADR-003: feature-flag seam — both branches of every flag (spec §6)."""
from incidents import flags


def test_both_branches_via_in_memory_provider():
    flags.set_provider_for_tests(flags.InMemoryProvider({"new_triage_ui": True}))
    assert flags.is_enabled("new_triage_ui", default=False) is True   # ON branch
    assert flags.is_enabled("unset_flag", default=False) is False     # OFF branch (default)
    flags.set_provider_for_tests(flags.InMemoryProvider({"new_triage_ui": False}))
    assert flags.is_enabled("new_triage_ui", default=True) is False   # explicit OFF beats default
