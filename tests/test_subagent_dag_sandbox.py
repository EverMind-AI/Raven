"""A DAG node runs under the sandbox the host configured, or not at all.

``tools.sandbox`` promises no silent fallback to host execution
(``docs/sandbox/usage.md``). The spawn path honors it -- ``SubagentManager``
builds an executor per dispatch -- but the graph paths historically dropped the
config on the floor: ``SubAgentDagTool`` called ``run_dag`` without ``sandbox``,
so every node backend built its ``ExecTool`` on ``DirectExecutor`` and ran on
the host. These tests pin both halves of the fix: the tool asks an injected
factory for the run's executor and hands it to ``run_dag``, and the manager
answers with the same recipe a spawn gets.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from raven.agent.subagent import dag_tool as dag_tool_mod
from raven.agent.subagent import manager as manager_mod
from raven.agent.subagent.dag_runner import DagRunResult
from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.agent.subagent.manager import SubagentManager
from raven.agent.workdir import bind
from raven.config.schema import ThirdPartyCliSubagentConfig
from raven.sandbox import SandboxConfig


class _StubExecutor:
    """Stands in for a BoxliteExecutor: an async context manager, entered once."""

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    @property
    def is_sandboxed(self) -> bool:
        return True

    async def __aenter__(self) -> "_StubExecutor":
        self.entered += 1
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self.exited += 1
        return False


class _StubProvider:
    def get_default_model(self) -> str:
        return "stub-model"


async def _run_one_node(
    tool: SubAgentDagTool,
    workdir: Path,
    recorded: dict,
    reached: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submit a one-node graph and wait for the tool's ``run_dag`` call.

    The runner is stubbed out rather than a node backend: what this pins is the
    kwargs the tool hands the runner, one of which is the executor every node
    backend is dispatched with.
    """

    async def _fake_run_dag(spec: Any, **kwargs: Any) -> DagRunResult:
        recorded.update(kwargs)
        reached.set()
        return DagRunResult(run_id="r1", dir=str(workdir))

    monkeypatch.setattr(dag_tool_mod, "run_dag", _fake_run_dag)
    tool.set_context("web", "chat-1")
    with bind(workdir):
        await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "stub", "node_summary": "node a", "prompt_template": "hi"}],
        )
    await asyncio.wait_for(reached.wait(), timeout=5)


@pytest.mark.asyncio
async def test_the_graph_tool_hands_the_sandbox_executor_to_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host with ``tools.sandbox`` configured gets its executor into the graph.

    The factory is asked with the directory the nodes will run in -- the same
    workspace a spawn's executor is built over -- and what it returns travels
    to ``run_dag`` as ``sandbox``, entered for the run and exited after it.
    Without this, ``run_dag``'s ``sandbox=None`` default reached every node
    backend and a raven-loop node's ``ExecTool`` fell back to ``DirectExecutor``:
    host execution under a sandboxed config, exactly what the docs promise
    never happens silently.
    """
    project = tmp_path / "project"
    project.mkdir()
    executor = _StubExecutor()
    asked_with: list[Path] = []

    def factory(workspace: Path) -> _StubExecutor:
        asked_with.append(workspace)
        return executor

    recorded: dict = {}
    reached = asyncio.Event()
    tool = SubAgentDagTool(
        workspace=tmp_path / "home",
        agents=[ThirdPartyCliSubagentConfig(name="stub", command="cat")],
        sandbox_factory=factory,
    )
    (tmp_path / "home").mkdir()

    await _run_one_node(tool, project, recorded, reached, monkeypatch)

    assert asked_with == [project], "the factory is asked with the run's own workdir"
    assert recorded["sandbox"] is executor, "the runner dispatches nodes under the configured executor"
    assert executor.entered == 1 and executor.exited == 1, "the executor spans exactly the run"


@pytest.mark.asyncio
async def test_the_graph_tool_without_a_sandbox_keeps_sandbox_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sandbox configured: the run goes out with ``sandbox=None``, as before.

    A host that never configured a sandbox has no executor to build, and the
    tool must not invent one -- the node backends' fallback stays exactly the
    behavior this file exists to fix only where a sandbox was asked for.
    """
    project = tmp_path / "project"
    project.mkdir()
    recorded: dict = {}
    reached = asyncio.Event()
    tool = SubAgentDagTool(
        workspace=tmp_path / "home",
        agents=[ThirdPartyCliSubagentConfig(name="stub", command="cat")],
    )
    (tmp_path / "home").mkdir()

    await _run_one_node(tool, project, recorded, reached, monkeypatch)

    assert recorded["sandbox"] is None


def test_the_manager_builds_the_spawn_recipe_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory answers with what a spawn would run under, built the same way.

    One recipe, in the manager, next to the spawn call sites: this config, the
    owned-id set and the agent-home mount -- so a graph node sees the same VM
    volumes a spawn to the same agent does, memory and skills included.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    config = SandboxConfig(backend="boxlite")
    manager = SubagentManager(provider=_StubProvider(), workspace=home, sandbox_config=config)
    sentinel = _StubExecutor()
    calls: list[tuple[tuple, dict]] = []

    def fake_build_executor(*args: Any, **kwargs: Any) -> _StubExecutor:
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(manager_mod, "build_executor", fake_build_executor)

    executor = manager.sandbox_executor_for(project)

    assert executor is sentinel
    (args, kwargs) = calls[0]
    assert args[0] is config
    assert args[1] == project
    assert args[2] is manager._owned_ids
    assert args[3] == ((str(home), "/agent-home", "rw"),), "agent home is mounted, as a spawn's is"
    assert "sandbox_dir" in kwargs


def test_the_manager_without_a_sandbox_config_builds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No config, no executor: the answer is ``None`` and nothing is built.

    ``build_executor(None, ...)`` would return a ``DirectExecutor`` and log the
    host-execution warning; the tool's no-sandbox path needs neither -- it
    keeps the run exactly as an unconfigured host always ran it.
    """
    manager = SubagentManager(provider=_StubProvider(), workspace=tmp_path)
    monkeypatch.setattr(
        manager_mod,
        "build_executor",
        lambda *a, **k: pytest.fail("build_executor must not be called without a sandbox config"),
    )

    assert manager.sandbox_executor_for(tmp_path) is None
