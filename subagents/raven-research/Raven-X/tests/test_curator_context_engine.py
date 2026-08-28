from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.config import ContextConfig
from raven.context_engine import ContextAssembler, TurnContext
from raven.context_engine.segments.curator import CuratorSegmentBuilder
from raven.memory_engine.base import TokenBudget
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class CuratorScriptProvider(LLMProvider):
    def __init__(self, *, curator_mode: str = "slow"):
        super().__init__(api_key="test")
        self.curator_mode = curator_mode
        self.curator_calls = 0
        self.main_calls = 0

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
        tool_names = {tool.get("function", {}).get("name") for tool in (tools or []) if isinstance(tool, dict)}
        if "curator_build_context" in tool_names:
            self.curator_calls += 1
            if self.curator_mode == "fallback":
                return LLMResponse(content="I cannot decide.", tool_calls=[])
            if self.curator_calls == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCallRequest(
                            id="curator_archive_1",
                            name="curator_archive_messages",
                            arguments={
                                "message_ids": [0, 1],
                                "reason": "old context",
                                "tags": ["old"],
                                "summary": "old setup",
                            },
                        )
                    ],
                )
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="curator_build_1",
                        name="curator_build_context",
                        arguments={
                            "include_message_ids": [0, 1, 2, 3],
                            "working_state_injection": "Keep project setup and latest request.",
                            "notes": "test plan",
                        },
                    )
                ],
            )

        self.main_calls += 1
        return LLMResponse(content="main done")

    def get_default_model(self) -> str:
        return "fake-main"


def _session_messages() -> list[dict]:
    return [
        {"role": "user", "content": "Initial project rule: preserve exact config.", "timestamp": "2026-05-12T10:00:00"},
        {"role": "assistant", "content": "Noted the config preservation rule.", "timestamp": "2026-05-12T10:01:00"},
        {"role": "user", "content": "Now design curator context management.", "timestamp": "2026-05-12T11:00:00"},
        {
            "role": "assistant",
            "content": "We should use manifest plus selective retrieval.",
            "timestamp": "2026-05-12T11:01:00",
        },
    ]


def _budget() -> TokenBudget:
    return TokenBudget(
        context_length=4096,
        reserved_output=512,
        reserved_tools=100,
        reserved_system=500,
        available_history=2984,
    )


def test_agentloop_uses_curator_and_keeps_internal_tools_private(tmp_path: Path):
    loop = AgentLoop(
        provider=CuratorScriptProvider(),
        workspace=tmp_path,
        context_config=ContextConfig(engine="curator", fast_path_threshold=0.0),
    )

    assert loop.context_engine.name == "context_assembler"
    assert loop.context_engine.owns_compaction is True
    assert isinstance(loop.context_engine, ContextAssembler)
    assert not any(name.startswith("curator_") for name in loop.tools.tool_names)


@pytest.mark.asyncio
async def test_curator_slow_path_archives_and_writes_trace(tmp_path: Path):
    provider = CuratorScriptProvider(curator_mode="slow")
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        context_config=ContextConfig(engine="curator", fast_path_threshold=0.0),
    )

    assembled = await loop.context_engine.assemble(
        "cli:curator-test",
        _session_messages(),
        _budget(),
        turn=TurnContext(current_message="Please continue the curator design.", channel="cli", chat_id="curator-test"),
    )

    assert assembled.metadata["path"] == "slow"
    assert provider.curator_calls == 2
    assert "Curator Working State" in assembled.messages[0]["content"]
    trace_path = Path(assembled.metadata["trace_path"])
    assert trace_path.exists()
    trace_text = trace_path.read_text(encoding="utf-8")
    assert "curator_archive_messages" in trace_text
    assert "slow_path_accepted" in trace_text

    manifest = json.loads((tmp_path / "memory/.curator/manifest/cli_curator-test.json").read_text(encoding="utf-8"))
    archived = [item for item in manifest["items"] if item["archived"]]
    assert [item["id"] for item in archived] == [0, 1]
    assert list((tmp_path / "memory/.curator/archive").glob("**/*.jsonl"))


@pytest.mark.asyncio
async def test_curator_fallback_when_internal_agent_does_not_finish(tmp_path: Path):
    provider = CuratorScriptProvider(curator_mode="fallback")
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        context_config=ContextConfig(engine="curator", fast_path_threshold=0.0),
    )

    assembled = await loop.context_engine.assemble(
        "cli:fallback-test",
        _session_messages(),
        _budget(),
        turn=TurnContext(current_message="Continue.", channel="cli", chat_id="fallback-test"),
    )

    assert assembled.metadata["path"] == "fallback"
    assert assembled.messages[0]["role"] == "system"
    assert assembled.messages[-1]["role"] == "user"
    assert "Continue." in assembled.messages[-1]["content"]


@pytest.mark.asyncio
async def test_process_message_records_main_and_curator_trajectories(tmp_path: Path):
    provider = CuratorScriptProvider(curator_mode="slow")
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        context_config=ContextConfig(engine="curator", fast_path_threshold=0.0),
    )
    session = loop.sessions.get_or_create("cli:trace-test")
    session.messages.extend(_session_messages())
    loop.sessions.save(session)

    response = await loop._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(
                channel="cli",
                chat_id="trace-test",
                sender_id="user",
                chat_type=ChatType.DM,
            ),
            text="Use the curator trajectory and answer.",
        )
    )

    assert response is not None
    assert response[0] == "main done"
    traces = list((tmp_path / "memory/.curator/traces/cli_trace-test").glob("*.jsonl"))
    assert len(traces) == 1
    trace_text = traces[0].read_text(encoding="utf-8")
    assert "curator_llm_request" in trace_text
    assert "main_agent_result" in trace_text


def test_history_from_messages_preserves_reasoning_fields():
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_content": "chain of thought",
            "thinking_blocks": [{"thinking": "block"}],
        },
    ]

    history = CuratorSegmentBuilder._history_from_messages(messages)

    assert history[1]["reasoning_content"] == "chain of thought"
    assert history[1]["thinking_blocks"] == [{"thinking": "block"}]
