"""Unit tests for ProactivePlanner — mocked provider, no network."""

from __future__ import annotations

from datetime import datetime

from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.proactive_engine.sentinel import (
    ActiveSession,
    NudgePolicyState,
    PlannerContext,
    ProactivePlanner,
    Routine,
)


class StubProvider(LLMProvider):
    """Returns a pre-configured response, recording last inputs for inspection."""

    def __init__(self, response: LLMResponse):
        super().__init__(api_key="test")
        self._response = response
        self.last_messages: list[dict] | None = None
        self.last_tools: list[dict] | None = None
        self.last_tool_choice = None

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
        self.last_messages = messages
        self.last_tools = tools
        self.last_tool_choice = tool_choice
        return self._response

    def get_default_model(self) -> str:
        return "stub"


def _make_context() -> PlannerContext:
    return PlannerContext(
        now=datetime.fromisoformat("2026-06-18T23:15:00+08:00"),
        memory_md="- Duolingo streak 47 days, prefers gentle tone",
        active_sessions=[
            ActiveSession(
                key="telegram:home",
                last_active_at=datetime.fromisoformat("2026-06-18T22:50:00+08:00"),
                last_user_message="困死了，躺一会儿",
                last_assistant_message="辛苦了",
            ),
        ],
        routines=[
            Routine(
                id="duolingo-evening",
                pattern="每天 22:00-22:45 Duolingo 打卡",
                time_slot=(22, 23),
                status="active",
                occurrence_count=47,
                user_confirmed=True,
            ),
        ],
        nudge_policy_state=NudgePolicyState(in_quiet_hours=False, remaining_today=3),
    )


async def test_planner_parses_tool_call_nudge():
    provider = StubProvider(LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(
            id="c1",
            name="planner_decision",
            arguments={
                "action": "nudge",
                "reason": "routine time window ended without checkin",
                "proactivity_score": 0.8,
                "priority": "low",
                "target_session": "telegram:home",
                "nudge_message": "你 Duolingo 连续 47 天了，今天还没打…",
            },
        )],
    ))

    decision = await ProactivePlanner(provider, "stub").decide(_make_context())

    assert decision.action == "nudge"
    assert decision.priority == "low"
    assert decision.proactivity_score == 0.8
    assert decision.target_session == "telegram:home"
    assert "47" in (decision.nudge_message or "")


async def test_planner_defaults_to_skip_when_no_tool_call():
    provider = StubProvider(LLMResponse(content="sorry, I don't know", tool_calls=[]))
    decision = await ProactivePlanner(provider, "stub").decide(_make_context())
    assert decision.action == "skip"
    assert "did not call" in decision.reason


async def test_planner_forces_valid_action():
    provider = StubProvider(LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(
            id="c",
            name="planner_decision",
            arguments={"action": "bogus", "reason": "x", "proactivity_score": 0.9},
        )],
    ))
    decision = await ProactivePlanner(provider, "stub").decide(_make_context())
    assert decision.action == "skip"


async def test_planner_clamps_score_and_priority():
    provider = StubProvider(LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(
            id="c",
            name="planner_decision",
            arguments={
                "action": "nudge",
                "reason": "x",
                "proactivity_score": 3.5,          # out of range
                "priority": "extremely_urgent",    # invalid
                "nudge_message": "m",
            },
        )],
    ))
    decision = await ProactivePlanner(provider, "stub").decide(_make_context())
    assert decision.proactivity_score == 1.0
    assert decision.priority == "low"


async def test_planner_passes_rich_context_to_prompt():
    provider = StubProvider(LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(
            id="c",
            name="planner_decision",
            arguments={"action": "skip", "reason": "nothing", "proactivity_score": 0.1},
        )],
    ))
    await ProactivePlanner(provider, "stub").decide(_make_context())

    user_msg = next(m for m in provider.last_messages if m["role"] == "user")
    content = user_msg["content"]
    assert "Duolingo" in content
    assert "困死了" in content
    assert "47" in content
    assert "telegram:home" in content
    # tool_choice is intentionally not forced — OpenRouter rejects strict
    # forms; strong system prompt carries the constraint instead.
    assert provider.last_tool_choice is None
    assert provider.last_tools is not None
    assert provider.last_tools[0]["function"]["name"] == "planner_decision"


async def test_planner_parses_nudge_defer():
    """Ensure the new nudge_defer action round-trips with its defer_condition."""
    provider = StubProvider(LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(
            id="c",
            name="planner_decision",
            arguments={
                "action": "nudge_defer",
                "reason": "user is consulting about health issue; wait to add refill reminder",
                "proactivity_score": 0.75,
                "priority": "medium",
                "target_session": "telegram:home",
                "nudge_message": "顺便两件和妈妈健康相关的事...",
                "defer_condition": "当前腰疼咨询告一段落",
            },
        )],
    ))
    decision = await ProactivePlanner(provider, "stub").decide(_make_context())
    assert decision.action == "nudge_defer"
    assert decision.defer_condition == "当前腰疼咨询告一段落"
    assert "妈妈健康" in (decision.nudge_message or "")


async def test_planner_parses_nudge_inject():
    provider = StubProvider(LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(
            id="c",
            name="planner_decision",
            arguments={
                "action": "nudge_inject",
                "reason": "user is planning travel; passport expiry is naturally additive",
                "proactivity_score": 0.85,
                "priority": "low",
                "target_session": "cli:direct",
                "nudge_message": "顺便提一下：你护照 2027-03 到期...",
            },
        )],
    ))
    decision = await ProactivePlanner(provider, "stub").decide(_make_context())
    assert decision.action == "nudge_inject"
    assert "护照" in (decision.nudge_message or "")
    assert decision.defer_condition is None


async def test_planner_downgrades_nudge_without_message():
    """nudge / nudge_inject / nudge_defer without nudge_message should become skip
    so the runner never has to fall back to reason-as-message."""
    provider = StubProvider(LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(
            id="c",
            name="planner_decision",
            arguments={
                "action": "nudge",
                "reason": "pretend nudge without a message",
                "proactivity_score": 0.8,
                "priority": "low",
                # nudge_message intentionally missing
            },
        )],
    ))
    decision = await ProactivePlanner(provider, "stub").decide(_make_context())
    assert decision.action == "skip"
    assert "missing nudge_message" in decision.reason
    # score survives so downstream dashboards still see the LLM was confident.
    assert decision.proactivity_score == 0.8


async def test_planner_downgrades_defer_without_condition():
    provider = StubProvider(LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(
            id="c",
            name="planner_decision",
            arguments={
                "action": "nudge_defer",
                "reason": "forgot the defer_condition",
                "proactivity_score": 0.7,
                "nudge_message": "有个事儿等你聊完再说",
                # defer_condition intentionally missing
            },
        )],
    ))
    decision = await ProactivePlanner(provider, "stub").decide(_make_context())
    assert decision.action == "skip"
    assert "missing defer_condition" in decision.reason


async def test_planner_downgrades_spawn_without_task():
    provider = StubProvider(LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(
            id="c",
            name="planner_decision",
            arguments={
                "action": "spawn_agent",
                "reason": "forgot the spawn_task",
                "proactivity_score": 0.9,
                # spawn_task intentionally missing
            },
        )],
    ))
    decision = await ProactivePlanner(provider, "stub").decide(_make_context())
    assert decision.action == "skip"
    assert "missing spawn_task" in decision.reason


async def test_planner_temperature_override():
    """Eval harness must be able to pin T=0 for reproducible runs."""
    captured = {}

    class RecordingProvider(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")

        async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                       temperature=0.7, reasoning_effort=None, tool_choice=None):
            captured["temperature"] = temperature
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="c", name="planner_decision",
                                             arguments={"action": "skip", "reason": "x",
                                                        "proactivity_score": 0.1})],
            )

        def get_default_model(self) -> str:
            return "stub"

    # Override to 0.0
    planner = ProactivePlanner(RecordingProvider(), "stub", temperature=0.0)
    await planner.decide(_make_context())
    assert captured["temperature"] == 0.0

    # No override → uses module default 0.3
    planner = ProactivePlanner(RecordingProvider(), "stub")
    await planner.decide(_make_context())
    assert captured["temperature"] == 0.3


async def test_planner_survives_llm_error():
    provider = StubProvider(LLMResponse(
        content="Error calling LLM: connection reset",
        finish_reason="error",
    ))
    decision = await ProactivePlanner(provider, "stub").decide(_make_context())
    assert decision.action == "skip"
    assert "llm_error" in decision.reason
