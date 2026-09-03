"""Taking a declared action: it goes in the ledger, and it happens once.

The command is one line the transport could have run directly, and running it
directly is the failure this exists to prevent. A wake turn is a cold start, so an
order placed with exec leaves nothing behind: the next wake reads the ledger, sees
no order, and places it again. Six shares.

The design note for this says the dividing line is not "long" versus "short" -- it
is whether the thing has an effect. Looking can go through exec, because doing it
twice costs a round trip. Acting cannot.
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

from oncall_flow.instrument import read_events  # noqa: E402
from oncall_flow.state_claims import read_facts, write_facts  # noqa: E402
from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops import OpsSubmitTool  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


def _bound(tool, scheduler):
    tool.bind_runtime(SimpleNamespace(wake_scheduler=scheduler))
    return tool


def _looked(cdir: Path) -> None:
    """Record that a look happened, the way ops_tune_status does.

    Acting is a decision, and a decision needs an observation taken since the last
    one -- so every act in a real run follows a look. Without this the basis gate
    refuses first and none of the cases below is reached, which is the gate working
    rather than a fixture detail worth hiding.
    """
    import dataclasses

    facts = read_facts(cdir)
    write_facts(cdir, dataclasses.replace(facts, probe_seq=facts.probe_seq + 1))


@pytest.fixture
def campaign(tmp_path, monkeypatch):

    home = tmp_path / "ops"
    cdir = home / "watch"
    cdir.mkdir(parents=True)
    (cdir / "meta.json").write_text(
        json.dumps(
            {
                "backend": "process",
                "connection": "conn_ok",
                "transport": "local",
                "objective": {"kind": "condition", "condition": "VOLT at or below 223.58"},
                "objective_words": "buy 3 shares if VOLT drops 10%",
                "command": "true",
                "max_rounds": 50,
                "actions": [
                    {"name": "buy", "command": "echo filled {qty} > /dev/null; echo ok", "repeat": "harmful"},
                    {"name": "note_it", "command": "echo noted", "repeat": "safe"},
                ],
            }
        ),
        encoding="utf-8",
    )
    tools_base.set_home(home)
    monkeypatch.setattr("oncall_flow.connections.get", lambda cid: {"id": cid, "transport": "local"})
    monkeypatch.setattr("oncall_flow.connections.resolve_into", lambda meta: {**meta, "transport": "local"})
    return cdir


async def _act(cdir: Path, **over):
    args = dict(
        campaign="watch",
        ledger=str(cdir / "ledger.json"),
        eta_seconds=600,
        configs=[],
        action="buy",
        values={"qty": 3},
        basis="VOLT read 223.10, below the 223.58 the campaign was watching for",
    )
    args.update(over)
    _looked(cdir)
    return await OpsSubmitTool().execute(**args)


@pytest.mark.asyncio
async def test_an_undeclared_action_is_refused_and_the_table_is_shown(campaign) -> None:
    """The table is the list of what may be done, and it is fixed before anything
    runs -- so this refusal is the point of having it."""
    out = await _act(campaign, action="sell")

    assert out.startswith("REFUSED")
    assert "buy" in out and "note_it" in out
    assert not (campaign / "ledger.json").exists(), "nothing was recorded"


@pytest.mark.asyncio
async def test_an_action_without_its_values_is_refused(campaign) -> None:
    out = await _act(campaign, values={})

    assert out.startswith("REFUSED") and "qty" in out


@pytest.mark.asyncio
async def test_an_action_without_a_basis_is_refused(campaign) -> None:
    """Acting is a decision, and the record of a decision is what it was made on."""
    out = await _act(campaign, basis="")

    assert "REFUSED" in out


@pytest.mark.asyncio
async def test_the_action_runs_and_lands_in_the_ledger(campaign) -> None:
    out = await _act(campaign)

    assert "Did 'buy'" in out
    records = json.loads((campaign / "ledger.json").read_text())["records"]
    ((key, rec),) = records.items()
    assert rec["status"] == "succeeded"
    assert rec["config"] == {"action": "buy", "values": {"qty": 3}}
    assert rec["result"]["output"]["rc"] == 0


@pytest.mark.asyncio
async def test_the_same_harmful_action_is_refused_the_second_time(campaign) -> None:
    """The cold-start guarantee. Not a style rule: the second wake has nothing but
    this record to tell it the order already went in."""
    await _act(campaign)

    again = await _act(campaign)

    assert again.startswith("ALREADY DONE")
    assert len(json.loads((campaign / "ledger.json").read_text())["records"]) == 1


@pytest.mark.asyncio
async def test_the_same_action_with_different_values_is_a_different_call(campaign) -> None:
    await _act(campaign)

    out = await _act(campaign, values={"qty": 5})

    assert "Did 'buy'" in out
    assert len(json.loads((campaign / "ledger.json").read_text())["records"]) == 2


@pytest.mark.asyncio
async def test_an_action_declared_safe_may_repeat(campaign) -> None:
    first = await _act(campaign, action="note_it", values={})
    second = await _act(campaign, action="note_it", values={})

    assert "Did 'note_it'" in first and "Did 'note_it'" in second
    assert len(json.loads((campaign / "ledger.json").read_text())["records"]) == 2


@pytest.mark.asyncio
async def test_a_failing_action_is_recorded_as_failed_and_says_what_it_said(campaign) -> None:
    """Whether it took effect anyway is not something this can know, and the reply
    says so rather than inviting a retry."""
    meta = json.loads((campaign / "meta.json").read_text())
    meta["actions"] = [{"name": "buy", "command": "echo refused by broker; exit 7", "repeat": "harmful"}]
    (campaign / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    out = await _act(campaign)

    assert "failed (exit 7)" in out and "refused by broker" in out
    records = json.loads((campaign / "ledger.json").read_text())["records"]
    assert list(records.values())[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_taking_an_action_is_in_the_trail(campaign) -> None:
    await _act(campaign)

    taken = [e for e in read_events(campaign) if e["kind"] == "action_taken"]
    assert taken and taken[0]["action"] == "buy" and taken[0]["values"] == {"qty": 3}


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
async def test_acting_leaves_something_that_brings_the_campaign_back(campaign) -> None:
    """The invariant every branch of an on-call turn owes. An action finishes the
    moment the command returns, so nothing about it outlives the turn -- and a turn
    that ends with no wake pending is a campaign that stops with its budget
    unspent. Measured 2026-08-21 on the ask-owner path: three hours, 86% and 81%
    left. Naming ops_check_later in the reply is a sentence the loop may or may not
    act on; this is a timer."""
    cron = _Cron()
    _looked(campaign)
    tool = _bound(OpsSubmitTool(), cron)
    tool.set_context("tui", "default", session_key="w1")

    out = await tool.execute(
        campaign="watch",
        ledger=str(campaign / "ledger.json"),
        eta_seconds=900,
        configs=[],
        action="buy",
        values={"qty": 3},
        basis="volt read 223.10, below the 223.58 this campaign was watching for",
    )

    assert "Did 'buy'" in out
    assert len(cron.jobs) == 1
    assert cron.jobs[0]["key"] == "watch"
    assert "must not be done again" in cron.jobs[0]["message"]


@pytest.mark.asyncio
async def test_a_running_trial_keeps_its_own_wake(campaign) -> None:
    """Its wake is already pending, and arranging another would replace it with an
    earlier one -- waking the loop to a round that has not finished."""
    (campaign / "ledger.json").write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    "r0": {
                        "idem_key": "r0",
                        "status": "running",
                        "campaign": "watch",
                        "handle": {"backend": "process", "job_id": "ops-r0"},
                        "result": None,
                        "attempts": 1,
                        "escalated": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    cron = _Cron()
    _looked(campaign)
    tool = _bound(OpsSubmitTool(), cron)
    tool.set_context("tui", "default", session_key="w1")

    out = await tool.execute(
        campaign="watch",
        ledger=str(campaign / "ledger.json"),
        eta_seconds=900,
        configs=[],
        action="note_it",
        values={},
        basis="volt read 223.10, and the running trial is unaffected",
    )

    assert "still running" in out
    assert cron.jobs == []


@pytest.mark.asyncio
async def test_an_action_of_none_is_pointed_at_the_door_that_already_exists(campaign) -> None:
    """ "I looked and nothing needed doing" is a real and common outcome -- 65 of
    SentinelBench's 100 tasks end that way -- and ops_check_later already records
    the look, its basis and the next wake. A second door would write the same three
    facts under another name."""
    out = await _act(campaign, action="none", values={})

    assert "ops_check_later" in out
    assert not (campaign / "ledger.json").exists(), "no trial, no action, no record of one"
