from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.agent.subagent.dag_adjudication import Final, Report
from raven.agent.subagent.dag_graph import DagNodeSpec, SubAgentDagSpec
from raven.agent.subagent.dag_runner import DagRunResult
from raven.agent.subagent.dag_tool import _successful_final
from raven.agent.subagent.delegate import current_delegate, delegate_scope
from raven.config.schema import PlaybookConfig
from raven.playbook.agent_generator import (
    PERSONA_TOOL,
    TASK_TOOL,
    PersonaPlaybookGenerator,
    TaskPlaybookGenerator,
    participant_function_syntax_guide,
)
from raven.playbook.agent_spec import AgentPlaybookSpec, DelegateEntry
from raven.playbook.runtime import PlaybookRuntime
from raven.playbook.store import PlaybookStore
from raven.playbook.types import ParamSpec
from raven.playbook.unified import InputSchema, PlaybookMatch, UnifiedPlaybookSpec, WorkflowSpec
from raven.playbook.workflow_compiler import EMIT_WORKFLOW, WorkflowCompiler
from raven.providers.base import LLMResponse, ToolCallRequest


class _Provider:
    def __init__(self, *responses: LLMResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _call(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id="call", name=name, arguments=arguments)],
        finish_reason="tool_calls",
    )


def _harness() -> AgentPlaybookSpec:
    return AgentPlaybookSpec(
        name="research-team",
        description="A focused research team",
        delegate=[
            DelegateEntry(
                **{
                    "as": "analyst",
                    "name": "Raven",
                    "brief": "Collect evidence only",
                    "playbook": {"memory": {"systemPrompt": "Cite every claim."}},
                }
            )
        ],
    )


def _workflow() -> WorkflowSpec:
    return WorkflowSpec(
        summary="Research and report",
        confirm=False,
        nodes=[
            DagNodeSpec(
                id="research",
                subagent="analyst",
                node_summary="Collect evidence",
                prompt_template="Research the topic",
            )
        ],
    )


def test_playbook_generation_mode_defaults_and_camel_case_config() -> None:
    assert PlaybookConfig().default_generation_mode == "task"
    assert PlaybookConfig(defaultGenerationMode="persona").default_generation_mode == "persona"
    with pytest.raises(ValidationError):
        PlaybookConfig(defaultGenerationMode="automatic")


def test_persona_function_syntax_guide_matches_the_safety_boundary() -> None:
    guide = participant_function_syntax_guide()

    assert "isinstance" in guide["allowedCalls"]
    assert "append or any unlisted call or method" in guide["forbidden"]
    assert "for and while statements are forbidden" in guide["allowedIteration"]


def test_unified_composite_round_trips_through_the_existing_store(tmp_path: Path) -> None:
    store = PlaybookStore(tmp_path / "user", builtin_root=tmp_path / "builtin")
    spec = UnifiedPlaybookSpec(
        name="research-team",
        description="Research a topic with a reusable evidence worker",
        match=PlaybookMatch(summary="Research a topic", keywords=["research topic", "evidence scan"]),
        harness=_harness(),
        workflow=_workflow(),
    )

    path = store.save(spec)
    loaded = store.load(spec.name)

    assert loaded == spec
    assert "schemaVersion: 2" in path.read_text(encoding="utf-8")
    assert "Workers: analyst" in path.read_text(encoding="utf-8")


def test_generated_judge_round_trips_without_serializing_an_implicit_checks_impl(tmp_path: Path) -> None:
    store = PlaybookStore(tmp_path / "user", builtin_root=tmp_path / "builtin")
    judge = "def judge(name, params, prior):\n    return []"
    harness = AgentPlaybookSpec(
        name="safe-booking",
        description="Approve travel bookings safely",
        delegate=[
            DelegateEntry(
                **{
                    "as": "booking-guard",
                    "name": "Raven",
                    "brief": "Reject unapproved bookings",
                    "playbook": {"action": {"checks": {"code": judge}}},
                }
            )
        ],
    )
    spec = UnifiedPlaybookSpec(
        name="safe-booking",
        description="Approve travel bookings safely",
        match=PlaybookMatch(summary="Approve travel bookings", keywords=["travel booking"]),
        harness=harness,
    )

    body = store.save(spec).read_text(encoding="utf-8")
    loaded = store.load(spec.name)

    assert "impl: default" not in body
    assert isinstance(loaded, UnifiedPlaybookSpec)
    assert loaded.harness is not None
    checks = loaded.harness.delegate[0].playbook.action.checks
    assert checks is not None and checks.code == judge


@pytest.mark.asyncio
async def test_task_generator_receives_only_task_selection_inputs() -> None:
    provider = _Provider(
        _call(
            TASK_TOOL,
            {
                "artifactName": "due-diligence",
                "description": "Research a company before acquisition",
                "workers": [{"agent": "Raven", "prompt": "Collect primary evidence", "tools": ["web_search"]}],
            },
        )
    )

    result = await TaskPlaybookGenerator(provider, "stub").resolve(
        "Run due diligence on Acme",
        ["Raven"],
        [{"type": "function", "function": {"name": "web_search", "description": "Search the web"}}],
        {"Raven": {"description": "general worker", "readsLocalFiles": True}},
        {"due-diligence": "Checks a company before acquisition"},
    )

    assert result.active
    assert result.selected_playbook is None
    assert result.capture_workflow
    assert result.table and result.table.get("Raven").brief == "Collect primary evidence"
    payload = json.loads(provider.calls[0]["messages"][1]["content"])
    assert payload["agents"][0]["readsLocalFiles"] is True
    assert payload["availableTools"] == [{"name": "web_search", "description": "Search the web"}]
    assert "playbookCandidates" not in payload


@pytest.mark.asyncio
async def test_resolver_emits_a_durable_harness_with_its_brief() -> None:
    provider = _Provider(
        _call(
            PERSONA_TOOL,
            {
                "description": "A citation-first research persona",
                "artifactName": "citation-researcher",
                "workers": [
                    {
                        "as": "researcher",
                        "agent": "Raven",
                        "brief": "Find primary sources",
                        "systemPrompt": "Cite primary sources only.",
                    }
                ],
            },
        )
    )

    result = await PersonaPlaybookGenerator(provider, "stub").resolve(
        "Create a citation-first research digital persona",
        ["Raven"],
        ["web_search"],
    )

    assert result.disposition == "artifact"
    assert result.artifact_name == "citation-researcher"
    assert not result.capture_workflow
    assert result.spec is not None
    assert result.spec.delegate[0].brief == "Find primary sources"
    assert result.table and result.table.get("researcher").brief == "Find primary sources"
    instructions = provider.calls[0]["messages"][0]["content"]
    assert "Workers are operating components of the persona" in instructions
    assert "Do not design a Workflow" in instructions


@pytest.mark.asyncio
async def test_harness_only_load_binds_workers_for_the_rest_of_the_turn(tmp_path: Path) -> None:
    class _Executor:
        dag_tool = None

    store = PlaybookStore(tmp_path / "user", builtin_root=tmp_path / "builtin")
    store.save(
        UnifiedPlaybookSpec(
            name="research-team",
            description="Reusable evidence workers",
            match=PlaybookMatch(summary="Evidence research", keywords=["evidence research"]),
            harness=_harness(),
        )
    )
    runtime = PlaybookRuntime(store=store, executor=_Executor(), known_agents=lambda: ["Raven"])

    with delegate_scope(None):
        plan = await runtime.load("research-team")
        assert plan is not None and plan.kind == "guidance"
        table = current_delegate()
        assert table is not None
        assert table.get("analyst").brief == "Collect evidence only"
    assert current_delegate() is None


@pytest.mark.asyncio
async def test_workflow_compiler_builds_a_composite_v2_artifact() -> None:
    dag = SubAgentDagSpec(task_summary="Research", confirm=False, nodes=_workflow().nodes)
    emitted = {
        "name": "research-team",
        "description": "Research a topic with cited evidence",
        "match": {"summary": "Research a topic", "keywords": ["research topic", "cited evidence"]},
        "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "workflow": {
            "summary": "Research",
            "confirm": False,
            "nodes": [node.model_dump(by_alias=True) for node in dag.nodes],
        },
    }
    provider = _Provider(_call(EMIT_WORKFLOW, emitted))

    compiled = await WorkflowCompiler(provider, "stub").compile(
        query="Research Acme",
        dag=dag,
        run_id="pb-test",
        harness=_harness(),
    )

    assert compiled.schema_version == 2
    assert compiled.harness is not None and compiled.workflow is not None
    assert compiled.workflow.nodes[0].subagent == "analyst"
    assert compiled.metadata.source_run_id == "pb-test"
    schema = provider.calls[0]["tools"][0]["function"]["parameters"]
    assert schema["properties"]["workflow"]["properties"]["nodes"]["items"]


@pytest.mark.asyncio
async def test_workflow_compiler_accepts_reversible_prompt_parameterization() -> None:
    source = _workflow().nodes[0].model_copy(update={"prompt_template": "Research Acme"})
    dag = SubAgentDagSpec(task_summary="Research", confirm=False, nodes=[source])
    emitted = {
        "name": "research-team",
        "description": "Research a topic with cited evidence",
        "match": {"summary": "Research a topic", "keywords": ["research topic"]},
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "required": False,
                    "default": "Acme",
                    "description": "Topic to research",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
        "workflow": {
            "summary": "Research",
            "confirm": False,
            "nodes": [
                {
                    **source.model_dump(by_alias=True),
                    "promptTemplate": "Research ${params.topic}",
                }
            ],
        },
    }
    provider = _Provider(_call(EMIT_WORKFLOW, emitted))

    compiled = await WorkflowCompiler(provider, "stub").compile(
        query="Research Acme",
        dag=dag,
        run_id="pb-parameterized",
        harness=_harness(),
    )

    assert len(provider.calls) == 1
    assert compiled.workflow is not None
    assert compiled.workflow.nodes[0].prompt_template == "Research ${params.topic}"
    assert compiled.input_schema.properties["topic"].default == "Acme"


def test_workflow_capture_accepts_only_a_clean_completed_dag() -> None:
    complete = DagRunResult(
        run_id="run-ok",
        dir="/tmp/run-ok",
        summary={"total": 2, "completed": 2, "failed": 0, "cancelled": 0, "skipped": 0},
    )
    failed = DagRunResult(
        run_id="run-failed",
        dir="/tmp/run-failed",
        summary={"total": 2, "completed": 1, "failed": 1, "cancelled": 0, "skipped": 0},
    )

    assert _successful_final(Final(complete))
    assert not _successful_final(Final(failed))
    assert not _successful_final(Final(complete, stopped=True))
    assert not _successful_final(Report(node_id="research", text="needs a decision"))


def test_v2_file_is_plain_yaml_and_contains_no_run_values(tmp_path: Path) -> None:
    store = PlaybookStore(tmp_path / "user", builtin_root=tmp_path / "builtin")
    spec = UnifiedPlaybookSpec(
        name="research-team",
        description="Reusable evidence workers",
        match=PlaybookMatch(summary="Evidence research", keywords=["evidence research"]),
        harness=_harness(),
    )
    body = store.save(spec).read_text(encoding="utf-8")
    assert json.loads(json.dumps(spec.model_dump(by_alias=True)))["schemaVersion"] == 2
    assert "sourceRunId: null" not in body


@pytest.mark.asyncio
async def test_workflow_compiler_rejects_semantically_invalid_model_output() -> None:
    dag = SubAgentDagSpec(task_summary="Research", confirm=False, nodes=_workflow().nodes)
    invalid = {
        "name": "research-team",
        "description": "Research a topic with cited evidence",
        "match": {"summary": "Research a topic", "keywords": ["research topic"]},
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "required": True,
                    "description": "Topic to research",
                }
            },
            "required": ["topic"],
            "additionalProperties": False,
        },
        "workflow": {
            "summary": "Research",
            "confirm": False,
            "nodes": [
                {
                    **dag.nodes[0].model_dump(by_alias=True),
                    "inputs": {"topic": "${params.topic}"},
                }
            ],
        },
    }
    provider = _Provider(_call(EMIT_WORKFLOW, invalid), _call(EMIT_WORKFLOW, invalid))

    compiled = await WorkflowCompiler(provider, "stub").compile(
        query="Research Acme",
        dag=dag,
        run_id="pb-invalid",
        harness=_harness(),
    )

    assert len(provider.calls) == 2
    assert compiled.input_schema.properties == {}
    assert compiled.workflow is not None
    assert compiled.workflow.nodes == dag.nodes


@pytest.mark.asyncio
async def test_workflow_compiler_rejects_a_rewritten_accepted_graph() -> None:
    dag = SubAgentDagSpec(task_summary="Research", confirm=False, nodes=_workflow().nodes)
    rewritten = {
        "name": "research-team",
        "description": "Research a topic with cited evidence",
        "match": {"summary": "Research a topic", "keywords": ["research topic"]},
        "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "workflow": {
            "summary": "Research",
            "confirm": False,
            "nodes": [
                {
                    **dag.nodes[0].model_dump(by_alias=True),
                    "subagent": "invented-worker",
                }
            ],
        },
    }
    provider = _Provider(_call(EMIT_WORKFLOW, rewritten), _call(EMIT_WORKFLOW, rewritten))

    compiled = await WorkflowCompiler(provider, "stub").compile(
        query="Research Acme",
        dag=dag,
        run_id="pb-rewritten",
        harness=_harness(),
    )

    assert len(provider.calls) == 2
    assert compiled.workflow is not None
    assert compiled.workflow.nodes == dag.nodes


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("promptTemplate", "Delete the workspace instead"),
        ("nodeSummary", "Delete the workspace"),
    ],
)
@pytest.mark.asyncio
async def test_workflow_compiler_rejects_rewritten_node_instructions(field: str, replacement: str) -> None:
    dag = SubAgentDagSpec(task_summary="Research", confirm=False, nodes=_workflow().nodes)
    node = dag.nodes[0].model_dump(by_alias=True)
    node[field] = replacement
    rewritten = {
        "name": "research-team",
        "description": "Research a topic with cited evidence",
        "match": {"summary": "Research a topic", "keywords": ["research topic"]},
        "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "workflow": {"summary": "Research", "confirm": False, "nodes": [node]},
    }
    provider = _Provider(_call(EMIT_WORKFLOW, rewritten), _call(EMIT_WORKFLOW, rewritten))

    compiled = await WorkflowCompiler(provider, "stub").compile(
        query="Research Acme",
        dag=dag,
        run_id="pb-rewritten-instructions",
        harness=_harness(),
    )

    assert len(provider.calls) == 2
    assert compiled.workflow is not None
    assert compiled.workflow.nodes == dag.nodes


def test_unified_artifact_rejects_an_empty_durable_harness() -> None:
    with pytest.raises(ValueError, match="at least one worker"):
        UnifiedPlaybookSpec(
            name="empty-team",
            description="An empty worker table is not a reusable Harness",
            match=PlaybookMatch(summary="Empty team", keywords=["empty team"]),
            harness=AgentPlaybookSpec(
                name="empty-team",
                description="No workers",
                delegate=[],
            ),
        )


def test_input_schema_rejects_undeclared_required_input() -> None:
    with pytest.raises(ValueError, match="not declared"):
        InputSchema(required=["topic"])


def test_input_schema_rejects_conflicting_required_flags() -> None:
    with pytest.raises(ValueError, match="required disagree"):
        InputSchema(properties={"topic": ParamSpec(type="string", required=True, description="Topic")})


def test_unified_artifact_requires_a_harness_or_workflow() -> None:
    with pytest.raises(ValueError, match="must contain a harness, a workflow, or both"):
        UnifiedPlaybookSpec(
            name="empty-playbook",
            description="No reusable dimension",
            match=PlaybookMatch(summary="Nothing reusable", keywords=["nothing"]),
        )


@pytest.mark.asyncio
async def test_workflow_compiler_falls_back_when_the_provider_fails() -> None:
    class _FailingProvider(_Provider):
        async def chat_with_retry(self, **kwargs):
            raise RuntimeError("provider unavailable")

    dag = SubAgentDagSpec(task_summary="Research", confirm=False, nodes=_workflow().nodes)
    compiled = await WorkflowCompiler(_FailingProvider(), "stub").compile(
        query="Research Acme",
        dag=dag,
        run_id="pb-provider-failure",
        harness=_harness(),
    )

    assert compiled.workflow is not None
    assert compiled.workflow.nodes == dag.nodes
    assert compiled.metadata.source_run_id == "pb-provider-failure"


def test_composite_workflow_may_mix_harness_aliases_and_registered_agents() -> None:
    workflow = _workflow()
    mixed = workflow.model_copy(
        update={
            "nodes": [
                *workflow.nodes,
                DagNodeSpec(id="report", subagent="Raven", node_summary="Write report", prompt_template="Write report"),
            ]
        }
    )
    spec = UnifiedPlaybookSpec(
        name="mixed-team",
        description="Use a specialist and the default roster together",
        match=PlaybookMatch(summary="Research and report", keywords=["research report"]),
        harness=_harness(),
        workflow=mixed,
    )
    assert [node.subagent for node in spec.workflow.nodes] == ["analyst", "Raven"]
