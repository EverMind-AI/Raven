"""The on-call's approval gate: three layers read against the owner's mode.

The gate exists because the trunk gate refused the on-call's own wake call in
a turn nobody attended (a reviewer timed out on a busy provider), and the
campaign stopped waking. These pin that the on-call's bookkeeping never waits
on anyone, that the owner's mode decides the rest, and that handing a call to
the owner files it, asks once for every pending item, and lets the approved
call through when it is sent again.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow import approval  # noqa: E402
from oncall_flow.approval import (  # noqa: E402
    APPROVE_ALL,
    LAYER_ONE,
    REFUSE_ALL,
    ApprovalStore,
    OncallApprovalGate,
    apply_answer,
    make_approval_gate,
    read_answer,
)
from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops_escalation import OpsAskOwnerTool  # noqa: E402

from raven.contracts.plugin_surface import ServiceLocator  # noqa: E402
from raven.plugins.context import PluginContext, RuntimeHandles  # noqa: E402

SUBMIT = {"campaign": "c", "trials": [{"seed": 1}]}
REMOTE_WRITE = {"command": "rm -rf /root/workspace/run/old", "machine": "conn_cpu_64106"}


class _Call:
    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.name = name
        self.arguments = arguments


class _Reply:
    def __init__(self, tool_calls: list[_Call]) -> None:
        self.tool_calls = tool_calls


class _Provider:
    """Answers every call with one scripted tool call, or hangs when told to."""

    def __init__(self, name: str = "", arguments: dict[str, Any] | None = None, *, hang: bool = False) -> None:
        self._reply = _Reply([_Call(name, arguments or {})]) if name else _Reply([])
        self._hang = hang
        self.calls = 0

    async def chat_with_retry(self, **_: Any) -> _Reply:
        self.calls += 1
        if self._hang:
            await asyncio.sleep(10)
        return self._reply


def _gate(tmp_path: Path, mode: str, provider: Any = None, timeout: float = 5.0) -> OncallApprovalGate:
    return OncallApprovalGate(home=tmp_path, mode=mode, provider=provider, review_timeout_s=timeout)


async def _ask(gate: OncallApprovalGate, name: str, params: dict[str, Any]) -> str | None:
    return await gate.adjudicate(name, params, session_workdir=None)


# --- layer one: the on-call's own tools never wait ---------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "smart", "ask"])
async def test_the_oncalls_own_tools_pass_in_every_mode(tmp_path, mode):
    provider = _Provider(hang=True)
    gate = _gate(tmp_path, mode, provider, timeout=0.01)
    for name in sorted(LAYER_ONE):
        assert await _ask(gate, name, {"campaign": "c"}) is None, name
    assert provider.calls == 0, "the reviewer is never consulted for the on-call's own bookkeeping"
    assert ApprovalStore(tmp_path).pending() == []


@pytest.mark.asyncio
async def test_a_trunk_default_read_passes_even_in_ask_mode(tmp_path):
    assert await _ask(_gate(tmp_path, "ask"), "read_file", {"path": "/tmp/x"}) is None


def test_scheduling_the_next_wake_is_layer_one():
    assert "ops_check_later" in LAYER_ONE and "ops_ask_owner" in LAYER_ONE
    assert "ops_submit" not in LAYER_ONE and "ops_kill" not in LAYER_ONE


# --- layer two follows the owner's mode ---------------------------------------


@pytest.mark.asyncio
async def test_full_passes_work_without_review(tmp_path):
    provider = _Provider(hang=True)
    gate = _gate(tmp_path, "full", provider)
    assert await _ask(gate, "ops_submit", SUBMIT) is None
    assert await _ask(gate, "exec", REMOTE_WRITE) is None
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_ask_files_the_call_and_says_how_to_carry_on(tmp_path):
    out = await _ask(_gate(tmp_path, "ask"), "ops_submit", SUBMIT)
    assert out is not None and out.startswith("NOT RUN")
    assert "ops_ask_owner" in out and "pending item 1" in out and "ops_check_later" in out
    pending = ApprovalStore(tmp_path).pending()
    assert [item["n"] for item in pending] == [1] and pending[0]["tool"] == "ops_submit"


@pytest.mark.asyncio
async def test_the_same_call_sent_twice_is_filed_once(tmp_path):
    gate = _gate(tmp_path, "ask")
    await _ask(gate, "exec", REMOTE_WRITE)
    await _ask(gate, "exec", REMOTE_WRITE)
    assert len(ApprovalStore(tmp_path).pending()) == 1


@pytest.mark.asyncio
async def test_smart_runs_what_the_reviewer_allows(tmp_path):
    provider = _Provider("report_permission_review", {"decision": "allow", "reason": "ordinary"})
    assert await _ask(_gate(tmp_path, "smart", provider), "ops_submit", SUBMIT) is None
    assert ApprovalStore(tmp_path).pending() == []


@pytest.mark.asyncio
async def test_smart_hands_an_escalation_to_the_owner(tmp_path):
    provider = _Provider("report_permission_review", {"decision": "escalate", "reason": "deletes data"})
    out = await _ask(_gate(tmp_path, "smart", provider), "exec", REMOTE_WRITE)
    assert out is not None and "deletes data" in out
    assert len(ApprovalStore(tmp_path).pending()) == 1


@pytest.mark.asyncio
async def test_a_reviewer_that_times_out_hands_the_call_to_the_owner_instead_of_refusing_it(tmp_path):
    out = await _ask(_gate(tmp_path, "smart", _Provider(hang=True), timeout=0.01), "ops_submit", SUBMIT)
    assert out is not None and out.startswith("NOT RUN") and "timed out" in out
    assert len(ApprovalStore(tmp_path).pending()) == 1


@pytest.mark.asyncio
async def test_an_already_pending_call_is_not_reviewed_again(tmp_path):
    provider = _Provider("report_permission_review", {"decision": "escalate", "reason": "x"})
    gate = _gate(tmp_path, "smart", provider)
    await _ask(gate, "ops_submit", SUBMIT)
    await _ask(gate, "ops_submit", SUBMIT)
    assert provider.calls == 1


# --- the owner's answer --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_approved_call_runs_when_it_is_sent_again(tmp_path):
    gate = _gate(tmp_path, "ask")
    await _ask(gate, "exec", REMOTE_WRITE)
    approved, refused = await apply_answer(tmp_path, APPROVE_ALL, [1])
    assert (approved, refused) == ([1], [])
    assert await _ask(gate, "exec", REMOTE_WRITE) is None
    other = dict(REMOTE_WRITE, command="rm -rf /root/workspace/run/new")
    assert (await _ask(gate, "exec", other) or "").startswith("NOT RUN"), "an approval covers that exact call"


@pytest.mark.asyncio
async def test_a_refused_call_is_refused_with_the_owners_words(tmp_path):
    gate = _gate(tmp_path, "ask")
    await _ask(gate, "ops_submit", SUBMIT)
    await apply_answer(tmp_path, REFUSE_ALL, [1])
    out = await _ask(gate, "ops_submit", SUBMIT)
    assert out is not None and out.startswith("Error: the owner did not approve") and REFUSE_ALL in out


@pytest.mark.asyncio
async def test_a_reply_in_the_owners_own_words_is_read_by_the_model(tmp_path):
    store = ApprovalStore(tmp_path)
    store.file("d1", "exec", "a", "ask")
    store.file("d2", "exec", "b", "ask")
    provider = _Provider("report_owner_decision", {"approve": [1], "refuse": [2]})
    approve, refuse = await read_answer("first one yes, not the second", store.pending(), provider)
    assert (approve, refuse) == ({1}, {2})


@pytest.mark.asyncio
async def test_a_reply_nobody_can_read_leaves_everything_pending(tmp_path):
    store = ApprovalStore(tmp_path)
    store.file("d1", "exec", "a", "ask")
    assert await read_answer("hmm, let me think", store.pending(), None) == (set(), set())
    assert await read_answer("hmm", store.pending(), _Provider()) == (set(), set())


@pytest.mark.asyncio
async def test_a_free_text_reply_without_a_model_is_kept_and_read_by_the_gate(tmp_path):
    gate = _gate(tmp_path, "ask", _Provider("report_owner_decision", {"approve": [1], "refuse": []}))
    await _ask(gate, "ops_submit", SUBMIT)
    assert await apply_answer(tmp_path, "go ahead", [1]) == ([], [])
    assert ApprovalStore(tmp_path).pending()[0]["answer"] == "go ahead"
    assert await _ask(gate, "ops_submit", SUBMIT) is None


# --- one question carries every pending item -----------------------------------


class _Owner:
    def __init__(self) -> None:
        self.asked: list[dict] = []
        self.answers: list[asyncio.Future] = []

    async def __call__(self, prompt, choices, conversation_id, timeout_s):
        answer = asyncio.get_running_loop().create_future()
        self.asked.append({"prompt": prompt, "choices": choices})
        self.answers.append(answer)
        try:
            return await answer
        except asyncio.CancelledError:
            return ""


def _campaign(tmp_path: Path) -> Path:
    cdir = tmp_path / "camp"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "meta.json").write_text(json.dumps({"host": "h", "port": 1, "key": "k", "start_hour": 9}))
    return cdir


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_every_pending_item_rides_one_question_and_the_answer_settles_them(tmp_path):
    home = tmp_path / "ops"
    tools_base.set_home(home)
    gate = _gate(home, "ask")
    await _ask(gate, "ops_submit", SUBMIT)
    await _ask(gate, "exec", REMOTE_WRITE)
    owner = _Owner()
    tool = OpsAskOwnerTool()
    tool.bind_runtime(RuntimeHandles(direct_ask=owner))
    tool.set_context("acp", "s1", session_key="acp:s1")
    cdir = _campaign(tmp_path)

    out = await tool.execute(
        campaign="c",
        question="Two steps need you.",
        expected_loss_minutes=60,
        ledger=str(cdir / "ledger.json"),
    )
    await _settle()

    assert "put to the owner as a question" in out
    prompt = owner.asked[0]["prompt"]
    assert "2 action(s) are waiting for your approval" in prompt and "  1. " in prompt and "  2. " in prompt
    assert owner.asked[0]["choices"] == [APPROVE_ALL, REFUSE_ALL]

    owner.answers[0].set_result(APPROVE_ALL)
    await _settle()
    assert ApprovalStore(home).pending() == []
    assert await _ask(gate, "ops_submit", SUBMIT) is None
    assert await _ask(gate, "exec", REMOTE_WRITE) is None


@pytest.mark.asyncio
async def test_a_question_with_nothing_pending_is_unchanged(tmp_path):
    tools_base.set_home(tmp_path / "ops")
    owner = _Owner()
    tool = OpsAskOwnerTool()
    tool.bind_runtime(RuntimeHandles(direct_ask=owner))
    tool.set_context("acp", "s1", session_key="acp:s1")
    cdir = _campaign(tmp_path)
    await tool.execute(campaign="c", question="Which seed?", expected_loss_minutes=60, ledger=str(cdir / "ledger.json"))
    await _settle()
    assert "waiting for your approval" not in owner.asked[0]["prompt"]
    assert owner.asked[0]["choices"] is None or owner.asked[0]["choices"] == []


# --- the factory ---------------------------------------------------------------


def _ctx(tmp_path: Path, config: dict) -> PluginContext:
    services = ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a")
    return PluginContext(config=config, services=services)


def test_the_gate_declines_without_a_rendered_policy(tmp_path):
    assert make_approval_gate(_ctx(tmp_path, {"enabled": True})) is None
    assert make_approval_gate(_ctx(tmp_path, {"enabled": False, "approval": {"mode": "ask"}})) is None


def test_the_gate_takes_the_rendered_mode(tmp_path):
    gate = make_approval_gate(_ctx(tmp_path, {"enabled": True, "approval": {"mode": "ask"}}))
    assert isinstance(gate, OncallApprovalGate) and gate.mode == "ask"
    assert gate.name == approval.OncallApprovalGate.name == "oncall_approval"
