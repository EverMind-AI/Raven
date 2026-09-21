"""One real AgentLoop turn hands each role the complete participant roster."""

from __future__ import annotations

from dataclasses import replace

import pytest

from raven.agent.harness.participants import compose_advice
from raven.agent.hook import CompositeHook
from raven.agent.hook.participant import ParticipantHook
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.contracts.participant import AgentParticipant
from raven.providers.base import LLMProvider, LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class _Provider(LLMProvider):
    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kwargs):
        return LLMResponse(content="done", finish_reason="stop")

    async def chat_with_retry(self, messages, tools=None, model=None, fallback_models=None, **kwargs):
        return await self.chat(messages, tools, model, **kwargs)


class _Named(AgentParticipant):
    def __init__(self, name: str) -> None:
        self.name = name

    async def advise(self, step):
        return self.name


@pytest.mark.asyncio
async def test_a_real_turn_asks_planning_once_per_phase_with_the_whole_roster(tmp_path):
    seen: list[tuple[str, ...]] = []

    class Planning:
        async def prepare(self, request):
            return await original.prepare(request)

        async def ask_advice(self, step, participants):
            seen.append(tuple(participant.name for participant in participants))
            return await compose_advice(step, participants)

    hooks = CompositeHook(
        [
            ParticipantHook("a", lambda: _Named("a")),
            ParticipantHook("b", lambda: _Named("b")),
        ]
    )
    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    loop.hooks = hooks
    original = loop.harness.planning
    loop.harness = replace(loop.harness, planning=Planning())

    async def emit(*args, **kwargs):
        return None

    await loop.run_turn(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="answer briefly",
            conversation="test:c1",
        ),
        emit,
        lambda: [],
        stream=False,
    )

    assert seen == [("a", "b"), ("a", "b")]
