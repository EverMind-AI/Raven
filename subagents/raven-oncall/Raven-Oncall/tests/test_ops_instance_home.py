"""Campaign state belongs to the raven instance that is running, and the ledger
path must be answerable from disk.

Both come from the same measurement (2026-08-07). Three CFD runs were started in
parallel, each with its own --config, and the only thing keeping their campaigns
apart was an absolute ledger path typed into the task text: ops resolved to a
fixed ~/.raven/ops regardless of which instance was running. That path also had
to survive into the wake turns, which start with no history -- so a handle that
lives only in the task text is gone exactly when the decisions get made.

Passing no ledger at all used to reach Path("") -> "." and raise
IsADirectoryError: a crash, not a message, on the one turn that had nothing to
go on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.ops import OpsTuneStatusTool
from raven.config import loader
from raven.ops import instrument


def _instance(tmp_path: Path, campaigns: list[str]) -> Path:
    home = tmp_path / ".raven-slotN"
    (home).mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text("{}", encoding="utf-8")
    for c in campaigns:
        d = home / "ops" / c
        d.mkdir(parents=True)
        (d / "ledger.json").write_text(json.dumps({"version": 1, "records": {}}), encoding="utf-8")
    return home


@pytest.fixture
def _restore_config_path():
    before = loader._current_config_path
    yield
    loader._current_config_path = before


def test_the_ops_home_follows_the_running_instance(tmp_path, _restore_config_path):
    home = _instance(tmp_path, ["cfd-transient"])

    loader.set_config_path(home / "config.json")

    assert instrument.ops_home() == home / "ops"


def test_with_no_instance_chosen_it_stays_on_the_default(_restore_config_path):
    """Tests and library callers never call set_config_path; they must keep the
    behaviour they had.

    Asserted against what the config layer resolves rather than against a literal
    ~/.raven, because the session guard redirects that so no test writes to the
    real home. The bug this catches is unchanged: a fallback that reaches a stale
    module constant instead of delegating still fails here.
    """
    from raven.config import paths as config_paths

    loader._current_config_path = None

    assert instrument.ops_home() == config_paths.get_ops_home()


async def test_status_with_no_ledger_finds_the_only_campaign(tmp_path, _restore_config_path):
    home = _instance(tmp_path, ["cfd-transient"])
    loader.set_config_path(home / "config.json")

    out = await OpsTuneStatusTool().execute()

    # The full path, not just the name: the operator's real ~/.raven/ops happens to
    # hold a campaign called cfd-transient too, so asserting the name alone passed
    # with the instance-awareness reverted -- a green light for the bug.
    assert str(home / "ops" / "cfd-transient") in out


async def test_status_with_no_ledger_and_two_campaigns_asks_which(tmp_path, _restore_config_path):
    """Silently picking one is the worst outcome: a run against the wrong campaign
    reads exactly like a run against the right one."""
    home = _instance(tmp_path, ["cfd-transient", "cfd-steady"])
    loader.set_config_path(home / "config.json")

    out = await OpsTuneStatusTool().execute()

    assert "cfd-transient" in out and "cfd-steady" in out


async def test_status_with_no_ledger_and_no_campaign_says_so(tmp_path, _restore_config_path):
    home = _instance(tmp_path, [])
    loader.set_config_path(home / "config.json")

    out = await OpsTuneStatusTool().execute()

    # Wording from this line, not from the CFD branch's: the two say the same
    # thing, and this one also names the directory it looked in and what to do
    # about it -- which is the difference between "nothing here" and "nothing
    # here, and here is where here is".
    assert "no campaign under" in out and str(home / "ops") in out


async def test_an_empty_ledger_argument_answers_instead_of_crashing(tmp_path, _restore_config_path):
    """The regression itself: Path("") is ".", and reading it raised
    IsADirectoryError before the agent got a single word back."""
    home = _instance(tmp_path, ["cfd-transient"])
    loader.set_config_path(home / "config.json")

    out = await OpsTuneStatusTool().execute(ledger="", metric="ndcg")

    assert isinstance(out, str) and str(home / "ops") in out
