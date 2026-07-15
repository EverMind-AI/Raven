"""Spine ``TurnRunner`` that wraps an ``AgentLoop``: each turn delegates to
``AgentLoop.run_turn``. It lives on the agent side because it holds the loop;
spine never imports the agent.

``stream`` is the canon Q2-D assembly switch: a streaming outlet (TUI) passes
True so the reply streams as StreamDelta and dissolves; a non-streaming outlet
(REPL) passes False so the reply is one Text.

``inline_tool_stream`` lets a long tool (deep_research) stream its output inline
and return a compact receipt; on for local interactive surfaces (CLI/TUI), off
for channels/gateway (they wait for async delivery).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raven.agent.loop import AgentLoop
    from raven.spine.runner import Drain, Emit, TurnOutcome
    from raven.spine.turn import TurnRequest


class AgentTurnRunner:
    def __init__(self, agent_loop: AgentLoop, *, stream: bool, inline_tool_stream: bool = False) -> None:
        self._loop = agent_loop
        self._stream = stream
        self._inline_tool_stream = inline_tool_stream

    async def run(self, req: TurnRequest, emit: Emit, drain: Drain) -> TurnOutcome:
        return await self._loop.run_turn(
            req, emit, drain, stream=self._stream, inline_tool_stream=self._inline_tool_stream
        )
