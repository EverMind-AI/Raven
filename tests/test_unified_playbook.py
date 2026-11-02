from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.agent.subagent.dag_adjudication import Final, Report
from raven.agent.subagent.dag_graph import DagNodeSpec, SubAgentDagSpec
from raven.agent.subagent.dag_runner import DagRunResult
from raven.agent.subagent.dag_tool import _successful_final
from raven.agent.subagent.delegate import Worker, current_delegate, delegate_scope
from raven.config.schema import PlaybookConfig
from raven.playbook.agent_generator import (
    PERSONA_TOOL,
    TASK_TOOL,
    PersonaPlaybookGenerator,
    TaskPlaybookGenerator,
    participant_function_syntax_guide,
)
from raven.playbook.agent_spec import AgentPlaybookSpec, CoordinatorEntry, DelegateEntry
from raven.playbook.run_record import PlaybookRunCapture, record_completed_dag, rewrite_completed_dag
from raven.playbook.runtime import PlaybookRuntime
from raven.playbook.store import PlaybookStore
from raven.playbook.types import ParamSpec
from raven.playbook.unified import InputSchema, PlaybookMatch, UnifiedPlaybookSpec, WorkflowSpec
from raven.playbook.workflow_compiler import EMIT_WORKFLOW, WorkflowCompiler, _preservation_errors
from raven.providers.base import LLMResponse, ToolCallRequest


class _Provider:
    def __init__(self, *responses: LLMResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.purposes: list[str | None] = []

    async def chat_with_retry(self, **kwargs):
        from raven.observability.purpose import current

        self.calls.append(kwargs)
        self.purposes.append(current())
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
    assert provider.purposes == ["playbook"]
    assert "playbookCandidates" not in payload


@pytest.mark.asyncio
async def test_resolver_emits_a_durable_harness_with_its_brief() -> None:
    provider = _Provider(
        _call(
            PERSONA_TOOL,
            {
                "description": "A citation-first research persona",
                "artifactName": "citation-researcher",
                "coordinator": {
                    "brief": "Own the research conversation and synthesize specialist findings",
                    "systemPrompt": "Answer with traceable evidence.",
                },
                "workers": [
                    {
                        "as": "researcher",
                        "agent": "Raven-Research",
                        "brief": "Find primary sources",
                        "systemPrompt": "Cite primary sources only.",
                    }
                ],
            },
        )
    )

    result = await PersonaPlaybookGenerator(provider, "stub").resolve(
        "Create a citation-first research digital persona",
        ["Raven", "Raven-Research"],
        ["web_search"],
    )

    assert result.disposition == "artifact"
    assert result.artifact_name == "citation-researcher"
    assert not result.capture_workflow
    assert result.spec is not None
    assert result.spec.coordinator is not None
    assert result.spec.coordinator.brief.startswith("Own the research conversation")
    assert result.spec.delegate[0].brief == "Find primary sources"
    assert result.table and result.table.get("researcher").brief == "Find primary sources"
    instructions = provider.calls[0]["messages"][0]["content"]
    assert "The coordinator is the main Raven identity" in instructions
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
    assert provider.purposes == ["playbook"]
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


def _branching_dag() -> SubAgentDagSpec:
    return SubAgentDagSpec(
        task_summary="Triage",
        confirm=False,
        nodes=[
            DagNodeSpec(
                id="r",
                subagent="analyst",
                node_summary="Classify",
                prompt_template="Classify the issue",
                routes={"a": "it is a", "b": "it is b"},
            ),
            DagNodeSpec(
                id="g",
                subagent="analyst",
                node_summary="Handle a",
                prompt_template="Handle a",
                depends_on=["r"],
                when="r.a",
            ),
        ],
    )


def _compiled_from(dag: SubAgentDagSpec, nodes: list[DagNodeSpec]) -> UnifiedPlaybookSpec:
    return UnifiedPlaybookSpec(
        name="research-team",
        description="Research a topic with cited evidence",
        match=PlaybookMatch(summary="Research a topic", keywords=["research topic"]),
        harness=_harness(),
        workflow=WorkflowSpec(summary=dag.task_summary, confirm=dag.confirm, nodes=nodes),
    )


def _emitted_from(dag: SubAgentDagSpec, nodes: list[dict]) -> dict:
    return {
        "name": "research-team",
        "description": "Research a topic with cited evidence",
        "match": {"summary": "Research a topic", "keywords": ["research topic"]},
        "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "workflow": {"summary": dag.task_summary, "confirm": dag.confirm, "nodes": nodes},
    }


def test_compilation_that_keeps_the_branch_fields_is_preserving() -> None:
    accepted = _branching_dag()

    assert _preservation_errors(accepted, _compiled_from(accepted, accepted.nodes)) == []


@pytest.mark.parametrize(
    ("index", "changed"),
    [
        (0, {"routes": {"a": "it is a", "b": "something else entirely"}}),
        (0, {"routes": {"a": "it is a", "b": "it is b", "c": "a route nobody asked for"}}),
        (0, {"routes": None}),
        (0, {"select": "many"}),
        (1, {"when": "r.b"}),
        (1, {"when": None}),
    ],
)
def test_compilation_preserving_is_checked_for_the_branch_fields(index: int, changed: dict) -> None:
    accepted = _branching_dag()
    nodes = list(accepted.nodes)
    nodes[index] = nodes[index].model_copy(update=changed)
    [field] = changed

    errors = _preservation_errors(accepted, _compiled_from(accepted, nodes))

    assert errors == [f"node {accepted.nodes[index].id!r}: {field} changed during compilation"]


def test_a_restated_default_select_is_not_a_change() -> None:
    """A model that writes `select: "one"` beside routes has said what the omitted
    field already meant, and a repair round for it would spend the compile twice."""
    accepted = _branching_dag()
    nodes = list(accepted.nodes)
    nodes[0] = nodes[0].model_copy(update={"select": "one"})

    assert _preservation_errors(accepted, _compiled_from(accepted, nodes)) == []


@pytest.mark.parametrize("blank", ["", "  "], ids=["empty", "spaces"])
@pytest.mark.parametrize("branches", [True, False], ids=["branching", "plain"])
def test_a_blank_when_is_not_a_change(branches: bool, blank: str) -> None:
    """An unguarded node's `when` may come back empty, as spaces or as None; all mean no guard."""
    if branches:
        accepted = SubAgentDagSpec(
            task_summary="Triage",
            confirm=False,
            nodes=[
                DagNodeSpec(
                    id="r",
                    subagent="analyst",
                    node_summary="Classify",
                    prompt_template="Classify the issue",
                    routes={"a": "it is a", "b": "it is b"},
                ),
                DagNodeSpec(
                    id="g", subagent="analyst", node_summary="Handle", prompt_template="Handle", depends_on=["r"]
                ),
            ],
        )
    else:
        accepted = SubAgentDagSpec(
            task_summary="Fetch",
            confirm=False,
            nodes=[DagNodeSpec(id="a", subagent="analyst", node_summary="Fetch", prompt_template="Fetch")],
        )
    nodes = [node.model_copy(update={"when": blank}) for node in accepted.nodes]

    assert _preservation_errors(accepted, _compiled_from(accepted, nodes)) == []


@pytest.mark.asyncio
async def test_workflow_compiler_keeps_a_branching_graph_whole() -> None:
    dag = _branching_dag()
    emitted = _emitted_from(dag, [node.model_dump(by_alias=True, exclude_none=True) for node in dag.nodes])
    provider = _Provider(_call(EMIT_WORKFLOW, emitted))

    compiled = await WorkflowCompiler(provider, "stub").compile(
        query="Triage the issue",
        dag=dag,
        run_id="pb-branching",
        harness=_harness(),
    )

    assert len(provider.calls) == 1
    assert compiled.workflow is not None
    assert [node.model_dump() for node in compiled.workflow.nodes] == [node.model_dump() for node in dag.nodes]
    schema = provider.calls[0]["tools"][0]["function"]["parameters"]
    items = schema["properties"]["workflow"]["properties"]["nodes"]["items"]
    assert {"routes", "select", "when"} <= set(items["properties"])


@pytest.mark.asyncio
async def test_the_compiler_tells_the_model_to_keep_a_graphs_branches() -> None:
    """The preservation check refuses a compiled graph whose routes, select or guards
    changed, so the model has to be told they are among the things to keep, by the
    names the graph it is handed carries them under."""
    dag = _branching_dag()
    dag.nodes[0] = dag.nodes[0].model_copy(update={"select": "many"})
    emitted = _emitted_from(dag, [node.model_dump(by_alias=True, exclude_none=True) for node in dag.nodes])
    provider = _Provider(_call(EMIT_WORKFLOW, emitted))

    await WorkflowCompiler(provider, "stub").compile(
        query="Triage the issue",
        dag=dag,
        run_id="pb-branch-prompt",
        harness=_harness(),
    )

    system, user = provider.calls[0]["messages"]
    kept = " ".join(system["content"].split()).split("Preserve", 1)[1].split(". ", 1)[0]
    carried = {key for node in json.loads(user["content"])["acceptedDag"]["nodes"] for key in node}
    for field in ("routes", "select", "when"):
        assert field in carried
        assert re.search(rf"\b{field}\b", kept), f"the preserve list does not name {field}: {kept!r}"


@pytest.mark.asyncio
async def test_workflow_compiler_rejects_a_guard_rewritten_into_another_valid_one() -> None:
    """The change the graph rules cannot see: ``r.b`` is as sound a guard as ``r.a``."""
    dag = _branching_dag()
    nodes = [node.model_dump(by_alias=True, exclude_none=True) for node in dag.nodes]
    nodes[1]["when"] = "r.b"
    emitted = _emitted_from(dag, nodes)
    provider = _Provider(_call(EMIT_WORKFLOW, emitted), _call(EMIT_WORKFLOW, emitted))

    compiled = await WorkflowCompiler(provider, "stub").compile(
        query="Triage the issue",
        dag=dag,
        run_id="pb-rewritten-guard",
        harness=_harness(),
    )

    assert len(provider.calls) == 2
    assert "node 'g': when changed during compilation" in provider.calls[1]["messages"][-1]["content"]
    assert compiled.workflow is not None
    assert compiled.workflow.nodes == dag.nodes


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


# --------------------------------------------------------------------------- #
# Capturing a reassigned run                                                    #
# --------------------------------------------------------------------------- #


def _group_dag() -> SubAgentDagSpec:
    """One graph-named instance group, plus an ordinary node that ran alone."""
    return SubAgentDagSpec(
        task_summary="Triage the issue",
        confirm=True,
        nodes=[
            DagNodeSpec(
                id="first",
                subagent="alpha",
                node_summary="Classify the issue",
                prompt_template="Classify {{ ref:issue.md }}",
                routes={"bug": "it is a bug", "docs": "it is a docs gap"},
                select="many",
                inputs={"issue": {"file": "issue.md"}},
                skills=["triage"],
                instance="shared",
            ),
            DagNodeSpec(
                id="second",
                subagent="alpha",
                node_summary="Handle the bug",
                prompt_template="Handle the report",
                depends_on=["first"],
                when="first.bug",
                mcps=["mcp-one"],
                instance="shared",
            ),
            DagNodeSpec(
                id="third",
                subagent="alpha",
                node_summary="Summarise",
                prompt_template="Summarise the outcome",
                depends_on=["second"],
            ),
        ],
    )


def test_a_reassigned_group_is_captured_under_the_agent_its_session_ended_on() -> None:
    original = _group_dag()
    before = original.model_dump()
    capture = PlaybookRunCapture(query="triage the issue", capture_workflow=True)

    captured, harness = rewrite_completed_dag(
        original,
        harness=None,
        workers={},
        named_instance_groups={"first": ("alpha", "shared"), "second": ("alpha", "shared")},
        execution_nodes={
            "first": original.nodes[0],
            "second": original.nodes[1].model_copy(update={"subagent": "beta"}),
            "third": original.nodes[2].model_copy(update={"subagent": "beta"}),
        },
        instance_assignments={("alpha", "shared"): ("beta", "shared")},
    )
    record_completed_dag(
        captured,
        "run-1",
        capture,
        harness=harness,
        attempt_agents={"first": ["alpha"], "second": ["alpha", "beta"], "third": ["alpha", "beta"]},
    )
    evidence = capture.dags[-1]

    assert original.model_dump() == before, "the submitted graph is never rewritten in place"
    assert evidence.spec.nodes[0].id == original.nodes[0].id
    assert evidence.spec.nodes[0].prompt_template == original.nodes[0].prompt_template
    assert evidence.spec.nodes[0].subagent == "beta"
    assert evidence.spec.nodes[1].subagent == "beta", "one group, one session: the completed sibling follows"
    assert evidence.spec.nodes[0].instance == evidence.spec.nodes[1].instance == "shared"
    assert evidence.spec.nodes[2].subagent == "beta", "an ordinary node records its final effective agent"
    assert harness is None, "a run with no worker table has no Harness to extend"
    histories = evidence.attempt_agents
    assert histories is not None
    assert histories["first"] == ["alpha"], "the audit is not rewritten as replay advice"
    assert histories["second"] == ["alpha", "beta"]
    for index, node in enumerate(original.nodes):
        copied = evidence.spec.nodes[index].model_dump(exclude={"subagent", "instance"})
        assert copied == node.model_dump(exclude={"subagent", "instance"})
    assert (evidence.spec.task_summary, evidence.spec.confirm) == (original.task_summary, original.confirm)


def test_a_run_without_a_move_is_recorded_exactly_as_submitted() -> None:
    original = _group_dag()
    harness = _harness()
    captured, captured_harness = rewrite_completed_dag(
        original,
        harness=harness,
        workers={},
        named_instance_groups={"first": ("alpha", "shared"), "second": ("alpha", "shared")},
        execution_nodes={node.id: node for node in original.nodes},
        instance_assignments={("alpha", "shared"): ("alpha", "shared")},
    )
    capture = PlaybookRunCapture(query="triage the issue", capture_workflow=True)
    record_completed_dag(captured, "run-2", capture)

    assert captured is original
    assert captured_harness is harness
    assert capture.dags[-1].as_dict() == {
        "runId": "run-2",
        "spec": original.model_dump(by_alias=True, exclude_none=True),
    }


def _worker_harness() -> AgentPlaybookSpec:
    """Two roles on alpha, one of which already holds its first derived label."""
    return AgentPlaybookSpec(
        name="research-team",
        description="A focused research team",
        coordinator=CoordinatorEntry(brief="Own the turn", playbook={"memory": {"systemPrompt": "Coordinate."}}),
        delegate=[
            DelegateEntry(
                **{
                    "as": "writer",
                    "name": "alpha",
                    "brief": "Write the draft",
                    "playbook": {"memory": {"systemPrompt": "Write clearly."}},
                }
            ),
            DelegateEntry(
                **{
                    "as": "analyst",
                    "name": "alpha",
                    "brief": "Analyse the draft",
                    "playbook": {"capability": {"tools": ["read_file"]}},
                }
            ),
            DelegateEntry(**{"as": "writer--reassign-1", "name": "alpha", "brief": "An occupied label"}),
        ],
    )


def _worker_dag() -> SubAgentDagSpec:
    return SubAgentDagSpec(
        task_summary="Write the brief",
        nodes=[
            DagNodeSpec(id="draft", subagent="writer", node_summary="Draft", prompt_template="Write the draft"),
            DagNodeSpec(
                id="review",
                subagent="analyst",
                node_summary="Review",
                prompt_template="Review the draft",
                instance="shared",
            ),
            DagNodeSpec(
                id="edit",
                subagent="analyst",
                node_summary="Edit",
                prompt_template="Edit the draft",
                depends_on=["review"],
                instance="shared",
            ),
            DagNodeSpec(
                id="polish",
                subagent="writer",
                node_summary="Polish",
                prompt_template="Polish the draft",
                depends_on=["draft"],
            ),
        ],
    )


def _worker_workers() -> dict[str, Worker]:
    return {
        "draft": Worker("writer", "alpha", "Write the draft"),
        "review": Worker("analyst", "alpha", "Analyse the draft"),
        "edit": Worker("analyst", "alpha", "Analyse the draft"),
        "polish": Worker("writer", "alpha", "Write the draft"),
    }


def _worker_projection() -> tuple[AgentPlaybookSpec, SubAgentDagSpec, SubAgentDagSpec, AgentPlaybookSpec]:
    harness = _worker_harness()
    original = _worker_dag()
    captured, captured_harness = rewrite_completed_dag(
        original,
        harness=harness,
        workers=_worker_workers(),
        named_instance_groups={"review": ("alpha", "shared"), "edit": ("alpha", "shared")},
        execution_nodes={
            "draft": original.nodes[0].model_copy(update={"subagent": "beta"}),
            # The completed sibling was never rebuilt; the group's final owner still governs it.
            "review": original.nodes[1].model_copy(update={"subagent": "beta"}),
            "edit": original.nodes[2],
            "polish": original.nodes[3].model_copy(update={"subagent": "beta"}),
        },
        instance_assignments={("alpha", "shared"): ("beta", "shared")},
    )
    assert captured_harness is not None
    return harness, original, captured, captured_harness


def test_a_reassigned_worker_node_gets_one_derived_row_per_role_and_agent() -> None:
    harness, original, captured, captured_harness = _worker_projection()

    assert [node.subagent for node in captured.nodes] == [
        "writer--reassign-2",
        "analyst--reassign-1",
        "analyst--reassign-1",
        "writer--reassign-2",
    ], "one label per (role, target), collision-free, in submitted-node order"
    assert [entry.label for entry in captured_harness.delegate] == [
        "writer",
        "analyst",
        "writer--reassign-1",
        "writer--reassign-2",
        "analyst--reassign-1",
    ]
    assert captured_harness.delegate[:3] == harness.delegate, "original rows are left alone"
    assert len(harness.delegate) == 3, "the frozen source Harness is never mutated"
    assert captured_harness.coordinator == harness.coordinator
    assert captured_harness.name == harness.name
    assert captured_harness.description == harness.description
    derived = {entry.label: entry for entry in captured_harness.delegate[3:]}
    assert {entry.name for entry in derived.values()} == {"beta"}
    source = {entry.label: entry for entry in harness.delegate}
    for label, role in (("writer--reassign-2", "writer"), ("analyst--reassign-1", "analyst")):
        assert derived[label].brief == source[role].brief
        assert derived[label].playbook == source[role].playbook
        assert derived[label].playbook is not source[role].playbook
    assert [node.instance for node in captured.nodes[1:3]] == ["shared", "shared"]
    for index, node in enumerate(original.nodes):
        copied = captured.nodes[index].model_dump(exclude={"subagent", "instance"})
        assert copied == node.model_dump(exclude={"subagent", "instance"})


def test_a_worker_node_that_returns_to_its_original_agent_keeps_its_role_row() -> None:
    harness = _worker_harness()
    submitted = SubAgentDagSpec(
        task_summary="Write the draft",
        nodes=[DagNodeSpec(id="draft", subagent="writer", node_summary="Draft", prompt_template="Write the draft")],
    )

    captured, captured_harness = rewrite_completed_dag(
        submitted,
        harness=harness,
        workers={"draft": Worker("writer", "alpha", "Write the draft")},
        named_instance_groups={},
        execution_nodes={"draft": submitted.nodes[0].model_copy(update={"subagent": "alpha"})},
        instance_assignments={},
    )

    assert captured is submitted, "the original role row already names the agent the run ended on"
    assert captured_harness is harness


def test_a_moved_ordinary_node_never_captures_a_minted_handle() -> None:
    submitted = SubAgentDagSpec(
        task_summary="Write the draft",
        nodes=[DagNodeSpec(id="solo", subagent="alpha", node_summary="Draft", prompt_template="Write the draft")],
    )

    captured, harness = rewrite_completed_dag(
        submitted,
        harness=None,
        workers={},
        named_instance_groups={},
        execution_nodes={"solo": submitted.nodes[0].model_copy(update={"subagent": "beta", "instance": "solo-minted"})},
        instance_assignments={},
    )

    assert captured.nodes[0].subagent == "beta"
    assert captured.nodes[0].instance is None, "a runtime-minted handle is not a name the graph gave"


def test_the_effective_node_wins_over_a_differing_audit_history() -> None:
    """The audit records every attempt; the replay follows the dispatch graph."""
    submitted = SubAgentDagSpec(
        task_summary="Write the draft",
        nodes=[DagNodeSpec(id="solo", subagent="alpha", node_summary="Draft", prompt_template="Write the draft")],
    )

    captured, _harness = rewrite_completed_dag(
        submitted,
        harness=None,
        workers={},
        named_instance_groups={},
        execution_nodes={"solo": submitted.nodes[0].model_copy(update={"subagent": "beta"})},
        instance_assignments={},
    )
    capture = PlaybookRunCapture(query="write the draft", capture_workflow=True)
    record_completed_dag(
        captured,
        "run-1",
        capture,
        attempt_agents={"solo": ["alpha", "beta", "alpha"]},
    )

    assert captured.nodes[0].subagent == "beta", "the effective dispatch decides the replay agent"
    assert capture.dags[-1].attempt_agents == {"solo": ["alpha", "beta", "alpha"]}


def test_a_node_missing_from_the_effective_graph_keeps_its_submitted_identity() -> None:
    """Audit history is not a substitute for effective dispatch: with no entry in
    the effective graph, the node's own agent stands, never its last attempt."""
    submitted = SubAgentDagSpec(
        task_summary="Write the draft",
        nodes=[DagNodeSpec(id="solo", subagent="alpha", node_summary="Draft", prompt_template="Write the draft")],
    )

    captured, _harness = rewrite_completed_dag(
        submitted,
        harness=None,
        workers={},
        named_instance_groups={},
        execution_nodes={},
        instance_assignments={},
    )
    capture = PlaybookRunCapture(query="write the draft", capture_workflow=True)
    record_completed_dag(
        captured,
        "run-1",
        capture,
        attempt_agents={"solo": ["alpha", "beta"]},
    )

    assert captured.nodes[0].subagent == "alpha", "the last attempt is not a fallback for the dispatch graph"
    assert capture.dags[-1].attempt_agents == {"solo": ["alpha", "beta"]}


def test_a_group_sibling_missing_from_the_effective_graph_keeps_the_groups_final_owner() -> None:
    """History is not a substitute for the group's final pair: a sibling that ran
    on the old agent must still replay inside the one session the group ended on."""
    original = _group_dag()

    captured, _harness = rewrite_completed_dag(
        original,
        harness=None,
        workers={},
        named_instance_groups={"first": ("alpha", "shared"), "second": ("alpha", "shared")},
        execution_nodes={"second": original.nodes[1].model_copy(update={"subagent": "beta"})},
        instance_assignments={("alpha", "shared"): ("beta", "shared")},
    )
    capture = PlaybookRunCapture(query="triage the issue", capture_workflow=True)
    record_completed_dag(
        captured,
        "run-1",
        capture,
        attempt_agents={"first": ["alpha"], "second": ["alpha", "beta"]},
    )

    assert captured.nodes[0].subagent == "beta", "the last audit attempt is not the replay agent"
    assert captured.nodes[0].instance == "shared"
    assert capture.dags[-1].attempt_agents == {"first": ["alpha"], "second": ["alpha", "beta"]}


@pytest.mark.asyncio
async def test_the_projected_graph_compiles_against_the_extended_harness() -> None:
    _harness_before, _original, captured, captured_harness = _worker_projection()
    emitted = _emitted_from(captured, [node.model_dump(by_alias=True, exclude_none=True) for node in captured.nodes])
    provider = _Provider(_call(EMIT_WORKFLOW, emitted))

    compiled = await WorkflowCompiler(provider, "stub").compile(
        query="Write the brief",
        dag=captured,
        run_id="pb-reassigned",
        harness=captured_harness,
    )

    assert _preservation_errors(captured, compiled) == []
    assert compiled.workflow is not None
    assert [node.subagent for node in compiled.workflow.nodes] == [
        "writer--reassign-2",
        "analyst--reassign-1",
        "analyst--reassign-1",
        "writer--reassign-2",
    ]
    assert compiled.harness == captured_harness
    payload = json.loads(provider.calls[0]["messages"][1]["content"])
    assert payload["workerAliases"] == [entry.label for entry in captured_harness.delegate]
    assert payload["acceptedDag"]["nodes"][0]["subagent"] == "writer--reassign-2"


def test_the_run_record_audit_keeps_factual_attempt_histories() -> None:
    _harness_before, _original, captured, captured_harness = _worker_projection()
    capture = PlaybookRunCapture(query="write the brief", capture_workflow=True)

    record_completed_dag(
        captured,
        "run-3",
        capture,
        harness=captured_harness,
        attempt_agents={
            "draft": ["alpha", "beta"],
            "review": ["alpha", "beta"],
            "edit": ["alpha"],
            "polish": ["alpha", "beta"],
        },
    )

    payload = capture.dags[-1].as_dict()
    assert payload["spec"] == captured.model_dump(by_alias=True, exclude_none=True)
    assert payload["harness"] == captured_harness.model_dump(by_alias=True, exclude_none=True)
    assert payload["attemptAgents"] == {
        "draft": ["alpha", "beta"],
        "review": ["alpha", "beta"],
        "edit": ["alpha"],
        "polish": ["alpha", "beta"],
    }
    assert payload["attemptAgents"]["edit"] == ["alpha"], "a moved group's finished sibling keeps its own history"
