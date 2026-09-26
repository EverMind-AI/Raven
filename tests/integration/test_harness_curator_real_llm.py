"""Opt-in generated-strategy smoke checks using configured Curator and worker models."""

import json
import os
from pathlib import Path

import pytest

from experimental.curator.harness import Task
from experimental.curator.raven_adapter.inspection import Baseline
from experimental.curator.raven_adapter.worker import Worker
from experimental.curator.workflow import improve
from raven.config.raven import load_raven_config
from raven.core.config_stack import load_runtime_config
from raven.core.plugin_stack import discover_plugins
from raven.providers.factory import make_lazy_provider

pytestmark = [pytest.mark.integration, pytest.mark.real_llm, pytest.mark.slow]

SCENARIOS = {
    "memory": (
        "Implement memory.strategy with recall/retain and the query, context and retain bindings. "
        "Remember the task preference cobalt in the task checkpoint. Retrieve it before model calls; "
        "render the exact label MEMORY:cobalt as current task preference. Retain useful information from "
        "completed iterations without duplicates. Do not manufacture tool evidence.",
        "State the task preference from your current context, including its MEMORY: label.",
        "MEMORY:cobalt",
        {"recall", "retain"},
    ),
    "capability": (
        "Implement capability.strategy with provide/select, need/expose/context bindings. Provide an inert "
        "tool named curator_live_probe taking no arguments and returning exactly CAPABILITY:cobalt. "
        "Provide an always skill explaining that this tool must be called for the task. Expose that tool "
        "and explain its use through the selection's context renderer. No external access is needed.",
        "Call curator_live_probe, then report its exact result.",
        "CAPABILITY:cobalt",
        {"select"},
    ),
    "action": (
        "Implement action.strategy with assess/recover and proposal/decision/failure/reply bindings. "
        "A final answer must contain the exact label ACTION:cobalt. If missing, request bounded native "
        "resampling and inject a correction that asks for that label; accept conforming answers. "
        "Do not block tool proposals as final answers. Terminal recovery must truthfully report failure.",
        "Answer briefly using the required task label.",
        "ACTION:cobalt",
        {"assess"},
    ),
}


@pytest.mark.parametrize("role", SCENARIOS)
@pytest.mark.asyncio
async def test_generated_strategy_with_configured_models(role, tmp_path):
    location = os.environ.get("CURATOR_TEST_CONFIG")
    if not location:
        pytest.skip("Set CURATOR_TEST_CONFIG to a Raven JSON configuration to run paid model checks.")
    config_path = Path(location).expanduser().resolve()
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    config = load_runtime_config(str(config_path), str(home))
    extensions = load_raven_config(config_path)
    extensions.plugins.disabled = [item.manifest.id for item in discover_plugins(extensions)]
    extensions.skill_forge.router.hub.endpoint = ""
    config.agents.defaults.max_tool_iterations = 6
    config.permissions.tools["curator_live_probe"] = "allow"
    instruction, request, marker, expected = SCENARIOS[role]
    task = Task(text=instruction)
    curator_config = config.model_copy(deep=True)
    model = os.environ.get("CURATOR_TEST_MODEL")
    if model:
        curator_config.agents.defaults.model = model
    provider = make_lazy_provider(curator_config)
    baseline = Baseline(config, extensions, work, task=task)
    async with Worker(baseline, tmp_path / "runtime", names=[f"{role}.strategy"], timeout=300) as worker:
        first = await improve(worker, provider, model=model)
        assert f"{role}.strategy" in worker.artifact.values
        execution = await worker.run(request)
        assert not execution.errors, execution.errors
        calls = {row["operation"] for row in execution.records if row["kind"] == f"{role}.call"}
        assert expected <= calls
        text = "".join(
            row["event"]["content"]
            for row in execution.records
            if row["kind"] == "runner.event" and row["event_type"] == "Text"
        )
        assert marker in text
        if role == "capability":
            requests = [row for row in execution.records if row["kind"] == "provider.request"]
            tool_results = [
                message
                for row in requests
                for message in row["parameters"].get("messages", [])
                if message.get("role") == "tool" and message.get("name") == "curator_live_probe"
            ]
            assert any(marker in str(message.get("content")) for message in tool_results)
        before = execution.artifact_id
        revised = await improve(
            worker,
            provider,
            model=model,
            feedback={
                "source": "human",
                "text": "Keep the current task and mechanism, but change the task label from cobalt to amber. "
                "Revise the strategy and relevant resources, explicitly migrate retained data where needed.",
            },
        )
        second = await worker.run(request.replace("cobalt", "amber"))
        assert not second.errors, second.errors
        assert second.artifact_id != before
        text = "".join(
            row["event"]["content"]
            for row in second.records
            if row["kind"] == "runner.event" and row["event_type"] == "Text"
        )
        assert marker.replace("cobalt", "amber") in text
        report = {
            "role": role,
            "task_id": task.id,
            "curator_model": curator_config.agents.defaults.model,
            "worker_model": config.agents.defaults.model,
            "first_trace": first.trace,
            "revision_trace": revised.trace,
            "first_turn": execution.turn_id,
            "second_turn": second.turn_id,
            "before_artifact": before,
            "after_artifact": second.artifact_id,
        }
        (tmp_path / "model-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
