"""Bounded planning, source queries and repair operate on host-owned contracts."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from experimental.curator.generation.context.collect import collect
from experimental.curator.generation.run import GenerationError, Limits, generate
from experimental.curator.harness import Declaration, Validation
from experimental.curator.raven_adapter.targets import catalogue
from raven.contracts.llm_provider import LLMResponse, RunMeta, ToolCallRequest
from raven.contracts.tool import RAW_ARGUMENTS_KEY


class Provider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def chat_with_retry(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def packet(request):
    """A stage request's data: the curation's materials, then the stage's own data."""
    import json

    messages = request["messages"]
    return {**json.loads(messages[1]["content"]), **json.loads(messages[3]["content"])}


def instructions(request):
    """A stage request's host instructions: the shared rules, then the stage's instructions and actions."""
    return request["messages"][0]["content"] + "\n\n" + request["messages"][2]["content"]


def response(name, arguments):
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(name, name, arguments)])


def selection(*targets):
    return {"understanding": "Current task, observed need and candidate rationale", "targets": list(targets)}


def plan(*targets):
    return {
        "understanding": "Current task and native mechanisms",
        "design": "Connect the chosen operations to native consumers and preserve task-owned state.",
        "changes": [
            {
                "target": name,
                "reason": "Observed need",
                "expected": "Improve behavior",
                "verification": "Inspect actual calls",
            }
            for name in targets
        ],
    }


@pytest.fixture
def context():
    declaration = Declaration("worker@0", catalogue())
    return collect(
        "Complete the current task",
        declaration,
        facts={
            "backend": "AgentLoop",
            "tools": [],
            "configuration": {"config": {"agents": {"defaults": {"temperature": 0.1}}}},
            "authored": {"values": {}, "files": {"rules.py": "", "shared.py": ""}},
        },
        sources={"loop": {"start": 1, "end": 30}},
        read_source=lambda **kwargs: {"source": kwargs["name"], "text": "native protocol"},
        feedback={"source": "human", "text": "The prior behavior needs a reliable check."},
    )


@pytest.mark.asyncio
async def test_plan_queries_native_materials_then_generates_combined_artifacts(context):
    provider = Provider(
        response("read_source", {"name": "loop", "length": 1000}),
        response("read_fact", {"name": "configuration", "path": ["config", "agents", "defaults"]}),
        response("submit_selection", selection("action.config", "planning.skills")),
        response("submit_plan", plan("action.config", "planning.skills")),
        response(
            "submit_artifact",
            {
                "values": {
                    "action.config": {"temperature": 0.2},
                    "planning.skills": {
                        "guide/SKILL.md": "---\nname: guide\ndescription: A task guide\n---\nUse evidence."
                    },
                }
            },
        ),
    )
    checked = []

    async def validate(candidate):
        checked.append(candidate)
        return Validation([], [{"kind": "runtime.bound"}])

    result = await generate(context, provider, validate=validate)
    assert result.candidate is checked[0]
    assert set(result.candidate.artifact.values) == {"action.config", "planning.skills"}
    assert [event["tool"] for event in result.trace if event["event"] == "query"] == ["read_source", "read_fact"]
    assert "human" in provider.requests[0]["messages"][1]["content"]
    assert "Background" in provider.requests[0]["messages"][0]["content"]
    assert "Understand" in provider.requests[0]["messages"][2]["content"]


@pytest.mark.asyncio
async def test_validation_can_change_the_mechanism_instead_of_only_rewriting_code(context):
    provider = Provider(
        response("submit_selection", selection("planning.advise")),
        response("submit_plan", plan("planning.advise")),
        response("submit_artifact", {"values": {"planning.advise": "rules:Participant"}}),
        response("revise_selection", selection("action.review")),
        response("submit_plan", plan("action.review")),
        response(
            "submit_artifact",
            {"values": {"action.review": "checks:Participant"}, "files": {"checks.py": "class Participant: pass"}},
        ),
    )
    calls = []

    async def validate(candidate):
        calls.append(candidate)
        if len(calls) == 1:
            return Validation(["Guidance did not enforce the required condition."], [{"kind": "observed.gap"}])
        return Validation([], [{"kind": "runtime.bound"}])

    result = await generate(context, provider, validate=validate)
    assert result.candidate.plan.changes[0].target == "action.review"
    repair_request = json.dumps(packet(provider.requests[3]), ensure_ascii=False)
    assert "Guidance did not enforce" in repair_request
    assert "observed.gap" in repair_request
    assert any(event["event"] == "revise_selection" for event in result.trace)


@pytest.mark.asyncio
async def test_implementation_can_return_to_design_before_validation(context):
    provider = Provider(
        response("submit_selection", selection("planning.advise")),
        response("submit_plan", plan("planning.advise")),
        response("revise_selection", selection("memory.prompt")),
        response("submit_plan", plan("memory.prompt")),
        response("submit_artifact", {"values": {"memory.prompt": {"TOOLS.md": "Native tool guidance"}}}),
    )

    async def validate(candidate):
        return Validation([], [])

    result = await generate(context, provider, validate=validate)
    assert result.candidate.plan.changes[0].target == "memory.prompt"


@pytest.mark.asyncio
async def test_empty_plan_retains_the_existing_harness_only_after_host_validation(context):
    provider = Provider(response("submit_selection", selection()))
    checked = []

    async def validate(candidate):
        checked.append(candidate)
        return Validation([], [{"kind": "probe.passed"}])

    result = await generate(context, provider, validate=validate)
    assert not result.candidate.plan.changes
    assert result.candidate.artifact.values == {}
    assert checked == [result.candidate]
    assert result.validation.observations == [{"kind": "probe.passed"}]


@pytest.mark.asyncio
async def test_failed_unchanged_candidate_enters_repair_and_can_reselect(context):
    provider = Provider(
        response("submit_selection", selection()),
        response("revise_selection", selection("action.config")),
        response("submit_plan", plan("action.config")),
        response("submit_artifact", {"values": {"action.config": {"temperature": 0.2}}}),
    )
    checked = []
    evidence = {"kind": "action.error", "operation": "decision", "arguments": [{"status": "ready"}]}

    async def validate(candidate):
        checked.append(candidate)
        return Validation(["recorded contract failure"], [evidence]) if len(checked) == 1 else Validation([], [])

    result = await generate(context, provider, validate=validate)
    assert len(checked) == 2
    assert not checked[0].artifact.values
    assert result.candidate.artifact.values == {"action.config": {"temperature": 0.2}}
    assert "recorded contract failure" in str(provider.requests[1]["messages"])
    assert "ready" in str(provider.requests[1]["messages"])
    assert [event["stage"] for event in result.trace if event["event"] == "model.call"] == [
        "select",
        "repair",
        "design",
        "implement",
    ]


@pytest.mark.asyncio
async def test_failed_unchanged_candidate_cannot_bypass_repair_budget(context):
    async def validate(candidate):
        return Validation(["recorded contract failure"], [])

    with pytest.raises(GenerationError, match="repair budget exhausted"):
        await generate(
            context,
            Provider(response("submit_selection", selection())),
            validate=validate,
            limits=Limits(max_repairs=0),
        )


@pytest.mark.asyncio
async def test_invalid_selection_is_returned_to_the_model_and_still_cannot_expand_grants(context):
    context = collect(
        context.task,
        context.declaration.restrict(["planning.advise"]),
        facts=context.facts,
        sources=context.sources,
        read_source=context.read_source,
    )
    provider = Provider(
        response("submit_selection", selection("action.hooks")),
        response("submit_selection", selection("planning.advise")),
        response("submit_plan", plan("planning.advise")),
        response("submit_artifact", {"values": {"planning.advise": "rules:Participant"}}),
    )

    async def validate(candidate):
        return Validation([], [])

    result = await generate(context, provider, validate=validate)
    assert result.candidate.plan.changes[0].target == "planning.advise"
    assert any(event["event"] == "output.rejected" for event in result.trace)


@pytest.mark.asyncio
async def test_query_budget_refuses_more_reads_without_discarding_the_generation(context):
    from dataclasses import replace

    def unexpected_read(**kwargs):
        pytest.fail("the exhausted query budget must not execute the reader")

    context = replace(context, read_source=unexpected_read)
    provider = Provider(
        response("read_source", {"name": "not-registered"}),
        response("read_source", {"name": "loop"}),
        response("submit_selection", selection("action.config")),
        response("submit_plan", plan("action.config")),
        response("submit_artifact", {"values": {"action.config": {"temperature": 0.2}}}),
    )

    async def validate(candidate):
        return Validation([], [])

    result = await generate(context, provider, validate=validate, limits=Limits(max_queries=1))
    reads = [row for row in result.trace if row["event"] == "query"]
    assert len(reads) == 1 and "error" in reads[0]["result"]
    refused = [row for row in result.trace if row["event"] == "query.rejected"]
    assert len(refused) == 1 and "budget exhausted" in refused[0]["result"]["error"]
    assert result.validation.passed


@pytest.mark.asyncio
async def test_repair_and_total_call_budgets_are_program_controlled(context):
    provider = Provider(
        response("submit_selection", selection("planning.advise")),
        response("submit_plan", plan("planning.advise")),
        response("submit_artifact", {"values": {"planning.advise": "rules:Participant"}}),
    )

    async def validate(candidate):
        return Validation(["still invalid"], [])

    with pytest.raises(GenerationError, match="repair budget"):
        await generate(context, provider, validate=validate, limits=Limits(max_repairs=0))

    provider = Provider(LLMResponse(content="I will do it."))
    with pytest.raises(GenerationError, match="call budget"):
        await generate(context, provider, validate=validate, limits=Limits(max_calls=1))


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_into_model_repair(context):
    class Cancelled:
        async def chat_with_retry(self, **kwargs):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await generate(context, Cancelled(), validate=None)


@pytest.mark.asyncio
async def test_mixed_read_and_submission_is_rejected_as_an_ambiguous_handoff(context):
    provider = Provider(
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest("q", "read_fact", {"name": "configuration"}),
                ToolCallRequest("p", "submit_selection", selection()),
            ],
        ),
        response("submit_selection", selection()),
    )
    result = await generate(context, provider, validate=AsyncMock(return_value=Validation([], [])))
    assert not result.candidate.plan.changes
    assert any(event["event"] == "output.rejected" for event in result.trace)


@pytest.mark.asyncio
async def test_missing_native_capability_is_reported_without_a_fake_success(context):
    provider = Provider(response("report_gap", {"reason": "The required host binding is unavailable."}))
    with pytest.raises(GenerationError, match="binding is unavailable") as caught:
        await generate(context, provider, validate=None)
    assert caught.value.trace[-1]["event"] == "report_gap"


@pytest.mark.asyncio
async def test_stage_requests_keep_shared_consumers_readings_and_actual_actions(context):
    import re
    from dataclasses import replace

    authored = {
        "values": {"planning.advise": "shared:Guide", "action.review": "shared:Review"},
        "files": {"shared.py": "class Guide: pass\nclass Review: pass\n"},
    }
    context = replace(context, facts={**context.facts, "authored": authored})
    provider = Provider(
        response("read_source", {"name": "loop"}),
        response("submit_selection", selection("planning.advise")),
        response("submit_plan", plan("planning.advise")),
        response("submit_artifact", {"values": {"planning.advise": "shared:Guide"}}),
        response("submit_artifact", {"values": {"planning.advise": "shared:Guide"}}),
    )
    calls = 0

    async def validate(candidate):
        nonlocal calls
        calls += 1
        return Validation(["A shared consumer is incompatible"] if calls == 1 else [], [])

    await generate(context, provider, validate=validate)
    for request in provider.requests:
        data = packet(request)
        assert data["current_authored"] == authored
        text = instructions(request)
        actions = text.split("# Available actions\n\n", 1)[1]
        names = re.findall(r"^- `([^`]+)`:", actions, re.M)
        assert set(names) <= {entry["function"]["name"] for entry in request["tools"]}
        assert request["tools"] == provider.requests[0]["tools"]
        assert request["messages"][:2] == provider.requests[0]["messages"][:2]
        assert "shared.py" not in text
    planning_text = instructions(provider.requests[0])
    implementation_text = instructions(provider.requests[3])
    repair_text = instructions(provider.requests[4])
    assert "# Understand the current task" in planning_text
    assert "# Implement the selected plan" not in planning_text
    assert "# Repair from host validation" not in implementation_text
    assert "# Implement the selected plan" in repair_text and "# Repair from host validation" in repair_text
    for request in provider.requests[2:]:
        data = packet(request)
        assert next(row for row in data["history"] if row["event"] == "query")["result"]["text"] == "native protocol"
        assert [item["target"] for item in data["selected_contracts"]] == ["planning.advise"]
    assert packet(provider.requests[4])["validation_errors"]


def test_orientation_is_complete_and_uses_the_inspected_source_version(context, tmp_path):
    from dataclasses import replace

    from experimental.curator.generation.stages.understand import materials
    from experimental.curator.raven_adapter.inspection import Inspection, file_source

    text = "Host mechanism background.\n" * 1500 + "FINAL_REQUIRED_CONDITION"
    path = tmp_path / "index.md"
    path.write_text(text)
    inspection = Inspection(context.declaration, {}, {"reference.index": file_source(path)})
    context = replace(context, sources=inspection.sources, read_source=inspection.read_source)
    data = materials(context)
    assert data["orientation"]["text"] == text
    assert data["orientation"]["digest"] == inspection.sources["reference.index"]["digest"]
    path.write_text("Changed after inspection")
    with pytest.raises(ValueError, match="source changed"):
        materials(context)


def test_rendered_delivery_actions_follow_the_supplied_tool_instead_of_a_static_name():
    from experimental.curator.generation.context.render import messages, tool

    action = tool("host_candidate_v2", "Submit the current candidate.", {"type": "object"})
    other = tool("submit_selection", "Submit a selection.", {"type": "object"})
    rendered = messages(("implement",), {"task": "A task"}, {}, tools=[action, other], available={"host_candidate_v2"})
    text = rendered[2]["content"]
    assert "`host_candidate_v2`: Submit the current candidate." in text
    assert "submit_artifact" not in text and "submit_selection" not in text


def test_dynamic_control_uses_host_inputs_and_keeps_task_materials_in_the_data_message():
    import json

    from experimental.curator.generation.context.render import messages, tool

    material = {
        "task": "USER_TASK_MARKER ${actions}",
        "feedback": "FEEDBACK_MARKER",
        "selected_contracts": [{"knowledge": [{"content": "REFERENCE_MARKER"}]}],
    }
    action = tool("host_delivery", "Deliver through ${literal_host_text}.", {"type": "object"})
    stage = {"selected_contracts": material.pop("selected_contracts")}
    result = messages(("implement", "repair"), material, stage, tools=[action], available={"host_delivery"})
    assert [message["role"] for message in result] == ["system", "user", "user", "user"]
    assert "implement → repair" in result[2]["content"]
    assert "Deliver through ${literal_host_text}." in result[2]["content"]
    for marker in ("USER_TASK_MARKER", "FEEDBACK_MARKER", "REFERENCE_MARKER"):
        assert marker not in result[0]["content"] and marker not in result[2]["content"]
    assert json.loads(result[1]["content"]) == material and json.loads(result[3]["content"]) == stage


def test_artifact_wire_normalization_preserves_file_text_and_authority(context):
    import json

    from experimental.curator.generation.stages.implement import parse

    selected = context.declaration.parse_plan(plan("action.config"))
    file_text = '{"keep": "this is file content, not a container to decode"}'
    candidate = parse(
        context.declaration,
        selected,
        {
            "values": json.dumps({"action.config": {"temperature": 0.2}}),
            "files": json.dumps({"data.json": file_text}),
        },
    )
    assert candidate.artifact.values == {"action.config": {"temperature": 0.2}}
    assert candidate.artifact.files["data.json"] == file_text
    with pytest.raises(ValueError):
        parse(context.declaration, selected, {"values": json.dumps({"capability.tools": []})})
    with pytest.raises(ValueError):
        parse(context.declaration, selected, {"values": "not JSON"})
    with pytest.raises(ValueError):
        parse(context.declaration, selected, {"values": "[]"})


def test_implementation_keeps_other_granted_choices_visible_for_replanning(context):
    from experimental.curator.generation.stages.implement import materials

    selected = context.declaration.parse_plan(plan("action.strategy"))
    from experimental.curator.generation.stages.select import materials as catalogue_of

    data = materials(context, context.declaration.parse_selection(selection("action.strategy")), selected)
    assert [row["target"] for row in data["selected_contracts"]] == ["action.strategy"]
    assert data["selected_contracts"][0]["knowledge"] and data["plan"]["changes"][0]["target"] == "action.strategy"
    assert "available_targets" not in data
    available = catalogue_of(context.declaration)
    assert "prompt.resources" in {row["target"] for row in available}
    assert all("knowledge" not in row for row in available)


@pytest.mark.asyncio
async def test_three_stages_preserve_diagnosis_queries_failures_and_concrete_design(context):

    chosen = selection("action.config")
    chosen["understanding"] = "Sampling is unstable; retain task state. Check the temperature reader."
    designed = plan("action.config")
    designed["design"] = "Set temperature to 0.2 through native defaults; preserve every other setting."
    note = "The first lookup failed; the registered loop source supplies the needed reader."
    provider = Provider(
        response("read_source", {"name": "missing"}),
        response("submit_selection", chosen),
        LLMResponse(content=note, tool_calls=[ToolCallRequest("read", "read_source", {"name": "loop"})]),
        response("submit_plan", designed),
        response("submit_artifact", {"values": {"action.config": {"temperature": 0.2}}}),
    )

    async def validate(candidate):
        assert candidate.plan.design == designed["design"]
        return Validation([], [])

    result = await generate(context, provider, validate=validate)
    assert [row["stage"] for row in result.trace if row["event"] == "model.call"] == [
        "select",
        "select",
        "design",
        "design",
        "implement",
    ]
    initial, design_request, implementation = (provider.requests[i] for i in (0, 2, 4))
    assert initial["tools"] == design_request["tools"] == implementation["tools"]
    initial_actions, design_actions = instructions(initial), instructions(design_request)
    assert "`submit_selection`" in initial_actions and "`submit_plan`" not in initial_actions
    assert "`submit_plan`" in design_actions and "`submit_artifact`" not in design_actions
    initial_data = packet(initial)
    assert all("knowledge" not in target for target in initial_data["available_targets"])
    for request in (design_request, implementation):
        data = packet(request)
        assert data["task"] == context.task and data["feedback"] == context.feedback
        assert data["selection"] == chosen
        assert "error" in data["history"][0]["result"]
        assert data["history"][1]["output"] == chosen
    data = packet(implementation)
    assert data["plan"]["design"] == designed["design"]
    assert any(row.get("content") == note for row in data["history"])
    assert any(row.get("result", {}).get("text") == "native protocol" for row in data["history"])


@pytest.mark.asyncio
async def test_design_cannot_change_targets_or_skip_the_concrete_mechanism(context):
    missing_design = plan("action.config")
    missing_design.pop("design")
    provider = Provider(
        response("submit_selection", selection("action.config")),
        response("submit_plan", plan("memory.prompt")),
        response("submit_plan", missing_design),
        response("submit_plan", plan("action.config")),
        response("submit_artifact", {"values": {"action.config": {"temperature": 0.2}}}),
    )

    async def validate(candidate):
        return Validation([], [])

    result = await generate(context, provider, validate=validate)
    errors = [row["error"] for row in result.trace if row["event"] == "output.rejected"]
    assert len(errors) == 2
    assert "exactly the current selection" in errors[0]
    assert "concrete design" in errors[1]


@pytest.mark.asyncio
async def test_design_can_reselect_dependencies_but_must_design_the_new_selection(context):

    provider = Provider(
        response("submit_selection", selection("action.strategy")),
        response("revise_selection", selection("action.strategy", "prompt.resources")),
        response("submit_plan", plan("action.strategy")),
        response("submit_plan", plan("action.strategy", "prompt.resources")),
        response(
            "submit_artifact", {"values": {"action.strategy": {"factory": "rules:create"}, "prompt.resources": []}}
        ),
    )

    async def validate(candidate):
        return Validation([], [])

    result = await generate(context, provider, validate=validate)
    data = packet(provider.requests[2])
    assert {row["target"] for row in data["selected_contracts"]} == {"action.strategy", "prompt.resources"}
    assert "plan" not in data and "candidate" not in data
    assert len(result.candidate.plan.changes) == 2
    assert any(row["event"] == "output.rejected" for row in result.trace)


@pytest.mark.asyncio
async def test_redesign_retains_failed_candidate_evidence_but_clears_active_draft(context, tmp_path):

    from experimental.curator.raven_adapter.exploration import Exploration
    from experimental.curator.raven_adapter.inspection import Inspection
    from raven.config.schema import Config

    previous = plan("action.config")
    previous["design"] = "OLD_DESIGN"
    revised = {**previous, "design": "NEW_DESIGN addresses the observed failure"}
    old = {"values": {"action.config": {"temperature": 0.4}}, "files": {"old.py": "OLD_DRAFT = True\n"}}
    fixed = {"values": {"action.config": {"temperature": 0.2}}}
    probe_evidence = {"kind": "probe.failed", "details": "OLD_EVIDENCE"}
    provider = Provider(
        response("submit_selection", selection("action.config")),
        response("submit_plan", previous),
        response("check_candidate", old),
        response("submit_artifact", old),
        response("revise_plan", {"reason": "The design did not account for the observed condition."}),
        response("submit_plan", revised),
        response("submit_artifact", fixed),
    )
    checks = 0

    async def validate(candidate):
        nonlocal checks
        checks += 1
        return Validation(["OLD_FAILURE"], [probe_evidence]) if checks <= 2 else Validation([], [])

    inspection = Inspection(context.declaration, {}, {})
    async with Exploration(Config(), inspection, repository=tmp_path, source_paths=()) as exploration:

        def stage(candidate):
            result = exploration.stage_candidate(candidate)
            if candidate is None:
                assert not (exploration.root / "candidate").exists()
            return result

        result = await generate(context, provider, validate=validate, stage_candidate=stage)
        assert result.candidate.plan.design == revised["design"]
        assert not list((exploration.root / "candidate").rglob("old.py"))
    data = packet(provider.requests[5])
    assert "plan" not in data and "candidate" not in data
    history = data["history"]
    assert any(
        row["event"] == "submit_artifact" and row["output"]["artifact"] == {**old, "remove": [], "remove_paths": {}}
        for row in history
    )
    assert any(row["event"] == "validation" and row["observations"] == [probe_evidence] for row in history)
    assert any(row["event"] == "preflight" and row["arguments"] == old for row in history)
    assert history[-1]["event"] == "revise_plan"
    assert result.validation.passed and checks == 3


@pytest.mark.asyncio
async def test_reselection_remains_inside_grants_and_backtracking_uses_total_budget(context):
    from dataclasses import replace

    restricted = replace(context, declaration=context.declaration.restrict(["action.config"]))
    provider = Provider(
        response("submit_selection", selection("action.config")),
        response("revise_selection", selection("memory.prompt")),
        response("revise_selection", selection("action.config")),
    )
    with pytest.raises(GenerationError, match="call budget") as caught:
        await generate(restricted, provider, validate=None, limits=Limits(max_calls=3))
    assert any(row["event"] == "output.rejected" and "not granted" in row["error"] for row in caught.value.trace)
    assert [row["stage"] for row in caught.value.trace if row["event"] == "model.call"] == [
        "select",
        "design",
        "design",
    ]


@pytest.mark.asyncio
async def test_remaining_budget_tracks_queries_checks_and_repairs_across_stages(context):
    class BudgetProvider(Provider):
        def __init__(self, *responses):
            super().__init__(*responses)
            self.budgets = []
            self.sent = []

        async def chat_with_retry(self, **kwargs):
            sent = kwargs["messages"]
            assert "# Remaining generation budget" not in sent[0]["content"]
            assert sum("# Remaining generation budget" in str(row.get("content")) for row in sent) == 1
            assert sent[-1]["role"] == "user"
            self.budgets.append(sent[-1]["content"].split("# Remaining generation budget", 1)[1])
            self.sent.append(sent)
            return await super().chat_with_retry(**kwargs)

    artifact = {"values": {"action.config": {"temperature": 0.2}}}
    provider = BudgetProvider(
        response("submit_selection", selection("action.config")),
        response("read_source", {"name": "loop"}),
        response("submit_plan", plan("action.config")),
        response("check_candidate", artifact),
        response("submit_artifact", artifact),
        response("submit_artifact", artifact),
    )
    checks = 0

    async def validate(candidate):
        nonlocal checks
        checks += 1
        return Validation(["repair this candidate"] if checks == 2 else [], [])

    await generate(
        context, provider, validate=validate, limits=Limits(max_calls=8, max_queries=4, max_checks=2, max_repairs=2)
    )
    for earlier, later in zip(provider.sent, provider.sent[1:]):
        assert later[:2] == earlier[:2]
        if earlier[2] == later[2]:
            assert later[: len(earlier) - 1] == earlier[:-1]
    for index, budget in enumerate(provider.budgets):
        assert f"including this response: {8 - index}" in budget
        assert f"Exploration tool calls: {4 if index < 2 else 3}" in budget
        assert f"Draft preflight checks: {2 if index < 4 else 1}" in budget
        assert f"failed final validation: {2 if index < 5 else 1}" in budget


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_index", [0, 1, 2])
async def test_invalid_arguments_preserve_stage_and_context_before_resubmission(context, stage_index):
    outputs = [
        response("submit_selection", selection("action.config")),
        response("submit_plan", plan("action.config")),
        response("submit_artifact", {"values": {"action.config": {"temperature": 0.2}}}),
    ]
    raw = '{"description": "Use verdict="resample" here"}'
    name = outputs[stage_index].tool_calls[0].name
    invalid = LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest("invalid", name, {RAW_ARGUMENTS_KEY: raw}, run_meta=RunMeta(arguments_repaired=True))
        ],
    )
    outputs.insert(stage_index, invalid)
    provider = Provider(*outputs)
    checked = []

    async def validate(candidate):
        checked.append(candidate)
        return Validation([], [])

    result = await generate(context, provider, validate=validate, limits=Limits(max_calls=4))
    assert len(checked) == 1 and result.candidate is checked[0]
    events = [event for event in result.trace if event["event"] == "output.rejected"]
    assert len(events) == 1
    assert events[0]["stage"] == ("select", "design", "implement")[stage_index]
    assert events[0]["calls"][0]["function"]["arguments"] == raw
    messages = provider.requests[stage_index + 1]["messages"]
    assistant = next(m for m in messages if m.get("tool_calls", [{}])[0].get("id") == "invalid")
    assert assistant["tool_calls"][0]["function"]["arguments"] == raw
    feedback = next(m for m in messages if m.get("tool_call_id") == "invalid")
    assert "Parser:" in feedback["content"] and "No tools" in feedback["content"]
    assert context.task in messages[1]["content"]
    assert len(provider.requests) == 4


def test_provider_reasoning_and_tool_metadata_survive_native_message_building():
    from experimental.curator.generation.context.render import assistant_message

    response = LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest("id", "query", {}, provider_specific_fields={"tag": "opaque"})],
        reasoning_content="synthetic reasoning",
        thinking_blocks=[{"type": "thinking", "signature": "opaque"}],
    )
    message = assistant_message(response)
    assert message["reasoning_content"] == "synthetic reasoning"
    assert message["thinking_blocks"] == response.thinking_blocks
    assert message["tool_calls"][0]["provider_specific_fields"] == {"tag": "opaque"}


@pytest.mark.asyncio
async def test_unparsable_response_can_be_resubmitted_through_curator_without_restarting(context):
    raw = '{"understanding": "Keep "existing" harness", "targets": []}'
    provider = Provider(
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    "selection-call",
                    "submit_selection",
                    {RAW_ARGUMENTS_KEY: raw},
                    run_meta=RunMeta(arguments_repaired=True),
                )
            ],
        ),
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    "selection-call",
                    "submit_selection",
                    {"understanding": "Existing harness is sufficient", "targets": []},
                )
            ],
        ),
    )
    checked = []

    async def validate(candidate):
        checked.append(candidate)
        return Validation([], [])

    result = await generate(context, provider, validate=validate, limits=Limits(max_calls=2))
    assert not result.candidate.plan.changes and checked == [result.candidate]
    assert len(provider.requests) == 2
    messages = provider.requests[1]["messages"]
    assert messages[-3]["tool_calls"][0]["function"]["arguments"] == raw
    assert messages[-2]["tool_call_id"] == "selection-call"
    assert "Parser:" in messages[-2]["content"]
    assert "# Remaining generation budget" in messages[-1]["content"]
    assert context.task in messages[1]["content"]
    assert [event["event"] for event in result.trace] == [
        "model.call",
        "output.rejected",
        "model.call",
        "submit_selection",
        "validation",
    ]


@pytest.mark.asyncio
async def test_invalid_call_blocks_entire_tool_batch_without_spending_query_budget(context):
    from unittest.mock import AsyncMock, Mock

    registry = Mock()
    registry.get_definitions.return_value = []
    registry.execute = AsyncMock()
    provider = Provider(
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest("valid", "native_probe", {"path": "."}),
                ToolCallRequest(
                    "invalid", "native_probe", {RAW_ARGUMENTS_KEY: "[1]"}, run_meta=RunMeta(arguments_repaired=True)
                ),
            ],
        ),
        response("read_source", {"name": "loop"}),
        response("submit_selection", selection()),
    )

    async def validate(candidate):
        assert not candidate.artifact.values
        return Validation([], [])

    registry.get.return_value = None
    result = await generate(context, provider, validate=validate, tool_registry=registry, limits=Limits(max_queries=1))
    registry.execute.assert_not_awaited()
    assert len([event for event in result.trace if event["event"] == "query"]) == 1
    feedback = [m for m in provider.requests[1]["messages"] if m.get("tool_call_id") in ("valid", "invalid")]
    assert len(feedback) == 2 and all("No tools" in m["content"] for m in feedback)
    assert not any(event["event"] == "query.rejected" for event in result.trace)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["raw", "repaired", "truncated"])
async def test_repeated_invalid_submissions_stop_at_call_budget(context, kind):
    invalid = response("submit_selection", selection())
    if kind == "raw":
        invalid.tool_calls[0].arguments = {RAW_ARGUMENTS_KEY: "{"}
    elif kind == "repaired":
        invalid.tool_calls[0].run_meta = RunMeta(arguments_repaired=True)
    else:
        invalid.truncated = True
    provider = Provider(invalid, invalid)

    async def validate(candidate):
        pytest.fail("Invalid submission must not reach validation")

    with pytest.raises(GenerationError, match="call budget exhausted") as failure:
        await generate(context, provider, validate=validate, limits=Limits(max_calls=2))
    assert len(provider.requests) == 2
    assert len([event for event in failure.value.trace if event["event"] == "output.rejected"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError("transport timeout"), RuntimeError("provider implementation bug")])
async def test_provider_failures_do_not_become_model_output_repairs(context, error):
    from unittest.mock import AsyncMock

    provider = Provider()
    provider.chat_with_retry = AsyncMock(side_effect=error)
    with pytest.raises(GenerationError, match="model call failed") as failure:
        await generate(context, provider, validate=AsyncMock())
    assert failure.value.__cause__ is error
    assert provider.chat_with_retry.await_count == 1
    assert not any(event["event"] == "output.rejected" for event in failure.value.trace)


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_after", [1, 2, 3, 4, 5, 6])
async def test_serialized_resume_preserves_every_stage_and_does_not_replay_work(context, pause_after):
    from experimental.curator.generation.run import GenerationPausedError
    from experimental.curator.generation.state import GenerationState

    draft = {"values": {"action.config": {"temperature": 0.2}}}
    provider = Provider(
        response("read_source", {"name": "loop"}),
        response("submit_selection", selection("action.config")),
        response("read_fact", {"name": "configuration"}),
        response("submit_plan", plan("action.config")),
        response("check_candidate", draft),
        response("submit_artifact", draft),
        response("submit_artifact", {"values": {"action.config": {"temperature": 0.4}}}),
    )
    checks = []

    async def validate(candidate):
        checks.append(candidate)
        return Validation(["Adjust temperature"] if len(checks) == 2 else [], [])

    with pytest.raises(GenerationPausedError) as paused:
        await generate(context, provider, validate=validate, limits=Limits(max_calls=pause_after))
    state = GenerationState.model_validate_json(paused.value.state.model_dump_json())
    assert state.calls == pause_after
    assert (
        state.stage == {1: "select", 2: "design", 3: "design", 4: "implement", 5: "implement", 6: "repair"}[pause_after]
    )
    if pause_after == 5:
        assert state.checks == 1
    if pause_after == 6:
        assert state.repairs == 1 and state.validation.errors == ["Adjust temperature"]
    saved_messages = state.model_copy(deep=True).messages
    resumed = Provider(*provider.responses)
    result = await generate(context, resumed, validate=validate, limits=Limits(max_calls=7), resume=state)
    assert result.candidate.artifact.values["action.config"] == {"temperature": 0.4}
    assert len(checks) == 3
    assert len(provider.requests) + len(resumed.requests) == 7
    assert state.calls == pause_after and state.messages == saved_messages
    sent = resumed.requests[0]["messages"]
    assert sent[:-1] == saved_messages and "# Remaining generation budget" in sent[-1]["content"]
    assert len([event for event in result.trace if event["event"] == "query"]) == 2
    assert [event["call"] for event in result.trace if event["event"] == "model.call"] == list(range(1, 8))


@pytest.mark.asyncio
async def test_resumption_does_not_implicitly_refill_any_budget(context):
    from experimental.curator.generation.run import GenerationPausedError

    provider = Provider(response("read_source", {"name": "loop"}))
    with pytest.raises(GenerationPausedError) as paused:
        await generate(context, provider, validate=None, limits=Limits(max_calls=1, max_queries=1))
    state = paused.value.state
    with pytest.raises(GenerationPausedError):
        await generate(context, Provider(), validate=None, limits=Limits(max_calls=1, max_queries=1), resume=state)
    resumed = Provider(response("read_source", {"name": "loop"}), response("submit_selection", selection()))
    result = await generate(
        context,
        resumed,
        validate=AsyncMock(return_value=Validation([], [])),
        limits=Limits(max_calls=3, max_queries=1),
        resume=state,
    )
    assert len([event for event in result.trace if event["event"] == "query"]) == 1
    assert len([event for event in result.trace if event["event"] == "query.rejected"]) == 1
    with pytest.raises(ValueError, match="consumed budgets"):
        await generate(context, Provider(), validate=None, limits=Limits(max_calls=2, max_queries=0), resume=state)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["task", "feedback", "facts", "sources", "declaration"])
async def test_resume_refuses_changed_inputs_before_any_model_call(context, change):
    from dataclasses import replace

    from experimental.curator.generation.run import GenerationPausedError

    provider = Provider(response("read_source", {"name": "loop"}))
    with pytest.raises(GenerationPausedError) as paused:
        await generate(context, provider, validate=None, limits=Limits(max_calls=1))
    value = {
        "task": "Another task",
        "feedback": "New feedback",
        "facts": {"changed": True},
        "sources": {"new": {"start": 1, "end": 2}},
        "declaration": context.declaration.restrict(["action.config"]),
    }[change]
    resumed = Provider()
    with pytest.raises(ValueError, match="inputs changed"):
        await generate(replace(context, **{change: value}), resumed, validate=None, resume=paused.value.state)
    assert not resumed.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", ["revise_plan", "revise_selection"])
async def test_pause_after_backtracking_resumes_design_without_a_stale_plan(context, revision):
    from experimental.curator.generation.run import GenerationPausedError

    target = "memory.prompt" if revision == "revise_selection" else "action.config"
    arguments = selection(target) if revision == "revise_selection" else {"reason": "Reconsider the mechanism"}
    provider = Provider(
        response("submit_selection", selection("action.config")),
        response("submit_plan", plan("action.config")),
        response(revision, arguments),
    )
    staged = []
    with pytest.raises(GenerationPausedError) as paused:
        await generate(context, provider, validate=None, stage_candidate=staged.append, limits=Limits(max_calls=3))
    state = paused.value.state
    assert state.stage == "design" and state.plan is None and state.candidate is None
    assert state.selection.targets == (target,) and staged == [None]
    values = (
        {"memory.prompt": {"TOOLS.md": "New guidance"}}
        if target == "memory.prompt"
        else {"action.config": {"temperature": 0.2}}
    )
    resumed = Provider(response("submit_plan", plan(target)), response("submit_artifact", {"values": values}))

    async def validate(candidate):
        return Validation([], [])

    result = await generate(context, resumed, validate=validate, limits=Limits(max_calls=5), resume=state)
    assert result.candidate.artifact.values == values


@pytest.mark.asyncio
async def test_resume_does_not_reset_the_artifact_repair_budget(context):
    from experimental.curator.generation.run import GenerationPausedError

    draft = {"values": {"action.config": {"temperature": 0.2}}}
    provider = Provider(
        response("submit_selection", selection("action.config")),
        response("submit_plan", plan("action.config")),
        response("submit_artifact", draft),
    )

    async def validate(candidate):
        return Validation(["Still fails the behavior check"], [])

    with pytest.raises(GenerationPausedError) as paused:
        await generate(context, provider, validate=validate, limits=Limits(max_calls=3, max_repairs=1))
    with pytest.raises(GenerationError, match="repair budget exhausted"):
        await generate(
            context,
            Provider(response("submit_artifact", draft)),
            validate=validate,
            limits=Limits(max_calls=4, max_repairs=1),
            resume=paused.value.state,
        )


@pytest.mark.asyncio
async def test_staged_files_join_the_artifact_and_the_artifacts_own_files_win(context):
    provider = Provider(
        response("submit_selection", selection("action.strategy")),
        response("submit_plan", plan("action.strategy")),
        response("stage_file", {"path": "rules.py", "content": "def create(state, task):\n    return None\n"}),
        response("stage_file", {"path": "notes.md", "content": "draft"}),
        response("stage_file", {"path": "../escape.py", "content": "x"}),
        response(
            "submit_artifact",
            {"values": {"action.strategy": {"factory": "rules:create"}}, "files": {"notes.md": "final"}},
        ),
    )
    checked = []

    async def validate(candidate):
        checked.append(candidate)
        return Validation([], [{"kind": "runtime.bound"}])

    result = await generate(context, provider, validate=validate)
    assert result.candidate.artifact.files == {
        "rules.py": "def create(state, task):\n    return None\n",
        "notes.md": "final",
    }
    staged = [event for event in result.trace if event["event"] == "file.staged"]
    assert [event["result"].get("staged") for event in staged] == ["rules.py", "notes.md", None]
    assert "error" in staged[2]["result"]
    assert "`stage_file`" in instructions(provider.requests[2]) and "`stage_file`" not in instructions(
        provider.requests[1]
    )
    assert provider.requests[2]["tools"] == provider.requests[1]["tools"]


@pytest.mark.asyncio
async def test_a_value_naming_a_module_no_file_provides_is_sent_back_before_validation(context):
    provider = Provider(
        response("submit_selection", selection("action.strategy")),
        response("submit_plan", plan("action.strategy")),
        response("submit_artifact", {"values": {"action.strategy": {"factory": "planning_impl:create"}}}),
        response("stage_file", {"path": "planning_impl.py", "content": "def create(state, task):\n    return None\n"}),
        response("submit_artifact", {"values": {"action.strategy": {"factory": "planning_impl:create"}}}),
    )
    checked = []

    async def validate(candidate):
        checked.append(candidate)
        return Validation([], [{"kind": "runtime.bound"}])

    result = await generate(context, provider, validate=validate)
    assert len(checked) == 1 and "planning_impl.py" in result.candidate.artifact.files
    rejected = next(event for event in result.trace if event["event"] == "output.rejected")
    assert "'planning_impl'" in rejected["error"] and "stage_file" in rejected["error"]
    assert result.trace[-1]["event"] != "validation" or not result.trace[-1]["errors"]


def test_observations_reach_a_request_as_an_index_and_are_read_on_demand():
    import json

    from experimental.curator.generation.context import query, render
    from experimental.curator.generation.context.collect import Context

    huge = {"kind": "child.execution", "turn_id": "t1", "records": [{"kind": "tool", "text": "x" * 1000}] * 100}
    rows = [{"kind": "task.execution", "turn_id": "t1"}, huge]
    index = render.observation_index(rows)
    assert index[0] == {
        "index": 0,
        "bytes": len(json.dumps(rows[0]).encode()),
        "kind": "task.execution",
        "turn_id": "t1",
    }
    assert index[1]["records_kinds"] == {"tool": 100} and index[1]["bytes"] > 100_000
    assert len(json.dumps(index).encode()) < 400
    context = Context.__new__(Context)
    object.__setattr__(context, "observations", tuple(rows))
    first = query.execute(context, "read_observation", {"index": 1, "path": ["records", 0], "length": 30})
    assert first["text"].startswith("{") and first["next_offset"] == 30 and first["total_characters"] > 1000
    with pytest.raises(ValueError, match="there are 2"):
        query.execute(context, "read_observation", {"index": 2})
    many = [{"kind": "row", "n": number, "note": "z" * 200} for number in range(20_000)]
    assert render.observation_index(many) == {
        "rows": 20_000,
        "kinds": {"row": 20_000},
        "read": "read_observation by index",
    }
    assert render.observations(rows, row_bytes=2000)[1]["cut"] > 100_000


@pytest.mark.asyncio
async def test_a_call_that_spent_its_output_thinking_is_followed_by_a_short_thinking_call(context):
    provider = Provider(
        LLMResponse(content="Still thinking.", finish_reason="length"),
        # A provider can spend the whole output on reasoning and still report `stop` with no content.
        LLMResponse(content=None, finish_reason="stop"),
        LLMResponse(content=None, finish_reason="stop"),
        response("submit_selection", selection()),
        response("submit_plan", plan()),
        response("submit_artifact", {"values": {}}),
    )

    async def validate(candidate):
        return Validation([], [])

    try:
        await generate(context, provider, validate=validate, limits=Limits(max_calls=6, max_output=65536))
    except GenerationError:
        pass
    assert len(provider.requests) >= 2 and all(request["max_tokens"] == 65536 for request in provider.requests)
    efforts = [request.get("reasoning_effort") for request in provider.requests[:4]]
    assert efforts == [None, "low", "none", "none"]
    assert "ran out of output" in provider.requests[1]["messages"][-2]["content"]
    with pytest.raises(ValueError):
        Limits(max_output=0)


@pytest.mark.asyncio
async def test_a_curator_call_on_a_model_that_needs_breakpoints_marks_the_tail_so_the_next_call_reads_it(context):
    """Without breakpoints each call is written to the cache again and never read; with marks only on the tail, each
    new stage writes the curation's materials again."""
    provider = Provider(
        response("submit_selection", selection()),
        response("submit_plan", plan()),
        response("submit_artifact", {"values": {}}),
    )

    async def validate(candidate):
        return Validation([], [])

    try:
        await generate(
            context,
            provider,
            validate=validate,
            model="openrouter/anthropic/claude-opus-5.5",
            limits=Limits(max_calls=3),
        )
    except GenerationError:
        pass
    for request in provider.requests[:2]:
        messages = request["messages"]
        marked = [index for index, message in enumerate(messages) if "cache_control" in json.dumps(message)]
        assert marked == [0, 1, len(messages) - 2] and messages[0].get("cache_marks_placed")
        assert "cache_control" in json.dumps(request["tools"][-1])
    plain = Provider(response("submit_selection", selection()))
    try:
        await generate(context, plain, validate=validate, model="deepseek/deepseek-flash", limits=Limits(max_calls=1))
    except GenerationError:
        pass
    assert "cache_control" not in json.dumps(plain.requests[0]["messages"])
