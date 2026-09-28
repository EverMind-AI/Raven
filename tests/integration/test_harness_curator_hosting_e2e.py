"""Full strategy classes execute in the actual ACP child and retain isolated session state."""

import json
import os
import shlex
import sys
from functools import partial
from pathlib import Path

import pytest

from experimental.curator.harness import Artifact, Task
from experimental.curator.raven_adapter.baselines import Baseline
from experimental.curator.raven_adapter.hosting.acp import INSPECT
from experimental.curator.raven_adapter.hosting.connection import connection as agent_connection
from experimental.curator.raven_adapter.hosting.evidence import inspect as inspect_child
from experimental.curator.raven_adapter.inspection import Inspection
from experimental.curator.raven_adapter.worker import Worker
from raven.acp_client.acp_agent import AcpAgentBackend
from raven.acp_client.client import AcpClient
from raven.acp_client.pool import AcpConnectionPool
from raven.agent.subagent.instances import InstanceRegistry
from raven.config.loader import load_config
from raven.config.raven import load_raven_config
from raven.config.schema import ThirdPartyAcpSubagentConfig
from raven.contracts.llm_provider import LLMResponse
from raven.core.plugin_stack import discover_plugins
from raven.playbook import NodeSpec, PlaybookSpec, PlaybookStore
from tests.integration.test_harness_curator_e2e import baseline as baseline
from tests.integration.test_harness_curator_e2e import replay_provider
from tests.integration.test_harness_curator_planning_e2e import artifact as planning_artifact
from tests.integration.test_harness_curator_planning_e2e import response
from tests.test_agents_code_launcher import RUN_PY
from tests.test_agents_code_launcher import grounded as grounded
from tests.test_agents_code_launcher import launcher as launcher
from tests.test_harness_curator_strategies import files, values

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow(reason="Runs the native ACP server in an isolated child process."),
]

LAUNCHER = """import asyncio
import json
import sys
from pathlib import Path
from experimental.curator.raven_adapter.hosting.acp import main
from raven.providers.base import LLMProvider
from raven.contracts.llm_provider import GenerationSettings, LLMResponse
from tests.integration.test_harness_curator_planning_e2e import response

class Provider(LLMProvider):
    async def chat(self, messages, tools=None, **kwargs):
        names = {tool.get("function", {}).get("name") for tool in (tools or [])}
        if "emit_session_title" in names:
            return response("emit_session_title", {"title": "Evidence"})
        if "review_verdict" in names:
            return response("review_verdict", {"pass": True, "unsupported_claims": [], "estimated_cells": [], "issues": []})
        if "evidence_probe" not in names:
            return LLMResponse(content=json.dumps({"preferred": [], "alternatives": [], "plain_ok": False, "sound": False, "research": False, "turn": "context", "reason": "Use the local evidence tool", "why": "Local evidence"}))
        evidence = any(row.get("role") == "tool" and "FACT:cobalt" in str(row.get("content", "")) for row in messages)
        correction = any(row.get("role") == "user" and "Call evidence_probe before finishing." in str(row.get("content", "")) for row in messages)
        if not evidence and correction:
            return response("evidence_probe", {})
        answer = "The evidence says cobalt." if evidence else "An unsupported assertion."
        return LLMResponse(content="## Answer\\n" + answer + "\\n## Findings\\n" + answer + "\\n## Limitations\\nLocal evidence only.")

    def get_default_model(self):
        return "openai/gpt-4o-mini"

def provider(config):
    instance = Provider()
    instance.generation = GenerationSettings(temperature=config.agents.defaults.temperature)
    return instance

asyncio.run(main(Path(sys.argv[1]), provider_factory=provider))
"""


def hosted(baseline, tmp_path):
    baseline.task = Task(id="hosted-task", text="Verify")
    baseline.hosting = "acp"
    baseline.allow_delegation = False
    baseline.config.providers.openai.api_key = "local-test-only"
    baseline.config.permissions.tools["evidence_probe"] = "allow"
    planning = planning_artifact()
    supplied = {**values(), **planning["values"]}
    dropped = baseline.extensions.context.drop_segments
    if {"skills", "active_skills"} & set(dropped):
        supplied["memory.context_config"] = {
            "drop_segments": [name for name in dropped if name not in {"skills", "active_skills"}]
        }
    artifact = Artifact(values=supplied, files={**files(), **planning["files"]})
    state = tmp_path / "state"
    deployment = tmp_path / "deployment.json"
    deployment.write_text(
        json.dumps(
            {
                "baseline": baseline.export(),
                "artifact": artifact.model_dump(mode="json"),
                "state_dir": str(state),
                "grants": {"names": list(artifact.values)},
            }
        )
    )
    deployment.chmod(0o600)
    launcher = tmp_path / "host.py"
    launcher.write_text(LAUNCHER)
    repository = Path(__file__).resolve().parents[2]
    env = {
        "PYTHONPATH": str(repository),
        **({"PYTHONTZPATH": os.environ["PYTHONTZPATH"]} if "PYTHONTZPATH" in os.environ else {}),
        "RAVEN_HOME": str(tmp_path / "raven"),
    }
    command = shlex.join([sys.executable, str(launcher), str(deployment)])
    return command, env, state


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["connection", "delegate"])
@pytest.mark.parametrize("profile", ["plain", "code", "research", "oncall", "design", "ppt"])
async def test_full_four_strategies_run_in_the_responding_acp_process(baseline, tmp_path, route, profile, grounded):
    if profile == "code":
        rendered = grounded.render_acp_config(RUN_PY.parent / "config.json")
        config, extensions = load_config(rendered), load_raven_config(rendered)
        extensions.memory.backend = None
        extensions.plugins.disabled = [
            plugin.manifest.id for plugin in discover_plugins(extensions) if plugin.manifest.id != "code-flow"
        ]
        extensions.skill_forge.router.hub.endpoint = ""
        baseline = Baseline(config, extensions, baseline.workdir, source_roots=(RUN_PY.parent,))
    elif profile != "plain":
        from experimental.curator.raven_adapter.baselines.agents import prepare_agent

        host = tmp_path / "parent-config"
        host.mkdir()
        (host / "config.json").write_text(
            json.dumps(
                {
                    "agents": {"defaults": {"model": "openai/gpt-4o-mini", "provider": "openrouter"}},
                    "providers": {"openrouter": {"apiKey": "local-test-only"}},
                }
            )
        )
        baseline = prepare_agent(
            Path(__file__).resolve().parents[2] / "agents" / f"raven-{profile}",
            root=tmp_path / "prepared",
            workdir=baseline.workdir,
            environment={"RAVEN_HOME": str(host), "RESEARCH_SERPER_API_KEY": "local-test-only"},
        )
        baseline.extensions.memory.backend = None
        baseline.extensions.session_title.enabled = False
        baseline.extensions.skill_forge.router.hub.endpoint = ""
    command, env, state = hosted(baseline, tmp_path)
    pool = AcpConnectionPool() if route == "delegate" else None
    backend = None
    if pool is not None:
        backend = AcpAgentBackend(
            name="Hosted",
            command=command,
            cwd=str(baseline.workdir),
            env=env,
            pool=pool,
            registry=InstanceRegistry(tmp_path / "instances.json"),
        )
        connection = await agent_connection(backend, workspace=baseline.workdir)
        client = connection.client
    else:
        client = await AcpClient.launch(name="Hosted", command=command, cwd=str(baseline.workdir), env=env)
        await client.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=40)
    try:
        initial = await client.request(INSPECT, {}, timeout=10)
        inspection = Inspection.restore(initial["inspection"])
        names = {target.name for target in inspection.declaration.targets}
        assert {f"{role}.strategy" for role in ("planning", "memory", "action", "capability")} <= names
        assert "planning.playbooks" not in names
        assert "capability.mcp" not in names
        with pytest.raises(ValueError):
            Inspection.restore(initial["inspection"], names=["capability.mcp"])
        if profile == "code":
            assert "code-flow" in {plugin["id"] for plugin in inspection.facts["plugins"]}
            assert "todo" in {tool["function"]["name"] for tool in inspection.facts["tools"]}
        for index in range(2):
            if backend is not None:
                reply = await backend.run(
                    "Obtain actual evidence.",
                    task_id=f"work-{index}",
                    workspace=baseline.workdir,
                    executor=None,
                    session_key="parent:task",
                )
                assert "cobalt" in reply, reply
                assert (await agent_connection(backend, workspace=baseline.workdir)).client is client
            else:
                session = await client.request(
                    "session/new", {"cwd": str(baseline.workdir), "mcpServers": []}, timeout=20
                )
                result = await client.request(
                    "session/prompt",
                    {
                        "sessionId": session["sessionId"],
                        "prompt": [{"type": "text", "text": "Obtain actual evidence."}],
                    },
                    timeout=45,
                )
                assert result["stopReason"] == "end_turn", result
        final = await inspect_child(client, offset=0)
        facts = final["inspection"]["facts"]
        assert final["pid"] == initial["pid"]
        for role in ("planning", "capability", "action"):
            assert len(facts[role]["sessions"]) == 2, (role, facts[role])
        assert facts["memory"]["state"]["facts"] == {"evidence_probe": "cobalt"}
        records = final["records"]
        action_records = [row for row in records if row["kind"] == "action.result"]
        assert all(row["turn_id"] for row in action_records)
        assert {row["conversation"] for row in action_records} == set(facts["action"]["sessions"])
        for role in ("planning", "memory", "capability", "action"):
            assert any(row["kind"] == f"{role}.result" for row in records), role
        assert any(row["kind"] == "action.result" and row["result"].get("kind") == "retry" for row in records)
        logged = [json.loads(line) for line in Path(final["log"]).read_text().splitlines()]
        assert len(logged) == final["total"]
        requests = [row for row in logged if row["kind"] == "provider.request"]
        assert "CAPABILITY_SKILL_RUNTIME" in str(requests)
        assert "Call evidence_probe before finishing." in str(requests)
        assert not any(
            row["kind"] in {"planning.error", "memory.error", "capability.error", "action.error"} for row in records
        )
    finally:
        if pool is not None:
            await pool.close_all()
        else:
            await client.close()
    assert (state / "planning.json").exists() and (state / "memory.json").exists()


@pytest.mark.asyncio
async def test_parent_playbook_executes_the_same_child_its_inspection_observes(baseline, tmp_path):
    child = Baseline.restore(baseline.export())
    child.config.agents.defaults.workspace = str(tmp_path / "child-home")
    child.config.workspace_path.mkdir()
    command, env, state = hosted(child, tmp_path)
    baseline.task = Task(id="parent-task", text="Delegate evidence work")
    baseline.config.playbooks.enabled = True
    baseline.extensions.plugins.disabled = [name for name in baseline.extensions.plugins.disabled if name != "playbook"]
    baseline.config.permissions.tools["load_playbook"] = "allow"
    baseline.config.subagents.agents = [
        ThirdPartyAcpSubagentConfig(name="Hosted", command=command, cwd=str(baseline.workdir), env=env)
    ]
    store = PlaybookStore(baseline.config.workspace_path / "playbooks", builtin_root=tmp_path / "empty")
    store.save(
        PlaybookSpec(
            name="verify",
            description="Delegate evidence work",
            task_summary="Obtain evidence",
            mode="dag",
            confirm=False,
            triggers={"keywords": ["verify"]},
            nodes=[
                NodeSpec(
                    id="evidence",
                    subagent="Hosted",
                    node_summary="Obtain evidence",
                    prompt_template="Obtain actual evidence.",
                )
            ],
        )
    )
    replies = [
        response("load_playbook", {"name": "verify"}),
        LLMResponse(content="Delegated."),
        LLMResponse(content="Done."),
    ]
    async with Worker(
        baseline, tmp_path / "parent", provider_factory=partial(replay_provider, responses=replies), timeout=60
    ) as worker:
        before = await worker.inspect_agent("Hosted")
        assert not before.facts["action"]["sessions"]
        executed = await worker.run("Run verify.")
        assert not executed.errors, executed.errors
        after = await worker.inspect_agent("Hosted")
        assert after.facts["action"]["sessions"]
        assert after.facts["memory"]["state"]["facts"] == {"evidence_probe": "cobalt"}
        assert any(row["kind"] == "dag.progress" for row in executed.records)
    rows = [json.loads(line) for line in (state / "observations.jsonl").read_text().splitlines()]
    assert len([row for row in rows if row["kind"] == "hosting.ready"]) == 1
