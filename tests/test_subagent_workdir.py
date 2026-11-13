"""A sub-agent works in the directory its spawning turn was bound to."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from raven.agent.subagent import dag_tool as dag_tool_mod
from raven.agent.subagent import manager as manager_mod
from raven.agent.subagent.backends import raven_loop as raven_loop_mod
from raven.agent.subagent.backends.raven_loop import RavenLoopBackend
from raven.agent.subagent.dag_reader import DagReadError
from raven.agent.subagent.dag_runner import DagRunResult
from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.agent.subagent.history import dag_root, nodes_root
from raven.agent.subagent.manager import SubagentManager
from raven.agent.workdir import bind
from raven.config.schema import ThirdPartyCliSubagentConfig
from raven.contracts.llm_provider import LLMResponse
from raven.session.manager import SessionManager


def _sdir(home: Path, key: str) -> Path:
    """The session metadata directory the history roots hang off."""
    return SessionManager(home).session_dir(key)


class _StubProvider:
    def get_default_model(self) -> str:
        return "stub-model"


class _DummyExecutor:
    # Host execution: ExecTool reads this at construction.
    is_sandboxed = False

    async def __aenter__(self) -> "_DummyExecutor":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _RecordingBackend:
    """Records the workspace it is handed, and can block until released."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.workspace: Path | None = None
        self._gate = gate

    async def run(self, task: str, *, task_id, workspace, executor, session_key=None, instance=None, **_) -> str:
        if self._gate is not None:
            await self._gate.wait()
        self.workspace = workspace
        return "done"


def _manager(tmp_path: Path, monkeypatch, backend: _RecordingBackend) -> SubagentManager:
    manager = SubagentManager(provider=_StubProvider(), workspace=tmp_path / "fallback")
    monkeypatch.setattr(manager_mod, "build_executor", lambda *a, **k: _DummyExecutor())
    monkeypatch.setattr(manager, "_resolve_backend", lambda agent: backend)
    monkeypatch.setattr(manager, "_announce_result", _noop_announce)
    return manager


async def _noop_announce(*args, **kwargs) -> None:
    return None


@pytest.mark.asyncio
async def test_spawn_captures_the_bound_workdir(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "session"
    session.mkdir()
    backend = _RecordingBackend()
    manager = _manager(tmp_path, monkeypatch, backend)

    with bind(session):
        await manager.spawn("do the thing", session_key="web:abc", workspace=session)

    await asyncio.gather(*manager._running_tasks.values())
    assert backend.workspace == session


@pytest.mark.asyncio
async def test_spawn_records_its_input_and_output_under_the_session_history(tmp_path: Path, monkeypatch) -> None:
    """A spawn leaves the same kind of trail a DAG node does.

    The record is keyed on agent home and the session, not on the working
    directory the sub-agent ran in: pointing the session somewhere else must
    not scatter or orphan the history.
    """
    home = tmp_path / "fallback"
    session = tmp_path / "project"
    session.mkdir()
    manager = _manager(tmp_path, monkeypatch, _RecordingBackend())

    with bind(session):
        await manager.spawn("do the thing", session_key="web:abc", workspace=session)
    await asyncio.gather(*manager._running_tasks.values())

    (prompt,) = sorted(nodes_root(_sdir(home, "web:abc")).glob("*.prompt.md"))
    node_id = prompt.name.removesuffix(".prompt.md")
    files = prompt.parent
    assert prompt.read_text(encoding="utf-8") == "do the thing"
    assert (files / f"{node_id}.out.md").read_text(encoding="utf-8") == "done"
    meta = json.loads((files / f"{node_id}.meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert meta["session_key"] == "web:abc"
    # The working directory is recorded as a fact about the run, not used to
    # place the record -- the record lives under agent home either way.
    assert meta["working_directory"] == str(session)
    assert not (session / "subagent_history").exists()


@pytest.mark.asyncio
async def test_spawn_records_a_failed_call(tmp_path: Path, monkeypatch) -> None:
    """Without this the only trace of a crashed sub-agent is a log line."""
    home = tmp_path / "fallback"

    class _FailingBackend:
        async def run(self, task: str, **kwargs) -> str:
            raise RuntimeError("boom")

    manager = _manager(tmp_path, monkeypatch, _FailingBackend())
    await manager.spawn("do the thing", session_key="web:abc")
    await asyncio.gather(*manager._running_tasks.values())

    (prompt,) = sorted(nodes_root(_sdir(home, "web:abc")).glob("*.prompt.md"))
    node_id = prompt.name.removesuffix(".prompt.md")
    assert "boom" in (prompt.parent / f"{node_id}.error.md").read_text(encoding="utf-8")
    assert prompt.read_text(encoding="utf-8") == "do the thing"
    assert json.loads((prompt.parent / f"{node_id}.meta.json").read_text(encoding="utf-8"))["status"] == "failed"


@pytest.mark.asyncio
async def test_workdir_survives_the_turn_ending(tmp_path: Path, monkeypatch) -> None:
    """The captured value must not be re-read after the binding is released."""
    session = tmp_path / "session"
    session.mkdir()
    gate = asyncio.Event()
    backend = _RecordingBackend(gate)
    manager = _manager(tmp_path, monkeypatch, backend)

    with bind(session):
        await manager.spawn("do the thing", session_key="web:abc", workspace=session)

    gate.set()
    await asyncio.gather(*manager._running_tasks.values())
    assert backend.workspace == session


@pytest.mark.asyncio
async def test_dag_records_go_to_history_while_nodes_run_in_the_bound_workdir(tmp_path: Path, monkeypatch) -> None:
    """The two directories a DAG run touches must stay separate.

    Nodes run in the bound working directory (it is their cwd, and what
    ``{{ ref:<path> }}`` resolves against), while the prompt/output records go
    to the session's history under agent home. Collapsing them back into one
    would either write the audit trail into the user's project or resolve the
    user's file references against the history directory.

    Both are read while the submitting turn still holds them: the run is
    backgrounded, so the block that bound the working directory has exited by
    the time the graph reaches the runner.
    """
    home = tmp_path / "home"
    session = tmp_path / "project"
    home.mkdir()
    session.mkdir()
    recorded: dict = {}
    reached = asyncio.Event()

    async def _fake_run_dag(spec, **kwargs):
        recorded.update(kwargs)
        reached.set()
        return DagRunResult(run_id="r1", dir=str(session))

    monkeypatch.setattr(dag_tool_mod, "run_dag", _fake_run_dag)
    tool = SubAgentDagTool(
        workspace=home,
        agents=[ThirdPartyCliSubagentConfig(name="stub", command="cat")],
    )
    tool.set_context("web", "chat-1")

    with bind(session):
        await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "stub", "node_summary": "node a", "prompt_template": "hi"}],
        )

    await asyncio.wait_for(reached.wait(), timeout=5)

    assert recorded["workdir"] == str(session)
    assert recorded["run_root"] == str(dag_root(_sdir(home, "web:chat-1")))
    assert not str(recorded["run_root"]).startswith(str(session))


def _write_run_dir(root: Path, run_id: str) -> None:
    """The minimum on-disk run the reader accepts: a graph, unfinalized."""
    rdir = root / run_id
    rdir.mkdir(parents=True)
    (rdir / "graph.json").write_text(
        json.dumps({"nodes": [{"id": "a", "subagent": "stub", "prompt_template": "hi"}]}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_dag_run_is_read_back_from_the_session_that_wrote_it(tmp_path: Path) -> None:
    """A read must land on the root the write used, with no turn in progress.

    Reads arrive between turns -- a reloaded tab, a gateway restarted mid-run --
    so the root cannot come from anything turn-scoped. It is derived from agent
    home plus the session key, which is what the caller names.
    """
    home = tmp_path / "home"
    home.mkdir()
    run_id = "20260805T101112Z-abcdef01"
    _write_run_dir(dag_root(_sdir(home, "web:chat-1")), run_id)

    tool = SubAgentDagTool(workspace=home, agents=[])

    run = await tool.read_run(run_id, "web:chat-1")
    assert run["dir"] == str(dag_root(_sdir(home, "web:chat-1")) / run_id)
    assert [f["node"] for f in run["files"]] == ["a"]

    # Naming a different session must not reach it: the roots are per-session,
    # and asserting the miss pins which root the success above came from.
    with pytest.raises(DagReadError):
        await tool.read_run(run_id, "web:other-chat")


@pytest.mark.asyncio
async def test_dag_node_is_read_back_from_the_session_that_wrote_it(tmp_path: Path) -> None:
    """``read_node`` resolves its root exactly like ``read_run``.

    It is a separate RPC on both surfaces and the one that carried no session
    key at all before, so it needs its own guard.
    """
    home = tmp_path / "home"
    home.mkdir()
    run_id = "20260805T101112Z-abcdef01"
    session_dir = _sdir(home, "web:chat-1")
    root = dag_root(session_dir)
    _write_run_dir(root, run_id)
    nodes = nodes_root(session_dir)
    nodes.mkdir(parents=True)
    (nodes / "a.prompt.md").write_text("rendered prompt", encoding="utf-8")

    tool = SubAgentDagTool(workspace=home, agents=[])

    node = await tool.read_node(run_id, "a", session_key="web:chat-1")
    assert node["prompt"] == "rendered prompt"

    # Against the wrong root this one does not raise -- a missing prompt file
    # reads back as None, the same answer as a node that never ran. That is what
    # would make a root mismatch silent: the node panel just stays blank. So the
    # guard has to assert the emptiness, not an exception.
    assert (await tool.read_node(run_id, "a", session_key="web:other-chat"))["prompt"] is None


# ── the sub-agent's two directories: agent home vs the session working dir ──


class _PromptCapturingProvider:
    """Ends the sub-agent loop on the first reply, keeping its system prompt."""

    def __init__(self) -> None:
        self.system_prompt: str = ""

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, *, messages, tools, model) -> LLMResponse:
        self.system_prompt = messages[0]["content"]
        return LLMResponse(content="done", finish_reason="stop")


def _agent_home_with_a_skill(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    skill = home / "skills" / "invoice-audit"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: invoice-audit\ndescription: Audit an invoice.\n---\n\n# invoice-audit\n",
        encoding="utf-8",
    )
    return home


@pytest.mark.asyncio
async def test_subagent_reads_skills_from_agent_home_not_the_session_dir(tmp_path: Path) -> None:
    """One value must not drive both roles: the session directory is where the
    sub-agent works, agent home is where its skills and memory live. Feeding the
    session directory to the catalog builds a raven tree inside the user's own
    directory and hides every skill the agent actually has."""
    home = _agent_home_with_a_skill(tmp_path)
    session = tmp_path / "my-repo"
    session.mkdir()
    provider = _PromptCapturingProvider()
    backend = RavenLoopBackend(provider=provider, model="stub-model", agent_home=home)

    await backend.run("do the thing", task_id="t1", workspace=session, executor=_DummyExecutor())

    assert "invoice-audit" in provider.system_prompt
    assert str(session) in provider.system_prompt
    assert not (session / "skills").exists()
    assert not (session / "user_memory").exists()
    assert not (session / "sessions").exists()


@pytest.mark.asyncio
async def test_subagent_fences_on_both_the_session_dir_and_agent_home(tmp_path: Path, monkeypatch) -> None:
    """Before the split the sub-agent's single fence root WAS agent home; it
    must not lose access to the memory and skills its prompt points it at."""
    home = _agent_home_with_a_skill(tmp_path)
    session = tmp_path / "my-repo"
    session.mkdir()
    backend = RavenLoopBackend(
        provider=_PromptCapturingProvider(),
        model="stub-model",
        agent_home=home,
        restrict_to_workspace=True,
    )
    captured: dict = {}

    class _CapturingRegistry:
        tool_names: tuple[str, ...] = ()

        def __init__(self, **kwargs) -> None:
            pass

        def register(self, tool) -> None:
            captured.setdefault(tool.name, tool)

        def set_withheld_source(self, source) -> None:
            pass

        def get_definitions(self):
            return []

    monkeypatch.setattr(raven_loop_mod, "ToolRegistry", _CapturingRegistry)
    await backend.run("do the thing", task_id="t1", workspace=session, executor=_DummyExecutor())

    assert set(captured["read_file"]._allowed_dirs) == {session, home}
    assert set(captured["exec"].extra_allowed_dirs) == {session, home}


class _ToolCapturingRegistry:
    """Stands in for ``ToolRegistry`` so a test can call the tools a run built."""

    captured: dict = {}

    def __init__(self, **kwargs) -> None:
        pass

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(self.captured)

    def register(self, tool, **kwargs) -> None:
        self.captured.setdefault(tool.name, tool)

    def set_withheld_source(self, source) -> None:
        pass

    def get_definitions(self):
        return []


def _confined_run_tools(tmp_path: Path, monkeypatch, **backend_kw) -> tuple[dict, str]:
    home = _agent_home_with_a_skill(tmp_path)
    session = tmp_path / "my-repo"
    session.mkdir()
    provider = _PromptCapturingProvider()
    backend = RavenLoopBackend(
        provider=provider, model="stub-model", agent_home=home, restrict_to_workspace=True, **backend_kw
    )
    captured: dict = {}
    monkeypatch.setattr(_ToolCapturingRegistry, "captured", captured)
    monkeypatch.setattr(raven_loop_mod, "ToolRegistry", _ToolCapturingRegistry)
    asyncio.run(backend.run("do the thing", task_id="t1", workspace=session, executor=_DummyExecutor()))
    return captured, provider.system_prompt


def _point_live_config(tmp_path: Path, monkeypatch, payload: dict) -> None:
    from raven.config.live import LiveConfig

    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(raven_loop_mod, "_LIVE_CONFIG", LiveConfig(path))


def test_a_confined_subagent_reads_a_builtin_skill_through_read_skill(tmp_path: Path, monkeypatch) -> None:
    """A builtin skill's SKILL.md sits inside the installed package, outside
    both roots a confined run may read, so pointing the sub-agent at that path
    hands it a skill it cannot open. ``read_skill`` reads the body out of the
    registry instead, which no fence stands in front of -- the route the main
    agent already takes under ``restrictToWorkspace``."""
    _point_live_config(tmp_path, monkeypatch, {})
    tools, prompt = _confined_run_tools(tmp_path, monkeypatch)

    builtin = Path(raven_loop_mod.__file__).parents[3] / "memory_engine" / "skills" / "weather" / "SKILL.md"
    assert builtin.is_file()
    refused = asyncio.run(tools["read_file"].execute(path=str(builtin)))
    assert refused.startswith("Error"), "the fence itself stays: only the skill route goes around it"

    body = asyncio.run(tools["read_skill"].execute(skill_id="local/weather"))
    assert not body.startswith("Error"), body
    assert "### Skill: weather" in body
    assert 'read_skill("local/<name>")' in prompt
    assert "Read SKILL.md with read_file" not in prompt


def test_a_subagent_without_read_skill_keeps_the_file_route(tmp_path: Path, monkeypatch) -> None:
    """A row whose tool whitelist leaves ``read_skill`` out is told the route it
    does have, rather than a tool it cannot call."""
    _point_live_config(tmp_path, monkeypatch, {})
    tools, prompt = _confined_run_tools(tmp_path, monkeypatch, tools_allow={"read_file"})

    assert "read_skill" not in tools
    assert "invoice-audit" in prompt
    assert "Read SKILL.md with read_file" in prompt
    assert "read_skill(" not in prompt


def test_a_subagent_neither_offers_nor_reads_a_blocklisted_skill(tmp_path: Path, monkeypatch) -> None:
    """The operator's blocklist reaches the sub-agent the way it reaches the
    main agent: the menu leaves the skill out and ``read_skill`` refuses it."""
    _point_live_config(tmp_path, monkeypatch, {"skillForge": {"blocklist": ["invoice-audit"]}})
    tools, prompt = _confined_run_tools(tmp_path, monkeypatch)

    assert "invoice-audit" not in prompt
    refused = asyncio.run(tools["read_skill"].execute(skill_id="local/invoice-audit"))
    assert refused.startswith("Error") and "blocklist" in refused


def test_an_empty_skill_allow_list_leaves_read_skill_nothing_to_read(tmp_path: Path, monkeypatch) -> None:
    """``skills_allow=[]`` is a charter: this agent gets no skills. The menu
    already says so; ``read_skill`` has to agree rather than read any skill
    whose id the agent happens to know."""
    _point_live_config(tmp_path, monkeypatch, {})
    tools, prompt = _confined_run_tools(tmp_path, monkeypatch, skills_allow=[])

    assert "## Skills" not in prompt
    refused = asyncio.run(tools["read_skill"].execute(skill_id="local/invoice-audit"))
    assert refused.startswith("Error") and "allow-list" in refused


def test_a_named_skill_allow_list_reads_its_own_skills_only(tmp_path: Path, monkeypatch) -> None:
    """A row or a node that names its skills narrows the tool as it narrows the
    menu: the named one reads, a builtin outside the list does not."""
    _point_live_config(tmp_path, monkeypatch, {})
    tools, prompt = _confined_run_tools(tmp_path, monkeypatch, skills_allow=["invoice-audit"])

    assert "invoice-audit" in prompt
    allowed = asyncio.run(tools["read_skill"].execute(skill_id="local/invoice-audit"))
    assert "### Skill: invoice-audit" in allowed
    refused = asyncio.run(tools["read_skill"].execute(skill_id="local/weather"))
    assert refused.startswith("Error") and "allow-list" in refused


def test_subagent_sandbox_mounts_agent_home_when_the_session_dir_does_not_cover_it(tmp_path: Path) -> None:
    """The common shape: a project directory somewhere else on disk."""
    home = tmp_path / "home"
    session = tmp_path / "my-repo"
    manager = SubagentManager(provider=_StubProvider(), workspace=home)

    assert manager._home_volume(session) == ((str(home), "/agent-home", "rw"),)


def test_subagent_sandbox_skips_the_home_volume_when_the_session_dir_is_above_home(tmp_path: Path) -> None:
    """``cd ~ && raven tui`` with agent home at ``~/.raven/workspace``: the
    session mount already contains agent home, so a second volume would only
    shadow it."""
    home = tmp_path / ".raven" / "workspace"
    home.mkdir(parents=True)
    manager = SubagentManager(provider=_StubProvider(), workspace=home)

    assert manager._home_volume(tmp_path) == ()
