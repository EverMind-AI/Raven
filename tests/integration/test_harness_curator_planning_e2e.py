"""Semantic planning generation, native execution and feedback revision share one task."""

import json
from copy import deepcopy
from functools import partial
from pathlib import Path

import pytest

from experimental.curator.harness import Task
from experimental.curator.raven_adapter.planning.contracts import TARGET, TOOL_NAME
from experimental.curator.raven_adapter.worker import Worker, WorkerError
from experimental.curator.workflow import improve
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from tests.integration.test_harness_curator_e2e import ReplayProvider, plan_for, replay_provider
from tests.integration.test_harness_curator_e2e import baseline as baseline
from tests.test_harness_curator_generation import packet, selection

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/harness_curator"


def artifact():
    return {
        "values": {
            TARGET: {
                "factory": "task_planning:create",
                "tool": "planning_bindings:command",
                "context": "planning_bindings:context",
                "observe": "planning_bindings:observe",
            },
            "capability.tools": [{"name": "planning_probe", "factory": "planning_bindings:probe"}],
            "planning.skills": {
                "planning/SKILL.md": "---\nname: planning\ndescription: Use the task plan\nalways: true\n---\nPLANNING_SKILL_RUNTIME"
            },
        },
        "files": {name: (FIXTURES / name).read_text() for name in ("task_planning.py", "planning_bindings.py")},
    }


def response(name, arguments):
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(name, name, arguments)])


class CuratorProvider(ReplayProvider):
    def __init__(self, responses):
        super().__init__(responses)
        self.requests = []

    async def chat(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        return await super().chat(messages, **kwargs)


def bind_task(baseline):
    baseline.task = Task(id="planning-task", text="Verify")
    baseline.config.permissions.tools[TOOL_NAME] = "allow"
    baseline.config.permissions.tools["planning_probe"] = "allow"
    return baseline


@pytest.mark.integration
@pytest.mark.asyncio
async def test_curator_repairs_runs_and_revises_a_real_planning_strategy(baseline, tmp_path):
    bind_task(baseline)
    actions = [
        response(TOOL_NAME, {"request": {"operation": "complete", "item": "Verify"}}),
        response("planning_probe", {}),
        LLMResponse(content="Verification has failed."),
        response(TOOL_NAME, {"request": {"operation": "view"}}),
        LLMResponse(content="Continue with the retained plan."),
    ]
    worker = Worker(
        baseline, tmp_path / "runtime", provider_factory=partial(replay_provider, responses=actions), timeout=30
    )
    proposed = artifact()
    broken = deepcopy(proposed)
    broken["files"]["task_planning.py"] = "def create(state):\n    return object()\n"
    curator = CuratorProvider(
        [
            response("read_source", {"name": TARGET, "find": "initialize"}),
            response("submit_selection", selection(*proposed["values"])),
            response("submit_plan", plan_for(*proposed["values"]).model_dump(mode="json")),
            response("submit_artifact", broken),
            response("submit_artifact", proposed),
        ]
    )
    async with worker:
        await improve(worker, curator)
        assert any("initialize" in str(row) and "history" in str(row) for row in curator.requests[2:])
        materials = packet({"messages": curator.requests[2]})
        contract = next(row for row in materials["selected_contracts"] if row["target"] == TARGET)
        assert "class PlanningStrategy" in str(contract["knowledge"])
        initial_packet = json.loads(curator.requests[0][1]["content"])
        assert initial_packet["orientation"]["source"] == "reference.index"
        assert "reference.planning" in initial_packet["sources"]
        assert initial_packet["orientation"]["text"]
        assert "Factory and session state" in str(contract["knowledge"])
        assert "Completed iteration evidence" in str(contract["knowledge"])
        assert any("explicitly inheriting PlanningStrategy" in str(request) for request in curator.requests)
        first = await worker.run("Verify using the task plan.")
        assert not first.errors
        assert any(row["kind"] == "planning.result" and row["source"] == "tool" for row in first.records)
        assert any(row["kind"] == "planning.result" and row["source"] == "observation" for row in first.records)
        requests = [row for row in first.records if row["kind"] == "provider.request"]
        assert all("CURRENT_TASK_PLAN" in str(row) for row in requests)
        assert any("PLANNING_SKILL_RUNTIME" in str(row) for row in requests)
        inspected = await worker.inspect()
        assert inspected.facts["planning"]["view"]["items"] == {"Verify": False}
        assert inspected.facts["planning"]["sessions"]["curator:task"]["seen"] == ["planning_probe"]
        before = (worker.root / "planning.json").read_bytes()
        pid = worker._process.pid
        continuation = await worker.run("Continue without changing the Harness.")
        assert not continuation.errors and worker._process.pid == pid
        assert (worker.root / "planning.json").read_bytes() == before
        assert len(curator.requests) == 5
        revised = deepcopy(proposed)
        revised["values"] = {TARGET: revised["values"][TARGET]}
        revised["files"]["task_planning.py"] = revised["files"]["task_planning.py"].replace(
            "GUARD_COMPLETION = False", "GUARD_COMPLETION = True"
        )
        feedback = CuratorProvider(
            [
                response("submit_selection", selection(TARGET)),
                response("submit_plan", plan_for(TARGET).model_dump(mode="json")),
                response("submit_artifact", revised),
            ]
        )
        await improve(
            worker,
            feedback,
            feedback={"source": "human", "text": "Require execution evidence before accepting completion."},
        )
        revision = json.loads(feedback.requests[1][1]["content"])
        assert "class Planning" in revision["current_authored"]["files"]["task_planning.py"]
        assert revision["worker"]["planning"]["sessions"]["curator:task"]["seen"] == ["planning_probe"]
        assert revision["previous_expectations"]
        assert any(row.get("artifact_id") == continuation.artifact_id for row in revision["observations"])
        logs = [json.loads(path.read_text()) for path in (worker.root / "curation").glob("*.json")]
        assert len(logs) == 2
        assert any(row["feedback"] and row["turn_id"] == continuation.turn_id for row in logs)
        assert all(row["task_id"] == baseline.task.id for row in logs)
        after = await worker.inspect()
        assert after.facts["planning"]["view"]["guarded"]
        assert after.facts["planning"]["view"]["items"] == {"Verify": False}
        assert (worker.root / "planning.json").read_bytes() == before
        second = await worker.run("Try the revised behavior.")
        assert any("verified execution evidence" in str(row) for row in second.records)
        assert (await worker.inspect()).facts["planning"]["view"]["items"] == {"Verify": False}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_validation_isolates_state_and_failed_activation_restores_the_checkpoint(baseline, tmp_path):
    bind_task(baseline)
    proposed = artifact()
    async with Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30) as worker:
        inspection = await worker.inspect()
        await worker.install(inspection.declaration.accept(plan_for(*proposed["values"]), proposed))
        before = (worker.root / "planning.json").read_bytes()
        original = worker.artifact
        bad = deepcopy(proposed)
        bad["files"]["task_planning.py"] = bad["files"]["task_planning.py"].replace(
            "return Planning(state)", 'state["migration"] = "candidate"\n    return Planning(state)'
        )
        bad["files"]["failure.py"] = """class Service:
    name = "planning_failure"
    async def start(self, handles):
        raise RuntimeError("deliberate activation failure")
    async def stop(self):
        pass

def create(context):
    return Service()
"""
        bad["values"]["action.services"] = [{"name": "planning_failure", "factory": "failure:create"}]
        inspection = await worker.inspect()
        candidate = inspection.declaration.accept(plan_for(*bad["values"]), bad)
        report = await worker.check(candidate)
        assert report.passed, report.errors
        assert (worker.root / "planning.json").read_bytes() == before
        with pytest.raises(WorkerError, match="service did not start"):
            await worker.install(candidate)
        assert worker.artifact == original
        assert (worker.root / "planning.json").read_bytes() == before
        assert "migration" not in (await worker.inspect()).facts["planning"]["state"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_candidate_based_on_old_planning_state_cannot_overwrite_new_progress(baseline, tmp_path):
    bind_task(baseline)
    actions = [
        response(TOOL_NAME, {"request": {"operation": "complete", "item": "Verify"}}),
        LLMResponse(content="recorded"),
    ]
    async with Worker(
        baseline, tmp_path / "runtime", provider_factory=partial(replay_provider, responses=actions), timeout=30
    ) as worker:
        proposed = artifact()
        inspection = await worker.inspect()
        await worker.install(inspection.declaration.accept(plan_for(*proposed["values"]), proposed))
        inspection = await worker.inspect()
        candidate = inspection.declaration.accept(plan_for(TARGET), {"values": {TARGET: proposed["values"][TARGET]}})
        await worker.run("Update the plan")
        with pytest.raises(ValueError, match="baseline"):
            await worker.install(candidate)
        assert (await worker.inspect()).facts["planning"]["view"]["items"] == {"Verify": True}
