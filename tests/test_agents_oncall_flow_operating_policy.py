"""The campaign's standing rules are printed on every status call.

A wake turn is a cold start with no history, so a rule that lives only in the
task text is absent at exactly the moments the decisions get made. Measured on
the CFD line: nine runs were handed a water viscosity a thousand times too high,
and the run that recognised it ("1000x real water viscosity", with a Reynolds
number to back it up) still submitted it unchanged.

The skill channel is not a substitute: 34 gate calls selected the domain skill 13
times, and on wake turns almost never -- twelve consecutive empty selections on
2026-08-06. An empty selection is a legal response, so nothing is logged.

meta.json is read on every status call, which makes it the one channel that is
present whenever the agent is deciding.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops import OpsTuneStatusTool  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


RULE = "If the fluid property you were handed is not physical, say so before submitting."


def _campaign(tmp_path: Path, meta: dict | None) -> Path:
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    "t1": {
                        "idem_key": "t1",
                        "status": "running",
                        "campaign": "cfd",
                        "handle": None,
                        "result": None,
                        "attempts": 1,
                        "escalated": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    if meta is not None:
        (tmp_path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return ledger


async def test_the_rules_are_printed_once_a_trial_exists(tmp_path: Path) -> None:
    """The wake turns are the ones with a ledger. Printing the rules only on the
    no-ledger path -- which is what the code did -- put them everywhere except
    where they were needed."""
    ledger = _campaign(tmp_path, {"operating_policy": RULE})

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert RULE in out


async def test_a_list_of_rules_is_printed_verbatim(tmp_path: Path) -> None:
    ledger = _campaign(tmp_path, {"operating_policy": ["first rule", "second rule"]})

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert "first rule" in out and "second rule" in out


async def test_worked_examples_survive_as_written(tmp_path: Path) -> None:
    """The rules carry worked examples, not just directives -- a rule stated
    plainly was ignored six times running on the ML line. An example is only
    worth putting there if it arrives unparaphrased."""
    example = {"saw": "nu=1e-3 for water", "thought": "1000x too high", "did": "asked, then fixed it"}
    ledger = _campaign(tmp_path, {"operating_policy": {"example_1": json.dumps(example, ensure_ascii=False)}})

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert "1000x too high" in out and "asked, then fixed it" in out


async def test_a_campaign_without_rules_prints_no_heading(tmp_path: Path) -> None:
    """Nothing to say, nothing said: an empty heading in every status output is
    noise, and noise is what stops being read."""
    ledger = _campaign(tmp_path, {"host": "1.2.3.4"})

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert "Operating rules" not in out


async def test_a_missing_meta_file_is_not_an_error(tmp_path: Path) -> None:
    ledger = _campaign(tmp_path, None)

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert "Campaign" in out


async def test_the_rules_are_there_before_anything_has_run(tmp_path: Path) -> None:
    """The first turn is the one that decides what to submit, and it sees an empty
    ledger. With the rules only on the has-trials path it got back one sentence --
    "ledger is empty (starting up)" -- and nothing else.
    """
    (tmp_path / "ledger.json").write_text(json.dumps({"version": 1, "records": {}}), encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps({"operating_policy": RULE}), encoding="utf-8")

    out = await OpsTuneStatusTool().execute(ledger=str(tmp_path / "ledger.json"), metric="ndcg")

    assert RULE in out


async def test_the_rules_are_there_before_a_ledger_exists(tmp_path: Path) -> None:
    (tmp_path / "meta.json").write_text(json.dumps({"host": "1.2.3.4", "operating_policy": RULE}), encoding="utf-8")

    out = await OpsTuneStatusTool().execute(ledger=str(tmp_path / "ledger.json"), metric="ndcg")

    assert RULE in out
