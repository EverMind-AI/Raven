"""A live host model keeps its Task Board across turns: two matters apart, a dispatch placed.

Real calls on ``openrouter/z-ai/glm-5.3-flash``. The spawn tool is a stand-in that
returns the manager's own receipt, so the turn dispatches without starting a
sub-agent; everything after the receipt -- the dispatch list, the post-turn call,
validation, the write -- is the real path.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.task_board.store import load_board
from raven.config.schema import TaskBoardConfig
from raven.contracts.tool import Tool
from raven.providers.litellm_provider import LiteLLMProvider
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


def _config_key() -> str:
    path = Path.home() / ".raven" / "config.json"
    try:
        section = json.loads(path.read_text(encoding="utf-8")).get("providers", {}).get("openrouter") or {}
    except Exception:
        return ""
    return section.get("apiKey") or section.get("api_key") or ""


OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY") or _config_key()
MODEL = "openrouter/z-ai/glm-5.3-flash"
KEY = "test:live"

pytestmark = [
    pytest.mark.real_llm,
    pytest.mark.slow,
    pytest.mark.skipif(not OPENROUTER_KEY, reason="no OpenRouter credential (env or ~/.raven)"),
]


class _ReceiptSpawn(Tool):
    """Answers like the manager's spawn receipt, and starts nothing."""

    def __init__(self) -> None:
        self.started: list[str] = []

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return "Start a sub-agent on a task in the background; returns at once with its id."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "task_summary": {"type": "string"},
                "task": {"type": "string"},
            },
            "required": ["node_id", "task_summary", "task"],
        }

    async def execute(self, node_id: str, task_summary: str, task: str, **_: Any) -> str:
        self.started.append(node_id)
        return f"Subagent [{task_summary}] started (id: 1a2b3c4d). I'll notify you when it completes."


async def _turn(agent: AgentLoop, text: str) -> None:
    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="live", sender_id="user", chat_type=ChatType.DM),
            text=text,
        ),
        session_key=KEY,
    )
    while agent._consolidation_tasks:
        await asyncio.gather(*list(agent._consolidation_tasks), return_exceptions=True)


async def test_a_live_model_keeps_two_matters_apart_and_places_its_dispatch(tmp_path, monkeypatch):
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)
    agent = AgentLoop(
        provider=LiteLLMProvider(api_key=OPENROUTER_KEY, default_model=MODEL, provider_name="openrouter"),
        workspace=tmp_path,
        model=MODEL,
        policy=TurnPolicy(max_iterations=6),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(task_board_config=TaskBoardConfig()),
    )
    spawn = _ReceiptSpawn()
    agent.tools.register(spawn)

    await _turn(
        agent,
        "I'm planning my daughter's birthday party for Saturday. Call the spawn tool now with "
        "node_id 'cake_research' to have a sub-agent find three cake shops nearby; just dispatch "
        "it and tell me it started. Do not ask me anything first.",
    )
    # The precondition, named: placement can only be checked once the live model
    # has dispatched. A model that asked instead fails here, not further down.
    assert "cake_research" in spawn.started, f"turn 1 dispatched {spawn.started}, not cake_research"
    await _turn(agent, "Unrelated: draft a two-sentence email declining Friday's budget meeting.")
    await _turn(agent, "Back to the birthday party: add 'order balloons' to the plan.")

    board = load_board(agent.sessions.session_dir(KEY))
    assert board.unassigned == [], board
    assert len(board.groups) == 2, [g.title for g in board.groups]
    party = next(g for g in board.groups if any(t.id == "cake_research" for s in g.steps for t in s.tasks))
    assert len(party.steps) >= 2, [s.text for s in party.steps]
    # The other matter's step count is the model's call: a later reply that
    # reminds the reader of the pending draft may record a step of its own.
    assert next(g for g in board.groups if g is not party).steps
