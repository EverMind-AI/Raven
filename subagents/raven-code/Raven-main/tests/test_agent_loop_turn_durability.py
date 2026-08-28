"""Long-turn durability: history pairing repair and mid-turn checkpoints.

A crash mid-turn used to lose the whole trajectory (a turn is the unit of
session persistence, and benchmark tasks are one turn of hundreds of
iterations), and a partial persist could leave assistant tool_calls with no
results — a history providers reject.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# --------------------------------------------------------------------------- #
# unit: _repair_tool_pairing                                                   #
# --------------------------------------------------------------------------- #


def _asst_with_calls(*ids):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": i, "type": "function", "function": {"name": "exec", "arguments": "{}"}} for i in ids],
    }


def _tool_result(cid):
    return {"role": "tool", "tool_call_id": cid, "name": "exec", "content": "ok"}


def test_wellformed_history_is_returned_unchanged():
    msgs = [
        {"role": "user", "content": "hi"},
        _asst_with_calls("a"),
        _tool_result("a"),
        {"role": "assistant", "content": "done"},
    ]
    assert AgentLoop._repair_tool_pairing(msgs) is msgs


def test_dangling_call_gets_synthetic_aborted_result():
    msgs = [
        {"role": "user", "content": "hi"},
        _asst_with_calls("a", "b"),
        _tool_result("a"),
        {"role": "user", "content": "continue"},
    ]
    repaired = AgentLoop._repair_tool_pairing(msgs)
    kinds = [(m.get("role"), m.get("tool_call_id")) for m in repaired]
    assert kinds == [("user", None), ("assistant", None), ("tool", "a"), ("tool", "b"), ("user", None)]
    assert "aborted" in repaired[3]["content"]


def test_dangling_call_at_end_of_history_is_completed():
    msgs = [{"role": "user", "content": "hi"}, _asst_with_calls("a")]
    repaired = AgentLoop._repair_tool_pairing(msgs)
    assert repaired[-1]["role"] == "tool"
    assert repaired[-1]["tool_call_id"] == "a"


def test_orphan_tool_result_is_dropped():
    msgs = [
        {"role": "user", "content": "hi"},
        _tool_result("ghost"),
        {"role": "assistant", "content": "done"},
    ]
    repaired = AgentLoop._repair_tool_pairing(msgs)
    assert [m["role"] for m in repaired] == ["user", "assistant"]


# --------------------------------------------------------------------------- #
# loop level: mid-turn checkpoint cadence and session durability               #
# --------------------------------------------------------------------------- #


class _NToolCallsProvider(LLMProvider):
    """Issues one successful tool call per iteration, forever."""

    def __init__(self):
        super().__init__(api_key="test")
        self.turn = 0

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        if tools is None:
            return LLMResponse(content="done", finish_reason="stop")
        self.turn += 1
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(id=f"c{self.turn}", name="list_dir", arguments={"path": "."}),
            ],
            finish_reason="tool_calls",
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_on_checkpoint_fires_every_n_iterations(workspace):
    agent = AgentLoop(
        provider=_NToolCallsProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=21,
        restrict_to_workspace=True,
    )
    seen: list[int] = []
    await agent._run_agent_loop(
        [{"role": "user", "content": "go"}],
        on_checkpoint=lambda msgs: seen.append(len(msgs)),
    )
    # Iterations 10 and 20 checkpoint; lengths are cumulative and increasing.
    assert len(seen) == 2
    assert seen[0] < seen[1]


@pytest.mark.asyncio
async def test_no_checkpoint_callback_is_fine(workspace):
    agent = AgentLoop(
        provider=_NToolCallsProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=12,
        restrict_to_workspace=True,
    )
    final, _, msgs, outcome = await agent._run_agent_loop([{"role": "user", "content": "go"}])
    assert msgs


@pytest.mark.asyncio
async def test_incremental_persistence_never_duplicates_messages(workspace):
    """Mid-turn checkpoints plus the final save must persist each message
    exactly once."""
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    agent = AgentLoop(
        provider=_NToolCallsProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=25,
        restrict_to_workspace=True,
    )
    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )
    session = agent.sessions.get_or_create("s1")
    ids = [m.get("tool_call_id") for m in session.messages if m.get("role") == "tool"]
    assert len(ids) == 25, f"expected all 25 tool results persisted once, got {len(ids)}"
    assert len(ids) == len(set(ids)), f"duplicated tool results persisted: {len(ids)} vs {len(set(ids))}"
