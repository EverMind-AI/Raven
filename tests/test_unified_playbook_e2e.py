"""Whole-turn E2E for resolve -> Harness DAG -> compile -> save."""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, SubagentWiring, ToolWiring, TurnPolicy
from raven.agent.subagent.dag_graph import DagNodeSpec
from raven.agent.tools.load_playbook import LoadPlaybookTool
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.config.schema import BuiltinAgentConfig, PlaybookConfig
from raven.playbook.agent_generator import PERSONA_TOOL, TASK_TOOL
from raven.playbook.agent_spec import AgentPlaybookSpec, DelegateEntry
from raven.playbook.store import PlaybookStore
from raven.playbook.unified import PlaybookMatch, UnifiedPlaybookSpec, WorkflowSpec
from raven.playbook.workflow_compiler import EMIT_WORKFLOW
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


def _saved(root: Any) -> list[str]:
    """The names a reader kept, which is the user layer alone.

    ``list_ids`` unions that layer with the packaged one, so a builtin playbook
    nobody saved would otherwise read as evidence that the turn wrote something.
    """
    if not root.exists():
        return []
    store = PlaybookStore(root)
    return [name for name in store.list_ids() if store.origin_of(name) == "user"]


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
        if TASK_TOOL in names:
            self.setup_calls += 1
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="setup",
                        name=TASK_TOOL,
                        arguments={
                            "description": "A reusable evidence workflow",
                            "artifactName": "evidence-brief",
                            "workers": [
                                {
                                    "as": "researcher",
                                    "agent": "Raven",
                                    "prompt": "Return WORKER_EVIDENCE_OK after collecting the requested evidence",
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
                            "name": "model-tried-to-rename-the-artifact",
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


def _request(text: str, chat_id: str, *, playbook_mode: str | None = None) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="test", chat_id=chat_id, sender_id="user", chat_type=ChatType.DM),
        text=text,
        conversation=f"test:{chat_id}",
        playbook_mode=playbook_mode,
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
        if TASK_TOOL in names or PERSONA_TOOL in names:
            self.setup_calls += 1
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="select",
                        name=TASK_TOOL,
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
async def test_off_mode_skips_generation_but_keeps_explicit_playbook_loading(tmp_path) -> None:
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
        _request("Run my primary-source evidence brief for the launch", "reuse", playbook_mode="off"),
        _emit,
        lambda: [],
        stream=False,
    )
    await asyncio.gather(*list(loop._playbooks.dag_tool._runs.values()), return_exceptions=True)

    assert provider.setup_calls == 0
    assert provider.main_calls >= 2
    assert provider.worker_calls >= 1
    assert not (playbook_root / ".runs").exists()


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
        if PERSONA_TOOL in names:
            self.setup_calls += 1
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="persona",
                        name=PERSONA_TOOL,
                        arguments={
                            "description": "A skeptical claim-checking digital persona",
                            "artifactName": "skeptical-fact-checker",
                            "coordinator": {
                                "brief": "Own the claim-checking conversation and synthesize specialist findings",
                                "systemPrompt": "Be skeptical, concise, and cite primary evidence.",
                            },
                            "workers": [],
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
async def test_digital_persona_is_a_draft_until_it_is_saved(tmp_path) -> None:
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
        _request(
            "Create a skeptical fact-checking digital persona; do not run a process",
            "persona",
            playbook_mode="persona",
        ),
        _emit,
        lambda: [],
        stream=False,
    )

    # The turn writes nothing: a generated Persona is an answer on screen, and
    # the library is what a reader decided to keep.
    assert _saved(playbook_root) == []
    draft = loop.session_draft("test:persona")
    assert draft is not None
    assert draft.name == "skeptical-fact-checker"

    assert loop.save_session_draft("test:persona") == "skeptical-fact-checker"
    assert loop.session_draft("test:persona") is None

    saved = PlaybookStore(playbook_root).load("skeptical-fact-checker")
    assert isinstance(saved, UnifiedPlaybookSpec)
    assert saved.harness is not None
    assert saved.workflow is None
    assert saved.harness.coordinator is not None
    assert saved.harness.coordinator.brief.startswith("Own the claim-checking conversation")
    assert saved.harness.delegate == []
    assert provider.setup_calls == provider.main_calls == 1
    assert provider.compiler_calls == 0


@pytest.mark.asyncio
async def test_saving_two_drafts_under_one_name_gets_a_deterministic_numeric_suffix(tmp_path) -> None:
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

    for chat_id in ("persona-one", "persona-two"):
        await loop.run_turn(
            _request("Create the same skeptical fact-checking persona", chat_id, playbook_mode="persona"),
            _emit,
            lambda: [],
            stream=False,
        )
        loop.save_session_draft(f"test:{chat_id}")

    assert _saved(playbook_root) == ["skeptical-fact-checker", "skeptical-fact-checker-2"]
    suffixed = PlaybookStore(playbook_root).load("skeptical-fact-checker-2")
    assert isinstance(suffixed, UnifiedPlaybookSpec)
    assert suffixed.harness is not None and suffixed.harness.name == "skeptical-fact-checker-2"


class _TaskWithoutDagProvider(LLMProvider):
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
        if TASK_TOOL in names:
            self.setup_calls += 1
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="task-harness",
                        name=TASK_TOOL,
                        arguments={
                            "artifactName": "concise-answer-task",
                            "description": "Answer a bounded question concisely",
                            "workers": [{"agent": "Raven", "prompt": "Answer concisely with cited facts"}],
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        if EMIT_WORKFLOW in names:
            self.compiler_calls += 1
            raise AssertionError("a Task without a successful DAG must not compile a Workflow")
        self.main_calls += 1
        return LLMResponse(content="TASK_FINISHED_WITHOUT_DAG_OK", finish_reason="stop")


@pytest.mark.asyncio
async def test_task_without_a_dag_keeps_the_immediately_saved_harness(tmp_path) -> None:
    playbook_root = tmp_path / "playbooks"
    provider = _TaskWithoutDagProvider()
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
            playbook_config=PlaybookConfig(enabled=True, dir=str(playbook_root), agentHarness="generate"),
        ),
    )

    await loop.run_turn(
        _request("Answer this bounded question without delegating", "task-no-dag"),
        _emit,
        lambda: [],
        stream=False,
    )

    saved = PlaybookStore(playbook_root).load("concise-answer-task")
    assert isinstance(saved, UnifiedPlaybookSpec)
    assert saved.harness is not None and saved.workflow is None
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
        if {TASK_TOOL, PERSONA_TOOL} & _names(tools):
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


class _PersonaSessionProvider(LLMProvider):
    """One Persona turn, then ordinary ones, recording what each main call bound."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.setup_calls = 0
        self.seen: list[tuple[str, str, tuple[str, ...]]] = []

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(tools)

    async def chat_with_retry(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(tools)

    async def _answer(self, tools) -> LLMResponse:
        names = _names(tools)
        if PERSONA_TOOL in names:
            self.setup_calls += 1
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="persona",
                        name=PERSONA_TOOL,
                        arguments={
                            "description": "A travel concierge that plans walkable days",
                            "artifactName": "travel-concierge",
                            "coordinator": {
                                "brief": "Own the travel conversation and synthesize the daily plan",
                                "systemPrompt": "Refuse to plan before destination and dates are known.",
                            },
                            "workers": [
                                {
                                    "as": "planner",
                                    "agent": "Raven-Research",
                                    "brief": "Route each day within the walking limit",
                                }
                            ],
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        from raven.agent.subagent.charter import current_charter
        from raven.agent.subagent.delegate import current_delegate

        charter = current_charter()
        table = current_delegate()
        self.seen.append(
            (
                charter.prompt if charter else "",
                charter.instruction_addendum if charter else "",
                tuple(table.labels()) if table else (),
            )
        )
        return LLMResponse(content="OK", finish_reason="stop")


def _persona_session_loop(tmp_path, provider: LLMProvider, *, default_mode: str | None = None) -> AgentLoop:
    playbook_config = PlaybookConfig(
        enabled=True,
        dir=str(tmp_path / "playbooks"),
        agentHarness="generate",
        **({"defaultGenerationMode": default_mode} if default_mode else {}),
    )
    return AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=3),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
            playbook_config=playbook_config,
        ),
        subagents=SubagentWiring(
            agents=[
                BuiltinAgentConfig(
                    name="Raven-Research",
                    description="Researches current external facts and verifies primary sources.",
                    owns="opening hours, local restrictions, and route feasibility",
                )
            ]
        ),
    )


@pytest.mark.asyncio
async def test_a_generated_persona_runs_the_session_from_the_turn_after_it(tmp_path) -> None:
    provider = _PersonaSessionProvider()
    loop = _persona_session_loop(tmp_path, provider)

    await loop.run_turn(
        _request("Create a travel concierge persona", "persona-session", playbook_mode="persona"),
        _emit,
        lambda: [],
        stream=False,
    )
    await loop.run_turn(
        _request("Four days in Kyoto", "persona-session", playbook_mode="off"),
        _emit,
        lambda: [],
        stream=False,
    )

    generating, following = provider.seen
    # The turn that generated it answers about the artifact, not as it: a
    # coordinator that refuses to plan without dates would refuse to report what
    # it just made. It reports a draft, because nothing has been saved.
    assert "is holding it as a draft" in generating[0]
    assert generating[1] == ""
    assert generating[2] == ()
    assert following[0] == "Own the travel conversation and synthesize the daily plan"
    assert following[1] == "Refuse to plan before destination and dates are known."
    assert following[2] == ("planner",)
    assert provider.setup_calls == 1


class _UndesignableProvider(LLMProvider):
    """A model that cannot produce a valid Harness, and would rather write a file.

    Both halves matter. The persona tool answers with a coordinator the
    validator refuses, twice, so generation is thrown away -- and the main turn
    then reaches for ``create_playbook``, which is exactly what a reader whose
    own words said "create and save it" got when the turn kept its full tool
    table.
    """

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.setup_calls = 0
        self.wrote = 0
        self.offered: list[tuple[str, ...]] = []

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(tools)

    async def chat_with_retry(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(tools)

    async def _answer(self, tools) -> LLMResponse:
        names = _names(tools)
        if PERSONA_TOOL in names:
            self.setup_calls += 1
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id=f"bad-{self.setup_calls}",
                        name=PERSONA_TOOL,
                        # Neither a seat nor a sentence: nothing the validator
                        # can read, so the repair round fails the same way.
                        arguments={"description": "d", "artifactName": "a", "coordinator": 7, "workers": []},
                    )
                ],
            )
        self.offered.append(tuple(sorted(names)))
        writing = next((n for n in ("create_playbook", "write_file", "edit_file") if n in names), None)
        if writing:
            self.wrote += 1
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="w", name=writing, arguments={"name": "travel-concierge"})],
            )
        return LLMResponse(content="I could not design one.")


@pytest.mark.asyncio
async def test_a_maker_turn_writes_nothing_when_the_design_cannot_be_made(tmp_path) -> None:
    """Generation failing must not turn the maker into an ordinary turn.

    Caught in the app. The Harness came back invalid, was repaired once, came
    back invalid again and was dropped -- and the turn behind it still held
    every tool, so the reader's own "create and save it" was carried out by
    ``create_playbook``: a library row they never asked for, under a name that
    already existed, and a question about overwriting it.
    """
    playbook_root = tmp_path / "playbooks"
    provider = _UndesignableProvider()
    loop = _persona_session_loop(tmp_path, provider)

    await loop.run_turn(
        _request("Create and save a travel concierge called travel-concierge", "maker", playbook_mode="persona"),
        _emit,
        lambda: [],
        stream=False,
    )

    # Tried and failed, twice: generated once and repaired once.
    assert provider.setup_calls == 2
    # And wrote nothing, because it could not reach a tool that writes.
    assert provider.wrote == 0
    assert _saved(playbook_root) == []
    assert loop.session_draft("test:maker") is None
    # The tool table is the narrowing, not the wording. Naming one tool would
    # prove nothing -- a build without it registered passes by accident -- so
    # what is asserted is the whole table: talking to the reader, and nothing
    # that writes a file, runs a command or dispatches work. Unnarrowed, this
    # turn was offered twenty-six of them.
    from raven.agent.loop.main import MAKER_TOOLS

    assert provider.offered, "the main turn never ran"
    for offered in provider.offered:
        assert set(offered) <= set(MAKER_TOOLS), f"a maker turn reached {sorted(set(offered) - set(MAKER_TOOLS))}"


@pytest.mark.asyncio
async def test_a_coordinator_written_as_its_own_brief_is_read_not_refused(tmp_path) -> None:
    """The tool asks for an object; models send the sentence. Read it.

    Six generations in a row sent ``coordinator`` as a string, and every one was
    rejected, repaired once, rejected again and thrown away -- which is what
    left the maker with no draft and the turn free to write a playbook instead.
    """
    from raven.playbook.agent_generator import _persona_spec_from_args

    spec, _ = _persona_spec_from_args(
        {
            "description": "A travel concierge",
            "artifactName": "travel-concierge",
            "coordinator": "Own the trip conversation and dispatch the specialists",
            "workers": [{"as": "scout", "agent": "Raven-Research", "brief": "Find what is open"}],
        },
        {"Raven-Research"},
    )
    assert spec.coordinator is not None
    assert spec.coordinator.brief.startswith("Own the trip conversation")


def test_a_brief_wrapped_in_the_call_s_own_markup_is_peeled() -> None:
    """The value a model sent as a wire fragment is still the value.

    Caught in the app: six of six generated coordinators arrived as
    ``<parameter name="brief">Be the travel concierge...``, so the Persona ran
    on a systemPrompt that opened with an XML tag and the card drew one too.
    Delegates were clean, which is why this is peeled where every seat's prose
    is read rather than on the coordinator alone.
    """
    from raven.playbook.agent_generator import _persona_spec_from_args

    spec, briefs = _persona_spec_from_args(
        {
            "description": "A travel concierge",
            "artifactName": "travel-concierge",
            "coordinator": {"brief": '<parameter name="brief">Own the trip conversation'},
            "workers": [
                {
                    "as": "scout",
                    "agent": "Raven-Research",
                    "brief": '<parameter name="brief">Find what is open</parameter>',
                }
            ],
        },
        {"Raven-Research"},
    )
    assert spec.coordinator is not None
    assert spec.coordinator.brief == "Own the trip conversation"
    assert briefs["scout"] == "Find what is open"


@pytest.mark.asyncio
async def test_an_ordinary_turn_mints_no_persona_even_with_the_default_set(tmp_path) -> None:
    """`defaultGenerationMode: persona` enables the capability, not every message.

    Caught in the app rather than here. With that default shipped, a reader
    typing "hi" spent a model call minting a Persona, bound the conversation to
    it, and heard the assistant volunteer a draft they had not asked about --
    and a conversation started ON a saved Persona was rebound to the new one,
    so the Persona they picked lasted a single turn.
    """
    provider = _PersonaSessionProvider()
    loop = _persona_session_loop(tmp_path, provider, default_mode="persona")

    await loop.run_turn(
        _request("Four days in Kyoto", "ordinary-chat"),
        _emit,
        lambda: [],
        stream=False,
    )

    assert provider.setup_calls == 0
    assert loop.session_harness_name("test:ordinary-chat") is None
    assert loop.session_draft("test:ordinary-chat") is None


@pytest.mark.asyncio
async def test_asking_again_in_the_maker_replaces_the_draft_it_is_refining(tmp_path) -> None:
    """The maker is a conversation: saying "make it smaller" generates again.

    The opposite of the case above, and the reason the guard is on what the
    turn ASKED for rather than on whether the session already carries one: the
    page that describes a Persona keeps one conversation, so its second turn
    would otherwise be refused by its own first.
    """
    provider = _PersonaSessionProvider()
    loop = _persona_session_loop(tmp_path, provider, default_mode="persona")

    for text in ("Create a travel concierge persona", "Give it fewer workers"):
        await loop.run_turn(
            _request(text, "maker", playbook_mode="persona"),
            _emit,
            lambda: [],
            stream=False,
        )

    assert provider.setup_calls == 2
    assert loop.session_draft("test:maker") is not None


@pytest.mark.asyncio
async def test_an_unadopted_session_is_untouched_by_another_sessions_persona(tmp_path) -> None:
    provider = _PersonaSessionProvider()
    loop = _persona_session_loop(tmp_path, provider)

    await loop.run_turn(
        _request("Create a travel concierge persona", "adopted", playbook_mode="persona"),
        _emit,
        lambda: [],
        stream=False,
    )
    await loop.run_turn(
        _request("Four days in Kyoto", "bystander", playbook_mode="off"),
        _emit,
        lambda: [],
        stream=False,
    )

    assert provider.seen[1] == ("", "", ())


def _saved_persona() -> UnifiedPlaybookSpec:
    harness = AgentPlaybookSpec(
        name="travel-concierge",
        description="A travel concierge that plans walkable days",
        coordinator={
            "brief": "Own the travel conversation and synthesize the daily plan",
            "playbook": {"memory": {"systemPrompt": "Refuse to plan before destination and dates are known."}},
        },
        delegate=[
            DelegateEntry(
                **{
                    "as": "planner",
                    "name": "Raven",
                    "brief": "Route each day within the walking limit",
                }
            )
        ],
    )
    return UnifiedPlaybookSpec(
        name="travel-concierge",
        description="Plan walkable days from a destination and dates",
        match=PlaybookMatch(summary="Plan a trip", keywords=["trip", "itinerary"]),
        harness=harness,
    )


class _LoadPersonaProvider(LLMProvider):
    """Calls ``load_playbook`` once, then records what the rest of the turn runs as."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.loaded = False
        self.seen: list[tuple[str, str, tuple[str, ...]]] = []

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer()

    async def chat_with_retry(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer()

    async def _answer(self) -> LLMResponse:
        from raven.agent.subagent.charter import current_charter
        from raven.agent.subagent.delegate import current_delegate

        charter = current_charter()
        table = current_delegate()
        self.seen.append(
            (
                charter.prompt if charter else "",
                charter.instruction_addendum if charter else "",
                tuple(table.labels()) if table else (),
            )
        )
        if not self.loaded:
            self.loaded = True
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="load", name="load_playbook", arguments={"name": "travel-concierge"})],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="OK", finish_reason="stop")


@pytest.mark.asyncio
async def test_loading_a_stored_persona_binds_its_coordinator_for_the_rest_of_the_turn(tmp_path) -> None:
    playbook_root = tmp_path / "playbooks"
    PlaybookStore(playbook_root).save(_saved_persona())
    provider = _LoadPersonaProvider()
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
        _request("Four days in Kyoto", "load-persona", playbook_mode="off"),
        _emit,
        lambda: [],
        stream=False,
    )

    before, after = provider.seen[0], provider.seen[-1]
    assert before == ("", "", ())
    assert after[0] == "Own the travel conversation and synthesize the daily plan"
    assert after[1] == "Refuse to plan before destination and dates are known."
    assert after[2] == ("planner",)


@pytest.mark.asyncio
async def test_unbinding_a_session_puts_it_back_on_the_plain_path(tmp_path) -> None:
    provider = _PersonaSessionProvider()
    loop = _persona_session_loop(tmp_path, provider)

    await loop.run_turn(
        _request("Create a travel concierge persona", "disposable", playbook_mode="persona"),
        _emit,
        lambda: [],
        stream=False,
    )
    key = "test:disposable"
    assert loop.session_harness_name(key) == "travel-concierge"
    assert loop.session_persona(key) is not None

    loop.bind_session_harness(key, None)

    assert loop.session_harness_name(key) is None
    assert loop.session_persona(key) is None


@pytest.mark.asyncio
async def test_a_bound_harness_is_a_snapshot_the_library_can_no_longer_move(tmp_path) -> None:
    """The window the user opened keeps the Harness they chose."""
    playbook_root = tmp_path / "playbooks"
    PlaybookStore(playbook_root).save(_saved_persona())
    provider = _PersonaSessionProvider()
    loop = _persona_session_loop(tmp_path, provider)
    key = "test:frozen"

    loop.bind_session_harness(key, "travel-concierge")
    bound = loop.session_persona(key)
    assert bound is not None and bound[0].prompt.startswith("Own the travel conversation")

    shutil.rmtree(PlaybookStore(playbook_root).user_directory("travel-concierge"))
    loop._session_personas.pop(key, None)

    still = loop.session_persona(key)
    assert still is not None
    assert still[0].prompt == bound[0].prompt
    assert still[1].labels() == ["planner"]
