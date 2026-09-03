from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring
from raven.config import ContextConfig
from raven.context_engine import ContextAssembler, TurnContext
from raven.context_engine.segments.curator import CuratorSegmentBuilder
from raven.contracts.assembled import TokenBudget
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
                    finish_reason="length" if self.curator_mode == "truncated" else "tool_calls",
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
        engine=EngineWiring(context_config=ContextConfig(fast_path_threshold=0.0)),
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
        engine=EngineWiring(context_config=ContextConfig(fast_path_threshold=0.0)),
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
async def test_curator_does_not_dispatch_a_truncated_call(tmp_path: Path):
    """The curator's loop is a second agent loop, and archiving is destructive.

    A cut-off `curator_archive_messages` has an incomplete `message_ids` list
    and nothing about it says so -- the arguments that did arrive validate.
    `chat_with_retry` reaches the same verdict here as it does for the main
    loop; the dispatch has to honour it.
    """
    provider = CuratorScriptProvider(curator_mode="truncated")
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        engine=EngineWiring(context_config=ContextConfig(fast_path_threshold=0.0)),
    )

    assembled = await loop.context_engine.assemble(
        "cli:curator-cut",
        _session_messages(),
        _budget(),
        turn=TurnContext(current_message="Please continue the curator design.", channel="cli", chat_id="curator-cut"),
    )

    assert assembled.metadata["path"] == "slow"
    manifest = json.loads((tmp_path / "memory/.curator/manifest/cli_curator-cut.json").read_text(encoding="utf-8"))
    assert [item for item in manifest["items"] if item["archived"]] == []

    trace_text = Path(assembled.metadata["trace_path"]).read_text(encoding="utf-8")
    assert "[truncated]" in trace_text


@pytest.mark.asyncio
async def test_curator_fallback_when_internal_agent_does_not_finish(tmp_path: Path):
    provider = CuratorScriptProvider(curator_mode="fallback")
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        engine=EngineWiring(context_config=ContextConfig(fast_path_threshold=0.0)),
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
        engine=EngineWiring(context_config=ContextConfig(fast_path_threshold=0.0)),
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


# --- pinned skill bodies -------------------------------------------------

_GUIDE_MARKER = "PINNED-GUIDE-BODY-MARKER"


def _use_skill_exchange(call_id: str, skill_id: str, body: str) -> list[dict]:
    """The two messages a `use_skill` fetch leaves in the session log."""
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "use_skill", "arguments": json.dumps({"skill_id": skill_id})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "name": "use_skill", "content": body},
    ]


def _session_with_guide_fetch() -> list[dict]:
    return [
        {"role": "user", "content": "Set up the project."},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "Now orchestrate three sub-agents."},
        *_use_skill_exchange("call_guide", "local/subagent-dag-orchestration", _GUIDE_MARKER),
        {"role": "assistant", "content": "Read the guide."},
    ]


class _PlanOmittingPinsProvider(CuratorScriptProvider):
    """A curator that builds a plan naming only the first exchange — the shape
    that silently loses a mid-session skill body."""

    async def chat(self, messages, tools=None, **kwargs):
        tool_names = {tool.get("function", {}).get("name") for tool in (tools or []) if isinstance(tool, dict)}
        if "curator_build_context" in tool_names:
            self.curator_calls += 1
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="build_1",
                        name="curator_build_context",
                        arguments={"include_message_ids": [0, 1], "notes": "recent only"},
                    )
                ],
            )
        self.main_calls += 1
        return LLMResponse(content="main done")


def test_pinned_scan_takes_the_whole_exchange_and_supersedes_earlier_fetches() -> None:
    """Half a tool exchange is a dangling tool_call, and a re-read must move the
    pin rather than add a second copy of the same body."""
    from raven.context_engine.curator import pinned_message_ids

    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "use_skill", "arguments": '{"skill_id": "local/guide"}'},
                },
                {"id": "c2", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "use_skill", "content": "body"},
        {"role": "tool", "tool_call_id": "c2", "name": "grep", "content": "hits"},
        {"role": "user", "content": "again"},
        *_use_skill_exchange("c3", "local/guide", "body v2"),
    ]

    pinned = pinned_message_ids(messages, ["local/guide"])

    # Only the latest fetch (5, 6) — and the sibling grep result of the earlier
    # turn is not pinned because that whole turn was superseded.
    assert pinned == {5, 6}


def test_pinned_scan_covers_sibling_results_of_the_pinned_turn() -> None:
    from raven.context_engine.curator import pinned_message_ids

    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "use_skill", "arguments": '{"skill_id": "local/guide"}'},
                },
                {"id": "c2", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "use_skill", "content": "body"},
        {"role": "tool", "tool_call_id": "c2", "name": "grep", "content": "hits"},
    ]

    # The sibling result comes along: the trimmer drops ids without re-closing
    # adjacency, so an unpinned sibling could leave the pinned parent dangling.
    assert pinned_message_ids(messages, ["local/guide"]) == {1, 2, 3}


@pytest.mark.parametrize(
    "tool_calls",
    [
        [{"id": "c", "type": "function", "function": {"name": "use_skill", "arguments": "{not json"}}],
        [
            {
                "id": "c",
                "type": "function",
                "function": {"name": "use_skill", "arguments": '{"skill_id": "local/other"}'},
            }
        ],
        [
            {
                "id": "c",
                "type": "function",
                "function": {"name": "web_fetch", "arguments": '{"skill_id": "local/guide"}'},
            }
        ],
        ["not-a-dict"],
    ],
)
def test_pinned_scan_pins_nothing_it_cannot_positively_identify(tool_calls) -> None:
    """Manifest building must never raise on a malformed stored tool_call, and a
    fetch of some other skill is not this skill's pin."""
    from raven.context_engine.curator import pinned_message_ids

    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": tool_calls},
        {"role": "tool", "tool_call_id": "c", "name": "use_skill", "content": "body"},
    ]

    assert pinned_message_ids(messages, ["local/guide"]) == set()


def test_pinned_scan_is_off_when_no_ids_are_configured() -> None:
    from raven.context_engine.curator import pinned_message_ids

    messages = _session_with_guide_fetch()
    assert pinned_message_ids(messages, []) == set()
    assert pinned_message_ids(messages, None) == set()


@pytest.mark.asyncio
async def test_pinned_guide_body_survives_a_plan_that_omitted_it(tmp_path: Path):
    """The point of the pin: the curator's plan names only ids 0-1, yet the
    fetched body still reaches the main agent."""
    loop = AgentLoop(
        provider=_PlanOmittingPinsProvider(),
        workspace=tmp_path,
        engine=EngineWiring(context_config=ContextConfig(fast_path_threshold=0.0)),
    )

    assembled = await loop.context_engine.assemble(
        "cli:pin-test",
        _session_with_guide_fetch(),
        _budget(),
        turn=TurnContext(current_message="Build the DAG now.", channel="cli", chat_id="pin-test"),
    )

    assert assembled.metadata["path"] == "slow"
    rendered = json.dumps(assembled.messages, ensure_ascii=False)
    assert _GUIDE_MARKER in rendered
    # And the pin is what did it: the same plan drops the body with pinning off.
    off = AgentLoop(
        provider=_PlanOmittingPinsProvider(),
        workspace=tmp_path / "off",
        engine=EngineWiring(context_config=ContextConfig(fast_path_threshold=0.0, pinned_skill_ids=[])),
    )
    assembled_off = await off.context_engine.assemble(
        "cli:pin-test",
        _session_with_guide_fetch(),
        _budget(),
        turn=TurnContext(current_message="Build the DAG now.", channel="cli", chat_id="pin-test"),
    )
    assert _GUIDE_MARKER not in json.dumps(assembled_off.messages, ensure_ascii=False)


@pytest.mark.asyncio
async def test_pinned_ids_are_recorded_in_the_turn_trace(tmp_path: Path):
    """The trace is the only way to explain after the fact why an id stayed."""
    loop = AgentLoop(
        provider=_PlanOmittingPinsProvider(),
        workspace=tmp_path,
        engine=EngineWiring(context_config=ContextConfig(fast_path_threshold=0.0)),
    )

    assembled = await loop.context_engine.assemble(
        "cli:pin-trace",
        _session_with_guide_fetch(),
        _budget(),
        turn=TurnContext(current_message="Build the DAG now.", channel="cli", chat_id="pin-trace"),
    )

    trace = Path(assembled.metadata["trace_path"]).read_text(encoding="utf-8")
    assert '"pinned_message_ids": [3, 4]' in trace


def test_archiving_a_pinned_id_is_refused(tmp_path: Path) -> None:
    """Archiving marks a message archived and halves its relevance, which is the
    slow path's cue to stop selecting it — a pin has to survive that too."""
    from raven.context_engine.curator import CuratorArchiveStore

    config = ContextConfig()
    store = CuratorArchiveStore(tmp_path, config)
    messages = _session_with_guide_fetch()
    manifest = store.build_manifest("cli:pin-archive", messages)
    assert [item.id for item in manifest if item.pinned] == [3, 4]

    result = store.archive_messages("cli:pin-archive", manifest, messages, [3, 4, 5], reason="old")

    assert result["refused_pinned_ids"] == [3, 4]
    assert result["archived_message_ids"] == [5]
    assert [item.id for item in manifest if item.archived] == [5]


def test_pinned_items_carry_a_relevance_floor(tmp_path: Path) -> None:
    """So the slow path's own ranking never argues against the pin."""
    from raven.context_engine.curator import CuratorArchiveStore

    store = CuratorArchiveStore(tmp_path, ContextConfig())
    manifest = store.build_manifest("cli:pin-rel", _session_with_guide_fetch())

    assert all(item.relevance >= 0.9 for item in manifest if item.pinned)


def test_the_default_pinned_skill_is_the_id_the_dag_tool_advertises() -> None:
    """The config default and the tool's pointer are the same string in two
    layers; this is the seam that catches them drifting apart."""
    from raven.agent.subagent.dag_tool import GUIDE_SKILL_ID

    assert ContextConfig().pinned_skill_ids == [GUIDE_SKILL_ID]


@pytest.mark.asyncio
async def test_pinned_ids_are_also_protected_from_budget_trimming(tmp_path: Path, monkeypatch):
    """Inclusion alone is not enough: the trimmer drops the lowest-priority
    non-protected ids until the prompt fits, so a pinned id that were merely
    included would still be the first thing to go under pressure."""
    from raven.context_engine.history_trimmer import HistoryTrimmer

    seen: dict[str, object] = {}
    original = HistoryTrimmer.trim

    def _spy(self, **kwargs):
        seen.update(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(HistoryTrimmer, "trim", _spy)

    loop = AgentLoop(
        provider=_PlanOmittingPinsProvider(),
        workspace=tmp_path,
        engine=EngineWiring(context_config=ContextConfig(fast_path_threshold=0.0)),
    )
    await loop.context_engine.assemble(
        "cli:pin-protect",
        _session_with_guide_fetch(),
        _budget(),
        turn=TurnContext(current_message="Build the DAG now.", channel="cli", chat_id="pin-protect"),
    )

    assert {3, 4} <= seen["protected_ids"]
    assert {3, 4} <= set(seen["ids"])
