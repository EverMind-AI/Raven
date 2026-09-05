"""Every branch a wake turn is offered names the tool that performs it.

A wake turn is a cold start: the scheduled message is the whole of its context.
The re-check message used to end with "(ops_submit / ask user / stop)" -- two of
the three were prose. "stop" is a verb with no tool behind it, and ops_finish,
which is the tool that performs it, appeared in no wake message and in no
bootstrap file.

Measured 2026-08-11, four runs across two domains (embedding fine-tuning and an
OpenFOAM transient): each finished its work, wrote a complete report into the
chat -- physical checks, budget spent and remaining, what it had changed and why
-- then offered the user three options and waited. Nobody was there to answer.
The re-arm woke it every fourteen minutes until the cap, and each of those turns
produced another copy of the same report. ops_check_later was named in the same
sentence and was called every time; ops_finish's predecessor was named nowhere and called never.

It had written the report. What it lacked was the name of the door.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops import OpsCheckLaterTool, OpsSubmitTool  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


def _bound(tool, scheduler):
    tool.bind_runtime(SimpleNamespace(wake_scheduler=scheduler))
    return tool


BRANCH_TOOLS = ["ops_submit", "ops_check_later", "ops_finish", "ops_ask_owner"]


def _campaign(tmp_path: Path, monkeypatch) -> Path:

    root = tmp_path / "ops"
    cdir = root / "c1"
    cdir.mkdir(parents=True)
    (cdir / "meta.json").write_text(
        json.dumps({"backend": "process", "host": "h", "port": 22, "remote_dir": "/r"}),
        encoding="utf-8",
    )
    tools_base.set_home(root)
    return cdir


class _Cron:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.messages: list[str] = []
        self.advanced: list[str] = []
        self.cancelled: list[str] = []

    def schedule_wake(self, key, at_ms, message, **route):
        self.jobs = [j for j in self.jobs if j["key"] != key]
        self.jobs.append({"key": key, "at_ms": at_ms, "message": message, **route})
        self.messages.append(message)
        return SimpleNamespace(id=f"oncall-flow:{key}")

    def advance_wake_to_now(self, key):
        self.advanced.append(key)
        return any(j["key"] == key for j in self.jobs)

    def pending_wakes(self, prefix=""):
        return [SimpleNamespace(id=f"oncall-flow:{j['key']}") for j in self.jobs if j["key"].startswith(prefix)]

    def cancel_wake(self, key):
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if j["key"] != key]
        self.cancelled.append(key)
        return len(self.jobs) < before


@pytest.mark.asyncio
async def test_the_recheck_wake_names_a_tool_for_every_branch(tmp_path: Path, monkeypatch) -> None:
    cdir = _campaign(tmp_path, monkeypatch)
    # The basis gate wants a reading taken since the last decision, and it is right
    # to: that gate is why a wait has to cite something fresh. Record one so this
    # test exercises the wake message rather than the gate.
    from oncall_flow.state_claims import StateFacts, write_facts

    write_facts(cdir, StateFacts(probe_seq=1, shown_values=[0.30]))
    cron = _Cron()
    tool = _bound(OpsCheckLaterTool(), cron)
    tool.set_context("tui", "default")

    await tool.execute(
        eta_seconds=600,
        campaign="c1",
        ledger=str(cdir / "ledger.json"),
        basis="trial at step 200, ndcg 0.30, still climbing; waiting 10 minutes",
    )

    assert cron.messages, "the re-check must schedule a wake"
    message = cron.messages[0]
    for name in BRANCH_TOOLS:
        assert name in message, f"{name} is a branch the wake offers; name it"


def test_the_submit_tool_description_names_a_tool_for_every_branch() -> None:
    """The description is the other place a loop learns what the branches are."""
    description = OpsSubmitTool().description

    for name in BRANCH_TOOLS:
        assert name in description, f"{name} is a branch the description offers; name it"


def test_no_branch_is_offered_as_prose_alone() -> None:
    """The specific wording that produced the failure, kept as a regression.

    "stop" and "ask the user" read as instructions to a person. A loop that takes
    them literally writes prose, which no mechanism can see: the ledger records
    nothing, the campaign never terminates, and the re-arm keeps waking a turn
    that has already done its work.
    """
    description = OpsSubmitTool().description

    assert "ask the user if uncertain" not in description
    assert "or stop and report" not in description
