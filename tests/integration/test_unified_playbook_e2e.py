"""Whole-turn E2E for resolve -> Harness DAG -> compile -> save."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.subagent.dag_graph import DagNodeSpec
from raven.agent.tools.load_playbook import LoadPlaybookTool
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.config.schema import PlaybookConfig
from raven.playbook.agent_generator import EMIT_TOOL
from raven.playbook.agent_spec import AgentPlaybookSpec, DelegateEntry
from raven.playbook.store import PlaybookStore
from raven.playbook.unified import PlaybookMatch, UnifiedPlaybookSpec, WorkflowSpec
from raven.playbook.workflow_compiler import EMIT_WORKFLOW
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


def _names(tools: Any) -> set[str]:
    return {(tool.get("function", tool) or {}).get("name", "") for tool in tools or []}


class _WholeTurnProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.main_calls = 0
        self.setup_calls = 0
        self.compiler_calls = 0
        self.worker_calls = 0

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(messages, tools)

    async def chat_with_retry(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(messages, tools)

    async def _answer(self, messages, tools) -> LLMResponse:
        names = _names(tools)
        if EMIT_TOOL in names:
            self.setup_calls += 1
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="setup",
                        name=EMIT_TOOL,
                        arguments={
                            "description": "A reusable evidence workflow",
                            "disposition": "runtime_and_artifact",
                            "artifactName": "evidence-brief",
                            "captureWorkflow": True,
                            "workers": [
                                {
                                    "as": "researcher",
                                    "name": "Raven",
                                    "brief": "Return the requested evidence marker",
                                    "systemPrompt": "Return WORKER_EVIDENCE_OK when done.",
                                    "stopWhen": "WORKER_EVIDENCE_OK is returned",
                                }
                            ],
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        if EMIT_WORKFLOW in names:
            self.compiler_calls += 1
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="compile",
                        name=EMIT_WORKFLOW,
                        arguments={
                            "name": "evidence-brief",
                            "description": "Produce a concise evidence brief",
                            "match": {
                                "summary": "Produce an evidence brief",
                                "keywords": ["evidence brief", "source research"],
                            },
                            "inputSchema": {
                                "type": "object",
                                "properties": {},
                                "required": [],
                                "additionalProperties": False,
                            },
                            "workflow": {
                                "summary": "Build evidence brief",
                                "confirm": False,
                                "nodes": [
                                    {
                                        "id": "research",
                                        "subagent": "researcher",
                                        "nodeSummary": "Collect evidence",
                                        "promptTemplate": "Return WORKER_EVIDENCE_OK",
                                        "dependsOn": [],
                                    }
                                ],
                            },
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        if "run_subagent_dag" in names:
            self.main_calls += 1
            if self.main_calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="dag",
                            name="run_subagent_dag",
                            arguments={
                                "task_summary": "Build evidence brief",
                                "background": True,
                                "nodes": [
                                    {
                                        "id": "research",
                                        "subagent": "researcher",
                                        "node_summary": "Collect evidence",
                                        "prompt_template": "Return WORKER_EVIDENCE_OK",
                                        "depends_on": [],
                                    }
                                ],
                            },
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return LLMResponse(content="UNIFIED_PLAYBOOK_SAVED_OK", finish_reason="stop")
        self.worker_calls += 1
        return LLMResponse(content="WORKER_EVIDENCE_OK", finish_reason="stop")


@pytest.mark.asyncio
async def test_whole_turn_saves_a_composite_ready_playbook(tmp_path) -> None:
    playbook_root = tmp_path / "playbooks"
    provider = _WholeTurnProvider()
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=4),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
            playbook_config=PlaybookConfig(enabled=True, dir=str(playbook_root), agentHarness="generate"),
        ),
    )

    async def emit(*args, **kwargs) -> None:
        return None

    await loop.run_turn(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="unified", sender_id="user", chat_type=ChatType.DM),
            text="Research a topic and save this reusable evidence process",
            conversation="test:unified",
        ),
        emit,
        lambda: [],
        stream=False,
    )

    loaded = PlaybookStore(playbook_root).load("evidence-brief")
    assert isinstance(loaded, UnifiedPlaybookSpec)
    assert loaded.harness is not None and loaded.workflow is not None
    assert loaded.workflow.nodes[0].subagent == "researcher"
    assert provider.setup_calls == provider.compiler_calls == 1
    assert provider.worker_calls >= 1  # worker plus the optional DAG verdict call
    records = list((playbook_root / ".runs").glob("*.json"))
    assert len(records) == 1
    assert '"status": "completed"' in records[0].read_text(encoding="utf-8")


async def _emit(*args, **kwargs) -> None:
    return None


def _request(text: str, chat_id: str) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="test", chat_id=chat_id, sender_id="user", chat_type=ChatType.DM),
        text=text,
        conversation=f"test:{chat_id}",
    )


def _saved_composite() -> UnifiedPlaybookSpec:
    harness = AgentPlaybookSpec(
        name="evidence-brief",
        description="A citation-first evidence worker",
        delegate=[
            DelegateEntry(
                **{
                    "as": "researcher",
                    "name": "Raven",
                    "brief": "Collect primary evidence and return the marker",
                    "playbook": {"memory": {"systemPrompt": "Return WORKER_EVIDENCE_OK when complete."}},
                }
            )
        ],
    )
    return UnifiedPlaybookSpec(
        name="evidence-brief",
        description="Produce a concise evidence brief from primary sources",
        match=PlaybookMatch(
            summary="Produce an evidence brief",
            keywords=["evidence brief", "primary source research"],
        ),
        harness=harness,
        workflow=WorkflowSpec(
            summary="Build evidence brief",
            confirm=False,
            nodes=[
                DagNodeSpec(
                    id="research",
                    subagent="researcher",
                    nodeSummary="Collect evidence",
                    promptTemplate="Return WORKER_EVIDENCE_OK",
                )
            ],
        ),
    )


class _ReuseProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.setup_calls = 0
        self.main_calls = 0
        self.worker_calls = 0

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(tools)

    async def chat_with_retry(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(tools)

    async def _answer(self, tools) -> LLMResponse:
        names = _names(tools)
        if EMIT_TOOL in names:
            self.setup_calls += 1
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="select",
                        name=EMIT_TOOL,
                        arguments={
                            "description": "The saved evidence process is an exact match",
                            "disposition": "none",
                            "selectedPlaybook": "evidence-brief",
                            "captureWorkflow": False,
                            "workers": [],
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        if "load_playbook" in names:
            self.main_calls += 1
            if self.main_calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="load",
                            name="load_playbook",
                            arguments={"name": "evidence-brief", "params": {}},
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return LLMResponse(content="SAVED_PLAYBOOK_REUSED_OK", finish_reason="stop")
        self.worker_calls += 1
        return LLMResponse(content="WORKER_EVIDENCE_OK", finish_reason="stop")


@pytest.mark.asyncio
async def test_saved_composite_is_selected_and_executes_its_durable_harness(tmp_path) -> None:
    playbook_root = tmp_path / "playbooks"
    PlaybookStore(playbook_root).save(_saved_composite())
    provider = _ReuseProvider()
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=4),
        tools=ToolWiring(plugin_tools=[LoadPlaybookTool()], restrict_to_workspace=True),
        engine=EngineWiring(
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
            playbook_config=PlaybookConfig(enabled=True, dir=str(playbook_root), agentHarness="generate"),
        ),
    )

    await loop.run_turn(
        _request("Run my primary-source evidence brief for the launch", "reuse"),
        _emit,
        lambda: [],
        stream=False,
    )
    await asyncio.gather(*list(loop._playbooks.dag_tool._runs.values()), return_exceptions=True)

    assert provider.setup_calls == 1
    assert provider.main_calls >= 2
    assert provider.worker_calls >= 1
    assert len(list((playbook_root / ".runs").glob("*.json"))) == 1
    record = json.loads(next((playbook_root / ".runs").glob("*.json")).read_text(encoding="utf-8"))
    assert record["selectedPlaybook"] == "evidence-brief"
    assert "query" not in record and len(record["queryDigest"]) == 64


class _PersonaProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.setup_calls = 0
        self.main_calls = 0
        self.compiler_calls = 0

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(tools)

    async def chat_with_retry(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(tools)

    async def _answer(self, tools) -> LLMResponse:
        names = _names(tools)
        if EMIT_TOOL in names:
            self.setup_calls += 1
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="persona",
                        name=EMIT_TOOL,
                        arguments={
                            "description": "A skeptical claim-checking digital persona",
                            "disposition": "artifact",
                            "artifactName": "skeptical-fact-checker",
                            "captureWorkflow": False,
                            "workers": [
                                {
                                    "as": "fact-checker",
                                    "name": "Raven",
                                    "brief": "Challenge unsupported claims and require primary evidence",
                                    "systemPrompt": "Be skeptical, concise, and cite primary evidence.",
                                    "stopWhen": "Every material claim is supported or flagged",
                                }
                            ],
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        if EMIT_WORKFLOW in names:
            self.compiler_calls += 1
            raise AssertionError("a Harness-only persona must not compile a Workflow")
        self.main_calls += 1
        return LLMResponse(content="PERSONA_CREATED_WITHOUT_ODD_DAG_OK", finish_reason="stop")


@pytest.mark.asyncio
async def test_digital_persona_saves_harness_only_without_inventing_a_dag(tmp_path) -> None:
    playbook_root = tmp_path / "playbooks"
    provider = _PersonaProvider()
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=3),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
            playbook_config=PlaybookConfig(enabled=True, dir=str(playbook_root), agentHarness="generate"),
        ),
    )

    await loop.run_turn(
        _request("Create a skeptical fact-checking digital persona; do not run a process", "persona"),
        _emit,
        lambda: [],
        stream=False,
    )

    saved = PlaybookStore(playbook_root).load("skeptical-fact-checker")
    assert isinstance(saved, UnifiedPlaybookSpec)
    assert saved.harness is not None
    assert saved.workflow is None
    assert saved.harness.delegate[0].brief.startswith("Challenge unsupported")
    assert provider.setup_calls == provider.main_calls == 1
    assert provider.compiler_calls == 0


class _OffProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.setup_calls = 0
        self.main_calls = 0

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(tools)

    async def chat_with_retry(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(tools)

    async def _answer(self, tools) -> LLMResponse:
        if EMIT_TOOL in _names(tools):
            self.setup_calls += 1
            raise AssertionError("the disabled Playbook feature made a setup model call")
        self.main_calls += 1
        return LLMResponse(content="OFF_PATH_USES_DEFAULT_ROSTER_OK", finish_reason="stop")


@pytest.mark.asyncio
async def test_master_switch_off_has_no_resolution_record_or_library_side_effect(tmp_path) -> None:
    playbook_root = tmp_path / "playbooks"
    provider = _OffProvider()
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
            playbook_config=PlaybookConfig(enabled=False, dir=str(playbook_root), agentHarness="generate"),
        ),
    )

    await loop.run_turn(
        _request("Answer normally with Playbooks disabled", "off"),
        _emit,
        lambda: [],
        stream=False,
    )

    assert loop._playbooks is None
    assert provider.setup_calls == 0
    assert provider.main_calls == 1
    assert not playbook_root.exists()
