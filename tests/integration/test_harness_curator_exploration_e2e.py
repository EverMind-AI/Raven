"""Native exploration and draft preflight feed a generated revision of a real worker."""

import json
from pathlib import Path

import pytest

from experimental.curator.harness import Task
from experimental.curator.raven_adapter import exploration
from experimental.curator.raven_adapter.worker import Worker
from experimental.curator.workflow import improve
from tests.integration.test_harness_curator_e2e import baseline as baseline
from tests.integration.test_harness_curator_e2e import plan_for, replay_provider
from tests.integration.test_harness_curator_planning_e2e import CuratorProvider, response
from tests.test_harness_curator_exploration import Executor
from tests.test_harness_curator_generation import selection


@pytest.mark.integration
@pytest.mark.asyncio
async def test_native_exploration_preflight_repair_and_worker_installation(baseline, tmp_path, monkeypatch):
    baseline.task = Task(id="exploration-task", text="Improve the worker's sampling configuration.")
    baseline.config.permissions.tools["exec"] = "allow"
    monkeypatch.setattr(exploration, "build_executor", lambda *args, **kwargs: Executor())
    draft = {"values": {"action.config": {"temperature": 0.2}}, "files": {"policy.py": "def broken(\n"}}
    fixed = {"values": draft["values"], "files": {"policy.py": "POLICY = 'checked'\n"}}

    class Curator(CuratorProvider):
        def __init__(self):
            super().__init__([])
            self.workspace = None

        async def chat(self, messages, **kwargs):
            step = len(self.requests)
            packet = json.loads(messages[1]["content"])
            if self.workspace is None:
                self.workspace = Path(packet["exploration"]["workspace"])
            actions = [
                response("grep", {"pattern": "class ToolRegistry", "path": "source/raven/agent/tools"}),
                response("read_file", {"path": "source/raven/agent/tools/registry.py", "offset": 266, "limit": 25}),
                response("exec", {"command": "printf EXPLORATION_EXECUTED"}),
                response("submit_selection", selection("action.config")),
                response("submit_plan", plan_for("action.config").model_dump(mode="json")),
                response("check_candidate", draft),
                None,
                response("submit_artifact", fixed),
            ]
            if step == 6:
                checked = json.loads(next(row for row in reversed(messages) if row["role"] == "tool")["content"])
                assert not checked["passed"] and checked["errors"]
                assert "policy.py" in str(checked["errors"])
                actions[step] = response(
                    "read_file", {"path": str(Path(checked["candidate"]["package"]) / "policy.py")}
                )
            self.responses.append(actions[step])
            return await super().chat(messages, **kwargs)

    curator = Curator()
    async with Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30) as worker:
        result = await improve(worker, curator)
        assert worker.artifact.values["action.config"]["temperature"] == 0.2
        assert any(
            row["event"] == "query" and row["tool"] == "exec" and "EXPLORATION_EXECUTED" in row["result"]["text"]
            for row in result.trace
        )
        assert any(row["event"] == "preflight" and not row["result"]["passed"] for row in result.trace)
        assert not curator.workspace.exists()
        execution = await worker.run("Answer briefly.")
        assert not execution.errors
        request = next(row for row in execution.records if row["kind"] == "provider.request")
        assert request["defaults"]["temperature"] == 0.2
        records = [json.loads(path.read_text()) for path in (worker.root / "curation").glob("*.json")]
        assert records[0]["exploration"]["source_paths"]
        assert any(row["event"] == "preflight" for row in records[0]["generated"]["trace"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_worker_budget_pause_resumes_and_installs_without_reselection(baseline, tmp_path):
    from experimental.curator.generation.run import GenerationPausedError, Limits

    baseline.task = Task(id="resume-task", text="Adjust sampling for this task.")
    first = CuratorProvider(
        [
            response("submit_selection", selection("action.config")),
            response("submit_plan", plan_for("action.config").model_dump(mode="json")),
        ]
    )
    async with Worker(baseline, tmp_path / "resumed-worker", provider_factory=replay_provider) as worker:
        with pytest.raises(GenerationPausedError) as paused:
            await improve(worker, first, limits=Limits(max_calls=2))
        assert paused.value.state.stage == "implement"
        assert not worker.artifact.values
        pending_path = worker.root / "curation/pending.json"
        workspace = Path(json.loads(pending_path.read_text())["workspace"])
        before = (await worker.inspect()).declaration.baseline
        await worker.close()
        await worker.start()
        assert (await worker.inspect()).declaration.baseline == before
        resumed = CuratorProvider([response("submit_artifact", {"values": {"action.config": {"temperature": 0.2}}})])
        result = await improve(worker, resumed, limits=Limits(max_calls=3))
        assert result.validation.passed
        assert worker.artifact.values["action.config"]["temperature"] == 0.2
        assert len(resumed.requests) == 1
        assert not pending_path.exists() and not workspace.exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retiring_generated_content_checks_on_copy_and_restores_original(baseline, tmp_path):
    baseline.task = Task(id="retire-task", text="Restore managed guidance")
    path = baseline.config.workspace_path / "TOOLS.md"
    path.write_text("original guidance")
    async with Worker(baseline, tmp_path / "retire-worker", provider_factory=replay_provider) as worker:
        before = await worker.inspect()
        candidate = before.declaration.accept(
            plan_for("memory.prompt"), {"values": {"memory.prompt": {"TOOLS.md": "generated guidance"}}}
        )
        await worker.install(candidate)
        current = await worker.inspect()
        removal = current.declaration.accept(plan_for("memory.prompt"), {"values": {}, "remove": ["memory.prompt"]})
        checked = await worker.check(removal)
        assert checked.passed, checked.errors
        assert path.read_text() == "generated guidance"
        await worker.install(removal)
        assert path.read_text() == "original guidance"
        assert not worker.artifact.values


@pytest.mark.integration
@pytest.mark.asyncio
async def test_installation_restores_old_harness_when_effective_inspection_fails(baseline, tmp_path, monkeypatch):
    baseline.task = Task(id="view-failure", text="Keep a valid effective harness")
    path = baseline.config.workspace_path / "TOOLS.md"
    path.write_text("original")
    async with Worker(baseline, tmp_path / "view-worker", provider_factory=replay_provider) as worker:
        before = await worker.inspect()
        candidate = before.declaration.accept(
            plan_for("memory.prompt"), {"values": {"memory.prompt": {"TOOLS.md": "candidate"}}}
        )
        original_inspect = worker._inspect
        calls = 0

        async def inspect():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("effective view failed validation")
            return await original_inspect()

        monkeypatch.setattr(worker, "_inspect", inspect)
        with pytest.raises(ValueError, match="effective view failed"):
            await worker.install(candidate)
        assert path.read_text() == "original" and not worker.artifact.values
        assert not any(m.name == "authored.memory.prompt" for m in (await worker.inspect()).mechanisms)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_baseline_mode_reaches_native_turn_and_provider(baseline, tmp_path):
    from raven.config.schema import AcpModeConfig

    baseline.task = Task(id="mode-task", text="Answer briefly")
    baseline.config.acp.modes = {
        "review": AcpModeConfig(
            name="Review", max_tool_iterations=2, reasoning_effort="low", overlay={"marker": "effective"}
        )
    }
    baseline.mode = "review"
    async with Worker(baseline, tmp_path / "mode-worker", provider_factory=replay_provider) as worker:
        inspection = await worker.inspect()
        assert inspection.facts["turn_profile"]["reasoning_effort"] == "low"
        execution = await worker.run("Answer briefly")
        assert not execution.errors
        request = next(row for row in execution.records if row["kind"] == "provider.request")
        assert request["parameters"]["reasoning_effort"] == "low"
        control = next(row for row in execution.records if row["kind"] == "loop.control")
        assert control["mode"] == "review" and control["mode_overlay"] == {"marker": "effective"}
