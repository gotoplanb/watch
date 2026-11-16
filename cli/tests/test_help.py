"""wt's help is how a newcomer (or their AI assistant) learns Watch (#63). These tests keep it
complete and honest: every command explains itself with examples, every guide topic is reachable,
and every ADR or file the help cites actually exists in the repo."""
import argparse
import re
from importlib import resources
from pathlib import Path

import pytest

from wt import actions, cli, parser

REPO = Path(__file__).resolve().parents[2]
ADRS = set(re.findall(r"^## (ADR-\d{3})", (REPO / "watch-adrs.md").read_text(), re.M))


def _subcommands():
    sub = next(a for a in parser.build()._actions if isinstance(a, argparse._SubParsersAction))
    return sub.choices


def _guide_text():
    return {p.stem: p.read_text() for p in resources.files("wt.guide").iterdir()
            if p.name.endswith(".md")}


def _all_help_text():
    texts = [cmd.format_help() for cmd in _subcommands().values()]
    return "\n".join([parser.build().format_help(), *texts, *_guide_text().values()])


@pytest.mark.parametrize("name", sorted(_subcommands()))
def test_every_command_explains_itself_with_examples(name):
    cmd = _subcommands()[name]
    summary, _, explanation = (cmd.description or "").partition("\n\n")
    assert summary.strip(), f"{name}: no summary"
    assert len(explanation.split()) >= 8, f"{name}: the description must explain, not just name"
    assert cmd.epilog and cmd.epilog.startswith("examples:\n  wt "), f"{name}: no examples"


def test_top_level_help_points_newcomers_at_the_guide():
    text = parser.build().format_help()
    assert "wt guide" in text
    assert "wt guide tour" in text


def test_every_guide_topic_is_listed_in_the_overview_and_vice_versa():
    topics = _guide_text()
    listed = set(re.findall(r"wt guide (\w+)", topics["overview"]))
    assert listed == set(topics) - {"overview"}


def test_guide_command_prints_topics_and_rejects_unknown(capsys):
    assert cli.main(["guide", "lifecycle"]) == 0
    assert "Acknowledging is not progress" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="topics: assessments"):
        cli.main(["guide", "nope"])


def test_cited_adrs_and_files_exist():
    text = _all_help_text()
    cited = set(re.findall(r"ADR-\d{3}", text))
    assert cited, "the help should cite its decisions"
    assert cited <= ADRS, f"unknown ADRs cited: {sorted(cited - ADRS)}"
    for path in set(re.findall(r"`?((?:docs/)?[\w-]+\.md)`?", text)):
        if path.startswith(("docs/", "watch-")):
            assert (REPO / path).exists(), f"cited file missing: {path}"


def test_do_reference_covers_every_action():
    text = _subcommands()["do"].format_help()
    for name in actions.ACTIONS:
        assert f"  {name}: " in text
