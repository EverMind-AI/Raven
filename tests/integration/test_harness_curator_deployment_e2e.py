"""One activation boundary changes real delegated code and restores the entire previous revision."""

from functools import partial

import pytest

from experimental.curator.harness import Artifact, Task
from experimental.curator.raven_adapter.baselines import Baseline
from experimental.curator.raven_adapter.deployment import Child
from experimental.curator.raven_adapter.worker import Worker, WorkerError
from raven.contracts.llm_provider import LLMResponse
from raven.playbook import NodeSpec, PlaybookSpec, PlaybookStore
from tests.integration.test_harness_curator_e2e import baseline as baseline
from tests.integration.test_harness_curator_e2e import plan_for, replay_provider
from tests.integration.test_harness_curator_planning_e2e import response

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow(reason="Starts and replaces actual root and ACP child processes."),
]


async def recorded_probe_failure(bound):
    bound.recorder.add("action.error", error="Probe observed a swallowed callback failure")


def authored(reply):
    return Artifact(
        values={"memory.intake": "behavior:Behavior"},
        files={
            "behavior.py": f"""
from raven.contracts.participant import AgentParticipant, Intake
class Behavior(AgentParticipant):
    async def intake(self, text, step):
        return Intake(text, reply={reply!r})
"""
        },
    )


@pytest.mark.asyncio
async def test_child_change_uses_native_playbook_and_failed_activation_restores_previous_code(baseline, tmp_path):
    baseline.task = Task(id="task", text="Delegate work")
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
            name="delivery",
            description="Delegate work",
            task_summary="Run the child",
            mode="dag",
            confirm=False,
            triggers={"keywords": ["delivery"]},
            nodes=[
                NodeSpec(
                    id="work", subagent="Hosted", node_summary="Work", prompt_template="Complete the assigned work."
                )
            ],
        )
    )
    sibling = Baseline.restore(child.export())
    sibling.config.agents.defaults.workspace = str(tmp_path / "sibling-home")
    sibling.config.workspace_path.mkdir()
    replies = [
        response("load_playbook", {"name": "delivery"}),
        LLMResponse(content="Delegated."),
        LLMResponse(content="Done."),
    ]
    async with Worker(
        baseline,
        tmp_path / "worker",
        provider_factory=partial(replay_provider, responses=replies),
        children={
            "Hosted": Child(child, authored("CHILD_ORIGINAL")),
            "Sibling": Child(sibling, authored("SIBLING_UNCHANGED")),
        },
        timeout=60,
    ) as worker:
        first = await worker.run("Run delivery.")
        assert not first.errors
        assert "CHILD_ORIGINAL" in str(first.records)
        original = worker.revision_id
        root_view = await worker.inspect()
        unchanged = root_view.declaration.accept(plan_for(), {"values": {}})
        observed_failure = await worker.check_composition(unchanged, {}, probe=recorded_probe_failure)
        assert not observed_failure.passed and "swallowed callback" in str(observed_failure.errors)
        assert worker.revision_id == original
        child_view = await worker.inspect_agent("Hosted")
        root_candidate = root_view.declaration.accept(
            plan_for("capability.select_tools"),
            {
                "values": {"capability.select_tools": "selection:Selection"},
                "files": {
                    "selection.py": "from raven.contracts.participant import AgentParticipant\n"
                    "class Selection(AgentParticipant):\n"
                    "    async def select_tools(self, offered, step):\n"
                    "        return []\n"
                },
            },
        )
        child_candidate = child_view.declaration.accept(plan_for("memory.intake"), authored("CHILD_REVISED"))
        await worker.install(root_candidate, children={"Hosted": child_candidate})
        assert worker.revision_id != original
        assert worker.children["Sibling"].artifact == authored("SIBLING_UNCHANGED")
        sibling_view = await worker.inspect_agent("Sibling")
        assert sibling_view.facts["authored"]["values"] == authored("SIBLING_UNCHANGED").values
        assert sibling_view.facts["agent_home"] != (await worker.inspect_agent("Hosted")).facts["agent_home"]
        second = await worker.run("Run delivery.")
        assert not second.errors
        assert "CHILD_REVISED" in str(second.records)
        delegated = [row for row in second.records if row["kind"] == "child.execution"]
        assert len(delegated) == 1 and delegated[0]["harness"] == "Hosted"
        nodes = {
            node["id"]
            for row in second.records
            if row["kind"] == "dag.progress" and row["name"] == "dag_run_started"
            for node in row["payload"]["nodes"]
        }
        assert nodes and {row["nodeId"] for row in delegated[0]["instances"]} == nodes
        assert any(row.get("conversation") for row in delegated[0]["records"])
        assert delegated[0]["revision"] == (await worker.agent_state("Hosted"))["revision"]
        requests = [row for row in second.records if row["kind"] == "provider.request"]
        assert any("load_playbook" in str(row) for row in requests)
        accepted = worker.revision_id
        root_view, child_view = await worker.inspect(), await worker.inspect_agent("Hosted")
        bad = child_view.declaration.accept(
            plan_for("memory.intake"),
            Artifact(
                values={"memory.intake": "broken:create"},
                files={"broken.py": "def create():\n    raise RuntimeError('activation refused')\n"},
            ),
        )
        with pytest.raises(WorkerError):
            await worker.install(root_view.declaration.accept(plan_for(), {"values": {}}), children={"Hosted": bad})
        assert worker.revision_id == accepted
        third = await worker.run("Run delivery.")
        assert not third.errors
        assert "CHILD_REVISED" in str(third.records)

        root_view = await worker.inspect()
        unchanged_root = root_view.declaration.accept(plan_for(), {"values": {}})
        checked = await worker.check_composition(unchanged_root, {"Hosted": None})
        assert checked.passed, checked.errors
        await worker.install(unchanged_root, children={"Hosted": None})
        restored = await worker.run("Run delivery.")
        assert not restored.errors and "CHILD_ORIGINAL" in str(restored.records)
        assert worker.children["Hosted"].artifact == authored("CHILD_ORIGINAL")
