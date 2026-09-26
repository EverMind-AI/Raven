"""Two Curator scopes produce full strategy artifacts and activate them together through native delegation."""

import json
from functools import partial

import pytest

from experimental.curator.generation.run import GenerationPausedError, Limits
from experimental.curator.harness import Task
from experimental.curator.raven_adapter.baselines import Baseline
from experimental.curator.raven_adapter.worker import Worker
from experimental.curator.workflow import improve
from raven.contracts.llm_provider import LLMResponse
from raven.playbook import NodeSpec, PlaybookSpec, PlaybookStore
from tests.integration.test_harness_curator_deployment_e2e import authored
from tests.integration.test_harness_curator_e2e import baseline as baseline
from tests.integration.test_harness_curator_e2e import plan_for, replay_provider
from tests.integration.test_harness_curator_planning_e2e import artifact as planning_artifact
from tests.test_harness_curator_generation import Provider, packet, response, selection
from tests.test_harness_curator_strategies import files, values

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow(reason="Curates root and full strategy child with isolated checks and actual ACP activation."),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restart, report_gap, transport_failure",
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
)
async def test_root_and_child_generate_with_shared_contracts_and_resume_without_repeating_root(
    baseline, tmp_path, restart, report_gap, transport_failure
):
    baseline.task = Task(id="task", text="Train this worker")
    baseline.config.playbooks.enabled = True
    baseline.extensions.plugins.disabled = [name for name in baseline.extensions.plugins.disabled if name != "playbook"]
    baseline.config.permissions.tools["load_playbook"] = "allow"
    child = Baseline.restore(baseline.export())
    child.config.agents.defaults.workspace = str(tmp_path / "child-home")
    child.config.workspace_path.mkdir()
    child.config.providers.openai.api_key = "offline-test"
    child.config.providers.openai.api_base = "http://127.0.0.1:9/v1"
    child.extensions.session_title.enabled = False
    child.config.playbooks.enabled = False
    store = PlaybookStore(baseline.config.workspace_path / "playbooks", builtin_root=tmp_path / "empty")
    store.save(
        PlaybookSpec(
            name="work",
            description="Complete assigned work",
            task_summary="Work",
            mode="dag",
            confirm=False,
            triggers={"keywords": ["work"]},
            nodes=[
                NodeSpec(
                    id="step", subagent="Hosted", node_summary="Work", prompt_template="Complete the assigned work."
                )
            ],
        )
    )
    requirement = baseline.config.workspace_path / "playbooks/work/nodes/step/requirements.json"
    requirement.parent.mkdir(parents=True)
    requirement_text = json.dumps(
        [
            {
                "behavior": "Follow the supplied work procedure",
                "observed": "New working requirement",
                "evidence": ["Mentor material"],
                "expectation": "new",
                "acceptance": "The assigned work follows the procedure",
                "strength": "must_hold",
            }
        ]
    )
    planning = planning_artifact()
    answer = authored("CURATED_CHILD")
    artifact = {
        "values": {**values(), **planning["values"], **answer.values},
        "files": {**files(), **planning["files"], **answer.files},
    }
    root_plan = plan_for("planning.playbooks").model_dump(mode="json")
    root_plan["node_reasons"] = {
        "work/step": "Keep this responsibility in the existing child; let it realize the requirement"
    }
    turns = [
        response("submit_selection", selection("planning.playbooks")),
        response("submit_plan", root_plan),
        response(
            "submit_artifact",
            {"values": {"planning.playbooks": {"work/nodes/step/requirements.json": requirement_text}}},
        ),
        response("submit_selection", selection(*artifact["values"])),
        response("submit_plan", plan_for(*artifact["values"]).model_dump(mode="json")),
        response("submit_artifact", artifact),
    ]
    replies = [
        response("load_playbook", {"name": "work"}),
        LLMResponse(content="Delegated."),
        LLMResponse(content="Done."),
    ]
    async with Worker(
        baseline,
        tmp_path / "worker",
        provider_factory=partial(replay_provider, responses=replies),
        children={"Hosted": child},
        timeout=60,
    ) as worker:
        original = worker.revision_id
        with pytest.raises(GenerationPausedError) as stopped:
            await improve(worker, Provider(*turns[:4]), limits=Limits(max_calls=4))
        assert stopped.value.state.calls == 4
        assert worker.revision_id == original
        pending = json.loads((worker.root / "curation/composition.json").read_text())
        assert pending["root"] is not None
        if restart:
            await worker.close()
            await worker.start()
        if transport_failure:
            from experimental.curator.generation.run import GenerationInterruptedError

            with pytest.raises(GenerationInterruptedError) as interrupted:
                await improve(
                    worker,
                    Provider(LLMResponse(content="Provider unavailable", finish_reason="error")),
                    limits=Limits(max_calls=7),
                )
            assert interrupted.value.state.calls == 5
            assert interrupted.value.state.stage == "design"
            held = json.loads((worker.root / "curation/composition.json").read_text())
            assert held["error"] is None and held["repair"] is None
            assert held["root"] == pending["root"]
        remaining = turns[4:]
        if report_gap:
            clarified = json.loads(requirement_text)
            clarified[0]["acceptance"] += "; report missing input instead of inventing it"
            remaining = [
                response(
                    "report_gap", {"reason": "The assigned requirement needs a missing-input decision from the root"}
                ),
                response(
                    "submit_artifact",
                    {
                        "values": {
                            "planning.playbooks": {"work/nodes/step/requirements.json": json.dumps(clarified)},
                        }
                    },
                ),
                *turns[3:],
            ]
        continuation = Provider(*remaining)
        result = await improve(
            worker, continuation, limits=Limits(max_calls=4 + len(remaining) + int(transport_failure))
        )
        assert result.validation.passed and len(continuation.requests) == len(remaining)
        assert {f"{role}.strategy" for role in ("planning", "memory", "capability", "action")} <= worker.children[
            "Hosted"
        ].artifact.values.keys()
        materials = packet(continuation.requests[0])
        assert materials["worker"]["scope"]["kind"] == "child"
        assert "memory.strategy" in str(materials["selected_contracts"])
        assert "parent_materials" in materials["feedback"]
        if report_gap:
            repaired = packet(continuation.requests[1])
            failure = repaired["validation_observations"][0]
            assert failure["scope"] == "child/Hosted"
            assert failure["nodes"] == ["work/step"]
            assert failure["evidence"][-1]["event"] == "report_gap"
        assert worker.revision_id != original
        run = await worker.run("Run work.")
        assert not run.errors
        assert "CURATED_CHILD" in str(run.records)
        records = [json.loads(path.read_text()) for path in (worker.root / "curation").glob("*.json")]
        complete = next(row for row in records if row.get("revision"))
        assert complete["changed"] is True
        if report_gap:
            assert len(complete["composition"]["scopes"]) == 1
            assert any(event["event"] == "validation" for event in complete["generated"]["trace"])
        assert complete["child_changes"]["Hosted"]["candidate"]["artifact"]["files"]
        assert not (worker.root / "curation/composition.json").exists()
        withdrawal = plan_for("planning.playbooks").model_dump(mode="json")
        withdrawal["node_reasons"] = {"work/step": "Keep the node but withdraw its authored Harness changes"}
        withdrawn = await improve(
            worker,
            Provider(
                response("submit_selection", selection("planning.playbooks")),
                response("submit_plan", withdrawal),
                response(
                    "submit_artifact",
                    {
                        "values": {"planning.playbooks": {}},
                        "remove_paths": {"planning.playbooks": ["work/nodes/step/requirements.json"]},
                    },
                ),
            ),
            limits=Limits(max_calls=3),
        )
        assert withdrawn.validation.passed
        assert not requirement.exists()
        assert not worker.children["Hosted"].artifact.values
        assert not (await worker.inspect_agent("Hosted")).facts["authored"]["values"]
