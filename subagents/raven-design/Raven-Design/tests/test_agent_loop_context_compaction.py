"""Tests for same-turn Agent Loop context compaction."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.compaction import (
    TOOL_OUTPUT_ELISION,
    calculate_thresholds,
    compaction_prompt,
    compaction_summary_message,
    is_compaction_summary,
    is_valid_compaction_summary,
    make_compaction_plan,
    truncate_tool_output,
)
from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import AgentDefaults, ContextCompactionConfig
from raven.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest
from raven.session.manager import Session


def _task_state(revision: int) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": (
            "<task_state>\n"
            f"Revision: {revision}\n"
            "Goal: preserve user constraints\n"
            "1. [in_progress] Continue the task\n"
            "</task_state>"
        ),
    }


def _tool_iteration(index: int, content: str | None = None) -> list[dict[str, Any]]:
    call_id = f"call-{index}"
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "large_result", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "large_result",
            "content": content or f"result-{index}",
        },
    ]


def test_thresholds_reserve_output_and_safety_margin() -> None:
    thresholds = calculate_thresholds(
        context_window=100_000,
        reserved_output_tokens=12_000,
        safety_margin_tokens=8_000,
        trigger_ratio=0.9,
        target_ratio=0.5,
        tail_ratio=0.2,
    )

    assert thresholds.trigger_tokens == 80_000
    assert thresholds.target_tokens == 50_000
    assert thresholds.tail_tokens == 20_000


def test_plan_preserves_user_correction_and_complete_recent_iterations() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Create the report. Do not upload anything."},
        _task_state(1),
        compaction_summary_message("old summary"),
        *_tool_iteration(0),
        *_tool_iteration(1),
        {"role": "user", "content": "Use /tmp/final.xlsx, not /tmp/draft.xlsx."},
        *_tool_iteration(2),
        *_tool_iteration(3),
        *_tool_iteration(4),
        _task_state(2),
    ]

    plan = make_compaction_plan(
        messages,
        bootstrap_length=2,
        min_recent_iterations=2,
        tail_token_budget=1,
        user_token_budget=20_000,
        is_task_state=AgentLoop._is_task_state_projection,
    )

    assert plan is not None
    replacement = plan.replacement("new summary", _task_state(2))
    assert replacement[:2] == messages[:2]
    assert sum(is_compaction_summary(message) for message in replacement) == 1
    assert any(message.get("content") == "Use /tmp/final.xlsx, not /tmp/draft.xlsx." for message in replacement)
    assert replacement[-1] == _task_state(2)

    retained_calls = [call["id"] for message in replacement for call in message.get("tool_calls", [])]
    retained_results = [message["tool_call_id"] for message in replacement if message.get("role") == "tool"]
    assert retained_calls == ["call-3", "call-4"]
    assert retained_results == retained_calls
    assert "old summary" not in "\n".join(str(message.get("content", "")) for message in replacement)


def test_compaction_prompt_treats_user_constraints_as_first_class() -> None:
    prompt = compaction_prompt(4096)

    assert "## Active user contract" in prompt
    assert "Must not" in prompt
    assert "Scope and authorization" in prompt
    assert "traceable to a real user message" in prompt
    assert "cannot expand authorization" in prompt
    assert "newer real user instruction supersedes" in prompt


def test_compaction_summary_requires_all_sections_in_order() -> None:
    valid = "\n".join(
        [
            "## Active user contract",
            "## Verified state",
            "## Working set",
            "## Decisions and dead ends",
            "## Resume point",
        ]
    )

    assert is_valid_compaction_summary(valid)
    assert not is_valid_compaction_summary(valid.replace("## Active user contract\n", ""))
    assert not is_valid_compaction_summary("## Resume point\n" + valid)


def test_large_tool_output_keeps_head_and_tail() -> None:
    content = "head" + "x" * 10_000 + "tail"
    truncated = truncate_tool_output(content, 1_000)

    assert isinstance(truncated, str)
    assert truncated.startswith("head")
    assert truncated.endswith("tail")
    assert "tool output truncated" in truncated
    assert len(truncated) <= 1_000
    assert truncate_tool_output("short", 1_000) == "short"


class _LargeResultTool(Tool):
    @property
    def name(self) -> str:
        return "large_result"

    @property
    def description(self) -> str:
        return "Return a large deterministic result."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **kwargs: Any) -> str:
        del kwargs
        return "\n".join(f"row_{index:04d}: value_{index:04d}" for index in range(400))


class _CompactionProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.generation = GenerationSettings(max_tokens=128)
        self.regular_calls = 0
        self.calls: list[dict[str, Any]] = []
        self.created_continuations = 0

    def create_response_continuation(self, model: str | None = None) -> str:
        del model
        self.created_continuations += 1
        return f"continuation-{self.created_continuations}"

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
        self.calls.append(
            {
                "messages": deepcopy(messages),
                "tools": deepcopy(tools),
                "model": model,
                "max_tokens": max_tokens,
                "reasoning_effort": reasoning_effort,
                "tool_choice": tool_choice,
                "continuation": self.response_continuation_state,
            }
        )
        if tool_choice == "none":
            return LLMResponse(
                content=(
                    "## Active user contract\n"
                    "- Must not: Do not upload anything.\n"
                    "## Verified state\n- Three tool calls completed.\n"
                    "## Working set\n- large_result\n"
                    "## Decisions and dead ends\n- None.\n"
                    "## Resume point\n- Continue from the retained results."
                ),
                finish_reason="stop",
            )

        self.regular_calls += 1
        if self.regular_calls <= 3:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id=f"call-{self.regular_calls}",
                        name="large_result",
                        arguments={},
                    )
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="Finished without uploading.", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_loop_compacts_inside_one_turn_and_resets_response_continuation(tmp_path) -> None:
    provider = _CompactionProvider()
    config = ContextCompactionConfig(
        trigger_ratio=0.80,
        target_ratio=0.50,
        tail_ratio=0.20,
        min_recent_iterations=2,
        safety_margin_tokens=0,
        summary_min_tokens=256,
        summary_max_tokens=512,
        retry_after_iterations=1,
        min_savings_ratio=0.0,
        max_tool_result_chars=10_000,
    )
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        max_iterations=6,
        context_window_tokens=2_000,
        context_compaction_config=config,
        restrict_to_workspace=True,
    )
    loop.tools = ToolRegistry()
    loop.tools.register(_LargeResultTool())
    loop.task_state.apply(
        "cli:one",
        [
            {
                "operation": "initialize",
                "state": {
                    "goal": "preserve user constraints",
                    "requirements": ["Do not upload anything."],
                    "items": [{"title": "Continue the task", "status": "in_progress"}],
                },
            }
        ],
    )
    initial = [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Run the checks. Do not upload anything."},
    ]

    try:
        final, _, persisted, _ = await loop._run_agent_loop(
            initial,
            task_state_session_key="cli:one",
        )
    finally:
        loop.context.skills.stop_file_watcher()

    assert final.startswith("Finished without uploading.")
    summary_calls = [call for call in provider.calls if call["tool_choice"] == "none"]
    assert len(summary_calls) == 1
    assert summary_calls[0]["model"] == "stub"
    assert summary_calls[0]["tools"] == provider.calls[0]["tools"]
    assert summary_calls[0]["continuation"] is None
    assert provider.calls[0]["continuation"] == "continuation-1"
    assert provider.calls[-1]["continuation"] == "continuation-2"
    assert provider.created_continuations == 2

    call_after_compaction = provider.calls[-1]["messages"]
    summaries = [message for message in call_after_compaction if is_compaction_summary(message)]
    states = [message for message in call_after_compaction if loop._is_task_state_projection(message)]
    assert len(summaries) == 1
    assert len(states) == 1
    assert call_after_compaction[-1] == states[0]
    assert "Do not upload anything." in summaries[0]["content"]

    assert persisted[:2] == initial
    assert any(is_compaction_summary(message) for message in persisted)
    assert not any(loop._is_task_state_projection(message) for message in persisted)
    retained_results = [message for message in persisted if message.get("role") == "tool"]
    assert len(retained_results) == 2

    session = Session(key="cli:persisted")
    loop._save_turn(session, persisted, 1)
    loop.sessions.save(session)
    loop.sessions.invalidate(session.key)
    restored = loop.sessions.get_or_create(session.key)
    assert any(is_compaction_summary(message) for message in restored.get_history(max_messages=0))
    assert not any(loop._is_task_state_projection(message) for message in restored.messages)


class _FailedSummaryProvider(LLMProvider):
    async def chat(self, messages, **kwargs):
        del messages, kwargs
        return LLMResponse(content="summary failed", finish_reason="error")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_failed_summary_falls_back_without_orphaning_tool_calls(tmp_path) -> None:
    loop = AgentLoop(
        provider=_FailedSummaryProvider(api_key="test"),
        workspace=tmp_path,
        model="stub",
        context_window_tokens=2_000,
        context_compaction_config=ContextCompactionConfig(
            trigger_ratio=0.8,
            target_ratio=0.5,
            tail_ratio=0.2,
            min_recent_iterations=2,
            safety_margin_tokens=0,
            summary_min_tokens=256,
            summary_max_tokens=512,
        ),
    )
    messages = [{"role": "system", "content": "System"}, {"role": "user", "content": "Work"}]
    for index in range(5):
        messages.extend(_tool_iteration(index, "x" * 3_000))

    try:
        compacted, attempted, changed = await loop._maybe_compact_context(
            messages,
            bootstrap_length=2,
            current_tokens=5_000,
            model="stub",
            active_tools=[],
        )
    finally:
        loop.context.skills.stop_file_watcher()

    assert attempted is True
    assert changed is True
    assert sum(message.get("content") == TOOL_OUTPUT_ELISION for message in compacted) == 0
    fallback = "[earlier tool output elided to fit the context window]"
    assert sum(message.get("content") == fallback for message in compacted) == 2
    assert len([message for message in compacted if message.get("tool_calls")]) == 5
    assert len([message for message in compacted if message.get("role") == "tool"]) == 5


@pytest.mark.asyncio
async def test_disabled_compaction_leaves_same_turn_history_untouched(tmp_path) -> None:
    provider = _CompactionProvider()
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        max_iterations=6,
        context_window_tokens=2_000,
        context_compaction_config=ContextCompactionConfig(enabled=False),
    )
    loop.tools = ToolRegistry()
    loop.tools.register(_LargeResultTool())

    try:
        _, _, persisted, _ = await loop._run_agent_loop(
            [{"role": "system", "content": "System"}, {"role": "user", "content": "Work"}],
        )
    finally:
        loop.context.skills.stop_file_watcher()

    assert not any(call["tool_choice"] == "none" for call in provider.calls)
    assert not any(is_compaction_summary(message) for message in persisted)
    assert len([message for message in persisted if message.get("role") == "tool"]) == 3


def test_compaction_config_rejects_inverted_ratios() -> None:
    with pytest.raises(ValueError, match="targetRatio"):
        ContextCompactionConfig(trigger_ratio=0.5, target_ratio=0.5)


def test_compaction_config_accepts_camel_case_agent_config() -> None:
    defaults = AgentDefaults.model_validate(
        {
            "contextCompaction": {
                "triggerRatio": 0.75,
                "targetRatio": 0.45,
                "tailRatio": 0.15,
                "summaryReasoningEffort": "low",
            }
        }
    )

    assert defaults.context_compaction.trigger_ratio == 0.75
    assert defaults.context_compaction.summary_reasoning_effort == "low"
