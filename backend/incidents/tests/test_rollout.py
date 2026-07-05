"""Rollout-mode seam (ADR-014): on/off/sample, deterministic sampling, is_enabled wrapper."""
import pytest

from incidents import flags


@pytest.fixture(autouse=True)
def _reset():
    yield
    flags.set_provider_for_tests(None)


def _mem(values):
    flags.set_provider_for_tests(flags.InMemoryProvider(values))


def test_is_enabled_on_off_bool_and_default():
    _mem({"x": "on", "y": "off", "z": True})
    assert flags.is_enabled("x") and flags.is_enabled("z")
    assert not flags.is_enabled("y")
    assert not flags.is_enabled("missing", False)
    assert flags.is_enabled("missing2", True)


def test_active_on_off_and_bool():
    _mem({"a": "on", "b": "off", "c": True, "d": False})
    assert flags.active("a") and flags.active("c")
    assert not flags.active("b") and not flags.active("d")
    assert not flags.active("missing")  # default off


def test_active_sample_is_deterministic_per_key():
    _mem({"s": "sample:0.5"})
    assert flags.active("s", key="incident-1") == flags.active("s", key="incident-1")
    # ~half of many keys are in, and it's stable
    ins = sum(flags.active("s", key=f"k{i}") for i in range(200))
    assert 60 < ins < 140


def test_active_sample_bounds_and_unparseable():
    _mem({"none": "sample:0", "all": "sample:1", "bad": "sample:nope"})
    assert not flags.active("none", key="k")
    assert flags.active("all", key="k")
    assert not flags.active("bad", key="k")  # unparseable rate -> off


def test_active_sample_without_key_is_random_but_bounded():
    _mem({"all": "sample:1", "none": "sample:0", "half": "sample:0.5"})
    assert flags.active("all") and not flags.active("none")
    assert isinstance(flags.active("half"), bool)  # no key -> fresh random fraction, no error


def test_is_enabled_treats_sample_as_off():
    _mem({"s": "sample:1"})
    assert not flags.is_enabled("s")  # on/off wrapper ignores sampling
    assert flags.active("s", key="k")  # but the rollout form honors it
