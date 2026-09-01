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
    yield


async def test_status_with_no_ledger_finds_the_only_campaign(tmp_path, _restore_config_path):
    home = _instance(tmp_path, ["cfd-transient"])
    tools_base.set_home(home / "ops")

    out = await OpsTuneStatusTool().execute()

    # The full path, not just the name: the operator's real ~/.raven/ops happens to
    # hold a campaign called cfd-transient too, so asserting the name alone passed
    # with the instance-awareness reverted -- a green light for the bug.
    assert str(home / "ops" / "cfd-transient") in out


async def test_status_with_no_ledger_and_two_campaigns_asks_which(tmp_path, _restore_config_path):
    """Silently picking one is the worst outcome: a run against the wrong campaign
    reads exactly like a run against the right one."""
    home = _instance(tmp_path, ["cfd-transient", "cfd-steady"])
    tools_base.set_home(home / "ops")

    out = await OpsTuneStatusTool().execute()

    assert "cfd-transient" in out and "cfd-steady" in out


async def test_status_with_no_ledger_and_no_campaign_says_so(tmp_path, _restore_config_path):
    home = _instance(tmp_path, [])
    tools_base.set_home(home / "ops")

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
    tools_base.set_home(home / "ops")

    out = await OpsTuneStatusTool().execute(ledger="", metric="ndcg")

    assert isinstance(out, str) and str(home / "ops") in out
