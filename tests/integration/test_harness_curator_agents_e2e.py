"""A rendered product profile retains its native plugins when Curator changes its harness."""

import pytest

from experimental.curator.harness import Task
from experimental.curator.raven_adapter.inspection import Baseline
from experimental.curator.raven_adapter.worker import Worker
from raven.config.loader import load_config
from raven.config.raven import load_raven_config
from raven.core.plugin_stack import discover_plugins
from tests.integration.test_harness_curator_e2e import plan_for, replay_provider
from tests.test_agents_code_launcher import RUN_PY
from tests.test_agents_code_launcher import grounded as grounded
from tests.test_agents_code_launcher import launcher as launcher


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rendered_code_agent_preserves_plugin_tools_after_curator_install(grounded, tmp_path):
    rendered = grounded.render_acp_config(RUN_PY.parent / "config.json")
    config = load_config(rendered)
    extensions = load_raven_config(rendered)
    extensions.memory.backend = None
    extensions.plugins.disabled = [
        item.manifest.id for item in discover_plugins(extensions) if item.manifest.id != "code-flow"
    ]
    extensions.skill_forge.router.hub.endpoint = ""
    workdir = tmp_path / "work"
    workdir.mkdir()
    baseline = Baseline(config, extensions, workdir, task=Task(id="code-task", text="Inspect this project"))
    async with Worker(baseline, tmp_path / "worker", provider_factory=replay_provider) as worker:
        before = await worker.inspect()
        assert "code-flow" in {plugin["id"] for plugin in before.facts["plugins"]}
        assert "todo" in {tool["function"]["name"] for tool in before.facts["tools"]}
        candidate = before.declaration.accept(
            plan_for("action.config"), {"values": {"action.config": {"temperature": 0.2}}}
        )
        validation = await worker.check(candidate)
        assert validation.passed, validation.errors
        await worker.install(candidate)
        after = await worker.inspect()
        assert "code-flow" in {plugin["id"] for plugin in after.facts["plugins"]}
        assert {tool["function"]["name"] for tool in after.facts["tools"]} == {
            tool["function"]["name"] for tool in before.facts["tools"]
        }
        assert after.facts["configuration"]["config"]["agents"]["defaults"]["temperature"] == 0.2


@pytest.mark.integration
@pytest.mark.parametrize("name", ["raven-code", "raven-research", "raven-oncall", "raven-design", "raven-ppt"])
def test_existing_agent_preparation_reuses_renderer_and_isolates_host_environment(tmp_path, name):
    import json
    import os
    from pathlib import Path

    from experimental.curator.raven_adapter.baselines.agents import prepare_agent
    from raven.config.mode_catalogue import build_mode_catalogue

    host = tmp_path / "host"
    host.mkdir()
    (host / "config.json").write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "openai/gpt-4o-mini", "provider": "openrouter"}},
                "providers": {"openrouter": {"apiKey": "synthetic-key"}},
            }
        )
    )
    root = tmp_path / "prepared"
    directory = Path(__file__).resolve().parents[2] / "agents" / name
    previous = dict(os.environ)
    baseline = prepare_agent(
        directory,
        root=root,
        workdir=tmp_path,
        task=Task(id="task", text="Inspect"),
        environment={"RAVEN_HOME": str(host), "RESEARCH_SERPER_API_KEY": "synthetic-search-key"},
    )
    assert os.environ == previous
    assert baseline.source_roots == (directory,)
    assert baseline.config.workspace_path.is_relative_to(root)
    assert baseline.task.id == "task"
    assert build_mode_catalogue(baseline.config).get(baseline.mode) is not None
    assert (root / "prepared.json").stat().st_mode & 0o777 == 0o600
    if name in {"raven-code", "raven-oncall", "raven-research"}:
        assert str(directory / "plugins") in baseline.extensions.plugins.dirs


@pytest.mark.integration
@pytest.mark.asyncio
async def test_effective_code_mechanism_survives_modification_and_supporting_sources_are_searchable(grounded, tmp_path):
    from experimental.curator.raven_adapter.exploration import Exploration

    rendered = grounded.render_acp_config(RUN_PY.parent / "config.json")
    config, extensions = load_config(rendered), load_raven_config(rendered)
    extensions.memory.backend = None
    extensions.plugins.disabled = [p.manifest.id for p in discover_plugins(extensions) if p.manifest.id != "code-flow"]
    extensions.skill_forge.router.hub.endpoint = ""
    workdir = tmp_path / "work"
    workdir.mkdir()
    baseline = Baseline(config, extensions, workdir, task=Task(id="code-task", text="Inspect project"))
    async with Worker(baseline, tmp_path / "worker", provider_factory=replay_provider) as worker:
        before = await worker.inspect()
        mechanism = next(m for m in before.mechanisms if m.name == "code.plan")
        assert set(mechanism.roles) == {"memory", "planning", "capability"}
        async with Exploration(config, before) as exploration:
            tool = exploration.read_source(name="tool.todo")
            assert "TodoStore" in tool["text"]
            assert "code_flow" in tool["path"]
            assert any("code_flow" in str(path) for path in exploration.originals)
        candidate = before.declaration.accept(
            plan_for("action.config"), {"values": {"action.config": {"temperature": 0.2}}}
        )
        await worker.install(candidate)
        after = await worker.inspect()
        assert any(m.name == "code.plan" for m in after.mechanisms)
        assert any(m.name == "authored.action.config" for m in after.mechanisms)
        assert before.declaration.baseline != after.declaration.baseline
        removal = after.declaration.accept(plan_for("action.config"), {"values": {}, "remove": ["action.config"]})
        await worker.install(removal)
        current = await worker.inspect()
        assert not any(m.name == "authored.action.config" for m in current.mechanisms)
        assert any(m.name == "code.plan" for m in current.mechanisms)
