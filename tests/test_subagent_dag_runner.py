"""DAG runner + native run_subagent_dag tool (req4/P3)."""

from __future__ import annotations

import asyncio
import posixpath
from pathlib import Path

import pytest

from raven.agent.subagent import instances as instances_mod
from raven.agent.subagent.backends import format_agent_listing, third_party_agent_meta
from raven.agent.subagent_dag import parse_dag_spec
from raven.agent.subagent_dag.runner import run_dag
from raven.agent.subagent_dag.tool import _NODE_SCHEMA, GUIDE_SKILL_ID, SubAgentDagTool
from raven.agent.tools.base import ToolResult
from raven.config.schema import ThirdPartyCliSubagentConfig


@pytest.fixture(autouse=True)
def _isolated_instance_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module must never touch the real user registry file.

    Without this, any test whose DAG run carries a `session_key` (directly, or
    via `SubAgentDagTool.set_context`) resolves `get_registry()` to the
    process-wide singleton, i.e. the real `~/.raven/subagent_instances.json` --
    accumulating one junk row per node per run across every test run, forever
    (each run mints a fresh run_id, so nothing ever overwrites an old row).
    """
    monkeypatch.setattr(
        instances_mod, "_registry", instances_mod.InstanceRegistry(path=tmp_path / "_autouse_inst.json")
    )


class _InMemBackend:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def join_path(self, *parts: str) -> str:
        return posixpath.join(*parts)

    def abspath(self, path: str, cwd: str | None = None) -> str:
        return path if path.startswith("/") else posixpath.join(cwd or "/", path)

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def read_file(self, path: str) -> bytes:
        return self.files[path]

    async def file_exists(self, path: str) -> bool:
        return path in self.files


class _FakeExec:
    """A SubagentBackend that echoes its task, tagged by node id."""

    def __init__(self, fail_ids: set[str] | None = None) -> None:
        self.fail_ids = fail_ids or set()
        self.calls: list[dict] = []

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace,
        executor,
        session_key: str | None = None,
        instance: str | None = None,
    ) -> str:
        self.calls.append({"task_id": task_id, "session_key": session_key, "instance": instance})
        if task_id in self.fail_ids:
            raise RuntimeError(f"boom {task_id}")
        return f"OUT[{task_id}]:{task}"


# --- runner --------------------------------------------------------------


async def test_run_dag_runs_a_sub_agent_whose_name_has_a_space() -> None:
    # The roster key travels from the node spec through the capability check,
    # the backend lookup and the status records. A name `spawn` accepts must
    # work here too, so the whole path is exercised rather than only parsing.
    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "General Audit", "prompt_template": "hello"},
                {
                    "id": "b",
                    "subagent": "General Audit",
                    "prompt_template": "{{ a.output }}",
                    "depends_on": ["a"],
                },
            ]
        }
    )
    result = await run_dag(
        spec,
        subagents={"General Audit": _FakeExec()},
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
    )
    assert result.summary == {"total": 2, "completed": 2, "failed": 0, "skipped": 0}
    # The name survives into the per-node records the UI and the resume path read.
    assert {f["subagent"] for f in result.files} == {"General Audit"}


async def test_run_dag_passes_output_downstream_and_emits_progress() -> None:
    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hello"},
                {"id": "b", "subagent": "x", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        }
    )
    events: list[tuple[str, dict]] = []

    async def pub(name, value):
        events.append((name, value))

    result = await run_dag(
        spec,
        subagents={"x": _FakeExec()},
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        progress_publisher=pub,
    )

    assert result.summary == {"total": 2, "completed": 2, "failed": 0, "skipped": 0}
    # b is the only sink -> terminal output; it received a's output through a file.
    assert len(result.terminal_outputs) == 1
    term = result.terminal_outputs[0]
    assert term["node"] == "b"
    assert "OUT[a]:hello" in term["text"]
    # progress: one run_started + node updates incl. b completed
    names = [n for n, _ in events]
    assert names.count("dag_run_started") == 1
    assert any(v["node"] == "b" and v["status"] == "completed" for n, v in events if n == "dag_node_updated")


async def test_run_dag_threads_session_key_and_node_instance_to_backend() -> None:
    # Nodes sharing an `instance` handle must resume the same stateful CLI
    # session; that only works if run_dag forwards both the DAG's session_key
    # and each node's own instance down to backend.run.
    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hi", "instance": "refactor-auth"},
                {"id": "b", "subagent": "x", "prompt_template": "hi again"},
            ]
        }
    )
    backend = _FakeExec()
    await run_dag(
        spec,
        subagents={"x": backend},
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        session_key="web:sess1",
    )
    calls_by_id = {c["task_id"]: c for c in backend.calls}
    assert calls_by_id["a"]["session_key"] == "web:sess1"
    assert calls_by_id["a"]["instance"] == "refactor-auth"
    assert calls_by_id["b"]["session_key"] == "web:sess1"
    assert calls_by_id["b"]["instance"] is None


async def test_run_dag_failure_cascades_to_skip() -> None:
    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hi"},
                {"id": "b", "subagent": "x", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        }
    )
    result = await run_dag(
        spec,
        subagents={"x": _FakeExec(fail_ids={"a"})},
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
    )
    assert result.summary["failed"] == 1
    assert result.summary["skipped"] == 1  # b skipped because a failed
    assert result.terminal_outputs == []


async def test_run_dag_writes_node_status_transitions_to_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    seen: list[tuple[str, str]] = []
    original_upsert = reg.upsert_dag_node

    async def recording_upsert(session_key: str, run_id: str, node_id: str, agent: str, status: str) -> None:
        seen.append((node_id, status))
        await original_upsert(session_key, run_id, node_id, agent, status)

    monkeypatch.setattr(reg, "upsert_dag_node", recording_upsert)
    monkeypatch.setattr(instances_mod, "_registry", reg)

    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hello"},
                {"id": "b", "subagent": "x", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        }
    )
    result = await run_dag(
        spec,
        subagents={"x": _FakeExec()},
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        session_key="web:sess1",
    )

    assert result.summary == {"total": 2, "completed": 2, "failed": 0, "skipped": 0}
    assert any(status == "running" for _, status in seen)
    final_status = {r["nodeId"]: r["status"] for r in reg.list_instances("web:sess1")}
    assert final_status == {"a": "completed", "b": "completed"}


async def test_run_dag_cancel_skips_unfinished_and_reaps_in_flight_task(tmp_path: Path) -> None:
    # a and b are both independent (root) nodes so they run concurrently in the
    # same round; c depends on b, so cascade-skip is also covered. b blocks
    # until cancelled so the test can set `cancel` while it is genuinely in
    # flight, not merely pending. (An earlier revision of this test also set
    # max_concurrency=1, intending to prove a is only able to run because b's
    # semaphore slot was freed on cancellation -- but _run_ready_groups cancels
    # every not-done task in the round synchronously, in one batch, before
    # awaiting any of them, so a's cancellation is always delivered before b's
    # own unwind ever runs; a never gets the freed slot, deterministically
    # (verified 30/30 trials), not just occasionally. Demonstrating that claim
    # would require changing the cancellation scheduling itself, which is out
    # of scope here, so it was dropped -- the reaped/no-leaked-task assertions
    # below already cover the Critical-1 class of defect this test exists for.)
    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hi-a"},
                {"id": "b", "subagent": "x", "prompt_template": "hi-b"},
                {"id": "c", "subagent": "x", "prompt_template": "{{ b.output }}", "depends_on": ["b"]},
            ]
        }
    )
    b_running = asyncio.Event()
    reaped: list[str] = []

    class _BlockingExec:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None) -> str:
            if task_id == "b":
                b_running.set()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    # Proves cancellation actually reaches the node's own work
                    # (the "child process"), not just its wrapper task.
                    reaped.append(task_id)
                    raise
                return "should never complete"
            return f"OUT[{task_id}]"

    cancel = asyncio.Event()

    async def _cancel_once_b_is_running() -> None:
        await b_running.wait()
        cancel.set()

    tasks_before = set(asyncio.all_tasks())
    canceller = asyncio.create_task(_cancel_once_b_is_running())
    try:
        result = await asyncio.wait_for(
            run_dag(
                spec,
                subagents={"x": _BlockingExec()},
                backend=_InMemBackend(),
                workdir="/w",
                run_root="/hist/mas_dag",
                cancel=cancel,
            ),
            timeout=5,
        )
    finally:
        # If run_dag raised (e.g. RED: no `cancel` kwarg yet) before ever
        # invoking the "b" node, b_running is never set and this waiter would
        # hang forever; cancel it unconditionally rather than awaiting it.
        canceller.cancel()
        try:
            await canceller
        except asyncio.CancelledError:
            pass

    by_node = {f["node"]: f["status"] for f in result.files}
    assert by_node["a"] == "completed"
    assert by_node["b"] == "skipped"
    assert by_node["c"] == "skipped"  # dependent of the cancelled node
    assert result.summary == {"total": 3, "completed": 1, "failed": 0, "skipped": 2}

    assert reaped == ["b"]  # CancelledError was actually raised into the node

    # No _run_group task -- the wrapper around each node/instance-group's
    # coroutine -- is left running past run_dag's return. A leaky
    # _run_ready_groups that breaks out of the cancel race without cancelling
    # and awaiting every task leaves exactly this behind (see
    # probe_test_sensitivity.py).
    leftover_new_tasks = set(asyncio.all_tasks()) - tasks_before - {asyncio.current_task()}
    leaked_run_groups = [t for t in leftover_new_tasks if t.get_coro().__qualname__ == "_run_group"]
    assert leaked_run_groups == []


async def test_run_dag_writes_skipped_status_to_registry_with_session_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No test combined a session_key with a node that ends up "skipped" (as
    # opposed to "completed"): the cascade-skip path writes to the registry
    # from a different call site (the scheduling loop's skip-publish block,
    # not _run_node), and that site was uncovered.
    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hi"},
                {"id": "b", "subagent": "x", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        }
    )
    result = await run_dag(
        spec,
        subagents={"x": _FakeExec(fail_ids={"a"})},
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        session_key="web:sess1",
    )

    assert result.summary == {"total": 2, "completed": 0, "failed": 1, "skipped": 1}
    rows = {r["nodeId"]: r["status"] for r in reg.list_instances("web:sess1")}
    assert rows == {"a": "failed", "b": "skipped"}


async def test_run_dag_without_session_key_writes_nothing_to_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    calls: list[tuple] = []
    original_upsert = reg.upsert_dag_node

    async def recording_upsert(*args, **kwargs):
        calls.append((args, kwargs))
        await original_upsert(*args, **kwargs)

    monkeypatch.setattr(reg, "upsert_dag_node", recording_upsert)
    monkeypatch.setattr(instances_mod, "_registry", reg)

    spec = parse_dag_spec({"nodes": [{"id": "a", "subagent": "x", "prompt_template": "hi"}]})
    result = await run_dag(
        spec, subagents={"x": _FakeExec()}, backend=_InMemBackend(), workdir="/w", run_root="/hist/mas_dag"
    )

    assert result.summary["completed"] == 1
    assert calls == []


# --- native tool end-to-end (real `cat` CLI backend) ---------------------


async def test_run_subagent_dag_tool_end_to_end(tmp_path: Path) -> None:
    tool = SubAgentDagTool(
        workspace=tmp_path,
        third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    assert tool.name == "run_subagent_dag"
    assert "echo" in tool.description

    out = await tool.execute(
        nodes=[
            {"id": "a", "subagent": "echo", "prompt_template": "hello world"},
            {"id": "b", "subagent": "echo", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
        ]
    )
    assert "2 completed" in out.model_text
    assert "hello world" in out.model_text  # a's output flowed to b (the sink) and back


async def test_run_subagent_dag_tool_dispatches_to_a_name_with_a_space(tmp_path: Path) -> None:
    # The whole user-facing path for a config the web UI already allows: the
    # roster advertises "General Audit", so a node naming it must reach it.
    # Previously the node was refused by the name charset before anything ran.
    tool = SubAgentDagTool(
        workspace=tmp_path,
        third_party_subagents=[ThirdPartyCliSubagentConfig(name="General Audit", command="cat")],
    )
    assert "General Audit" in tool.description

    out = await tool.execute(nodes=[{"id": "a", "subagent": "General Audit", "prompt_template": "hello world"}])
    assert "1 completed" in out.model_text
    assert "hello world" in out.model_text


class TestSubagentRoster:
    """A node picks its `subagent` from this listing, so it needs at least what
    `spawn` shows — including which agents are stateful and which can read local
    files, since those are what the node-level `instance` field and the choice
    between path and content placeholders key off."""

    def test_capability_tags_are_rendered_for_every_agent(self, tmp_path: Path) -> None:
        """Both tags always render, positive or negative: the model has to
        confirm a capability before relying on it, and a missing tag reads the
        same as a roster that never mentioned it."""
        desc = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[
                ThirdPartyCliSubagentConfig(
                    name="Coder",
                    command="claude -p {prompt} --session-id {agent_id}",
                    resume_command="claude -p {prompt} --resume {agent_id}",
                ),
                ThirdPartyCliSubagentConfig(name="Boxed", command="cat", reads_local_files=False),
            ],
        ).description

        assert "Coder [stateful, local-files]" in desc
        assert "Boxed [stateless, no-local-files]" in desc

    def test_the_tags_are_explained_once_in_the_field_that_they_gate(self, tmp_path: Path) -> None:
        """Tags the model cannot interpret are just noise, and the pre-check would
        then reject graphs over a rule it was never told. The field descriptions
        are that explanation -- they ride in the same payload as the roster (both
        `to_schema()` and a tool_search hit carry description + parameters), so a
        second copy in the tool description is prose the model pays for twice."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        )
        node = tool.parameters["properties"]["nodes"]["items"]["properties"]

        assert "[stateful]" in node["instance"]["description"]
        assert "[no-local-files]" in node["prompt_template"]["description"]
        # The roster still shows each agent's tags; the rules are not restated.
        assert "[stateless, local-files]" in tool.description
        assert "[no-local-files]" not in tool.description

    def test_descriptions_are_surfaced_not_just_names(self, tmp_path: Path) -> None:
        desc = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[
                ThirdPartyCliSubagentConfig(
                    name="Coder",
                    description="Handles coding tasks.",
                    command="cat",
                ),
                ThirdPartyCliSubagentConfig(name="Bare", command="cat"),
            ],
        ).description

        assert "Coder [stateless, local-files] (Handles coding tasks.)" in desc
        assert "Bare [stateless, local-files]" in desc
        assert "Bare [stateless, local-files] (" not in desc  # no description -> no empty parens

    def test_roster_matches_spawns_rendering(self, tmp_path: Path) -> None:
        """The two tools must describe the same agent the same way."""
        cfgs = [
            ThirdPartyCliSubagentConfig(name="Coder", description="Codes things.", command="cat"),
            ThirdPartyCliSubagentConfig(name="Writer", description="Writes things.", command="cat"),
        ]
        expected = format_agent_listing([third_party_agent_meta(c) for c in cfgs])
        desc = SubAgentDagTool(workspace=tmp_path, third_party_subagents=cfgs).description

        assert expected in desc

    def test_a_backend_that_fails_to_build_is_not_advertised(self, tmp_path: Path) -> None:
        """The roster is captured in the same loop as the executors, so it can
        never list an agent a node would then fail to dispatch to."""

        class _Bad:
            name = "Broken"
            kind = "nope"
            description = "should never be advertised"

        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[_Bad(), ThirdPartyCliSubagentConfig(name="Good", command="cat")],
        )

        assert "Broken" not in tool.description
        assert "Good" in tool.description
        assert sorted(tool._subagents) == ["Good"]

    def test_empty_config_says_none_configured(self, tmp_path: Path) -> None:
        desc = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[]).description
        assert "(none configured)" in desc


class TestSubagentEnum:
    """`run_dag` rejects the whole graph over one unknown name, so a single
    typo costs the entire call. Constrain it in the schema instead."""

    def _subagent_field(self, tool: SubAgentDagTool) -> dict:
        return tool.parameters["properties"]["nodes"]["items"]["properties"]["subagent"]

    def test_enum_lists_the_configured_roster(self, tmp_path: Path) -> None:
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[
                ThirdPartyCliSubagentConfig(name="Writer", command="cat"),
                ThirdPartyCliSubagentConfig(name="Coder", command="cat"),
            ],
        )
        assert self._subagent_field(tool)["enum"] == ["Coder", "Writer"]

    def test_empty_roster_omits_the_enum_entirely(self, tmp_path: Path) -> None:
        """`enum: []` matches nothing while `subagent` stays required, which
        some providers reject as an unsatisfiable tool schema. Reachable at
        runtime: a hot-apply can clear the roster of a registered tool."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="Coder", command="cat")],
        )
        tool.set_third_party_subagents([])
        assert "enum" not in self._subagent_field(tool)

    def test_schema_is_rebuilt_and_never_mutates_the_module_constant(self, tmp_path: Path) -> None:
        """The constant is shared by every instance; annotating it in place
        would leak one tool's roster into another's schema."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="Coder", command="cat")],
        )
        tool.parameters  # noqa: B018 - the property is what would mutate

        assert "enum" not in _NODE_SCHEMA["properties"]["subagent"]
        fresh = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[])
        assert "enum" not in self._subagent_field(fresh)

    def test_enum_follows_a_hot_applied_roster(self, tmp_path: Path) -> None:
        tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[])
        tool.set_third_party_subagents([ThirdPartyCliSubagentConfig(name="Late", command="cat")])
        assert self._subagent_field(tool)["enum"] == ["Late"]


class TestGuideSkillPointer:
    """The node-wiring rules live in the skill, not in this description, so the
    description has to send the agent there before it guesses a graph shape."""

    def test_description_requires_reading_the_guide_first(self, tmp_path: Path) -> None:
        desc = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        ).description

        assert "REQUIRED FIRST STEP" in desc
        assert f'read_skill("{GUIDE_SKILL_ID}")' in desc
        # Still carries the runtime-only facts the skill body cannot know.
        assert "echo" in desc

    def test_pointer_is_dropped_when_the_guide_is_not_installed(self, tmp_path: Path) -> None:
        """Better no instruction than one that sends the agent after a skill
        that cannot resolve — that costs a wasted turn on every DAG call."""
        desc = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            guide_skill_id=None,
        ).description

        assert "REQUIRED FIRST STEP" not in desc
        assert "read_skill" not in desc
        assert "echo" in desc

    def test_loop_drops_the_pointer_only_on_a_definite_miss(self) -> None:
        """A registry that says "absent" suppresses the pointer; one that is
        missing or throwing keeps it — the shipped skill is there by default,
        so losing the instruction is the worse of the two failures."""
        from raven.agent.loop.main import AgentLoop

        class _Reg:
            def __init__(self, found):
                self._found = found

            def get(self, name, source=None):
                if self._found is Ellipsis:
                    raise RuntimeError("registry down")
                return object() if self._found else None

        def _resolve(registry) -> str | None:
            loop = object.__new__(AgentLoop)
            loop.context = type("C", (), {"skills": type("S", (), {"registry": registry})()})()
            return AgentLoop._dag_guide_skill_id(loop)

        assert _resolve(_Reg(True)) == GUIDE_SKILL_ID
        assert _resolve(_Reg(False)) is None
        assert _resolve(_Reg(Ellipsis)) == GUIDE_SKILL_ID  # raising
        assert _resolve(None) == GUIDE_SKILL_ID  # no registry wired

    def test_guide_id_resolves_against_the_shipped_registry(self, tmp_path: Path) -> None:
        """Pins the two halves together: the id the tool prints must be the id
        ``read_skill`` can actually resolve, or the instruction is a dead end."""
        from raven.agent.tools.skill_hub import _lookup_on_disk, _split_qualified_id
        from raven.memory_engine.skill_forge import LocalSkillCatalog

        registry = LocalSkillCatalog(tmp_path, start_watcher=False).registry
        source, native = _split_qualified_id(GUIDE_SKILL_ID)
        assert _lookup_on_disk(registry, source, native) is not None


async def test_run_subagent_dag_tool_unknown_subagent(tmp_path: Path) -> None:
    tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[])
    out = await tool.execute(nodes=[{"id": "a", "subagent": "nope", "prompt_template": "x"}])
    assert out.startswith("Error")


async def test_run_subagent_dag_tool_fans_progress_to_sink(tmp_path: Path) -> None:
    tool = SubAgentDagTool(
        workspace=tmp_path,
        third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    # The loop delivers the turn's conversation via set_context.
    tool.set_context("web", "default", "web:sess1")
    events: list[tuple[str, str, dict]] = []

    async def sink(conversation, name, payload):
        events.append((conversation, name, payload))

    tool.set_progress_sink(sink)
    await tool.execute(
        nodes=[
            {"id": "a", "subagent": "echo", "prompt_template": "hi"},
            {"id": "b", "subagent": "echo", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
        ]
    )

    assert all(conv == "web:sess1" for conv, _, _ in events)
    names = [n for _, n, _ in events]
    assert names[0] == "dag_run_started"
    assert names[-1] == "dag_run_completed"
    assert any(
        n == "dag_node_updated" and p.get("node") == "b" and p.get("status") == "completed" for _, n, p in events
    )
    # the completed event carries the authoritative manifest
    completed = [p for _, n, p in events if n == "dag_run_completed"][0]
    assert "manifest" in completed and "files" in completed["manifest"]


class TestCallAndResultLabels:
    """What a transcript shows for a DAG call when it cannot show the graph.

    The row's label is built before the run, from the raw arguments; the result
    line is what survives the loop's 200-char preview clamp. Neither may
    degrade to a node id picked out of the arguments blob at random.
    """

    @staticmethod
    def _tool(tmp_path: Path) -> SubAgentDagTool:
        return SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        )

    def test_display_call_counts_nodes_and_names_the_first_few(self, tmp_path: Path) -> None:
        label = self._tool(tmp_path).display_call(
            {
                "nodes": [
                    {"id": "fetch", "subagent": "echo", "prompt_template": "x"},
                    {"id": "parse", "subagent": "echo", "prompt_template": "x"},
                    {"id": "report", "subagent": "echo", "prompt_template": "x"},
                ]
            }
        )
        assert label == "3 nodes: fetch, parse, report"

    def test_display_call_elides_a_long_roster(self, tmp_path: Path) -> None:
        label = self._tool(tmp_path).display_call(
            {"nodes": [{"id": f"n{i}", "subagent": "echo", "prompt_template": "x"} for i in range(6)]}
        )
        assert label == "6 nodes: n0, n1, n2 (+3 more)"

    def test_display_call_declines_malformed_arguments(self, tmp_path: Path) -> None:
        """display_call runs before validation, so it must never raise -- None
        hands the row back to the UI's generic argument preview."""
        tool = self._tool(tmp_path)
        assert tool.display_call({}) is None
        assert tool.display_call({"nodes": "not-a-list"}) is None
        assert tool.display_call({"nodes": []}) is None

    async def test_result_preview_names_the_run_and_its_tally(self, tmp_path: Path) -> None:
        out = await self._tool(tmp_path).execute(
            nodes=[
                {"id": "a", "subagent": "echo", "prompt_template": "hi"},
                {"id": "b", "subagent": "echo", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        )
        preview = getattr(out, "display_text", None)
        assert preview is not None, "the result must carry a display string for the transcript"
        # The loop clamps the preview at 200 chars. The tally has to be inside
        # that window whatever the workspace path length is, so it leads.
        assert "2/2 completed" in preview[:200]
        assert "mas_dag" in preview

    async def test_result_preview_names_the_failed_nodes(self, tmp_path: Path) -> None:
        """A failure the user cannot see the id of is a failure they cannot act
        on -- and the full per-node list is exactly what the clamp cuts off."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[
                ThirdPartyCliSubagentConfig(name="echo", command="cat"),
                ThirdPartyCliSubagentConfig(name="broken", command="false"),
            ],
        )
        out = await tool.execute(
            nodes=[
                {"id": "boom", "subagent": "broken", "prompt_template": "x"},
                {"id": "after", "subagent": "echo", "prompt_template": "{{ boom.output }}", "depends_on": ["boom"]},
            ]
        )
        assert out.display_text is not None
        assert "1 failed (boom)" in out.display_text
        assert "1 skipped" in out.display_text


async def test_run_subagent_dag_tool_stamps_tool_call_id_on_every_event(tmp_path: Path) -> None:
    """A consumer that renders the graph under the tool row it belongs to needs
    to know which call each event is for -- keying on run_id alone forces it to
    guess when a turn issues more than one DAG call."""
    tool = SubAgentDagTool(
        workspace=tmp_path,
        third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    tool.set_context("tui", "default", "tui:sess1")
    tool.set_tool_call_id("call-42")
    events: list[tuple[str, dict]] = []

    async def sink(conversation, name, payload):
        events.append((name, payload))

    tool.set_progress_sink(sink)
    await tool.execute(nodes=[{"id": "a", "subagent": "echo", "prompt_template": "hi"}])

    assert events, "expected at least one progress event"
    assert all(p.get("tool_call_id") == "call-42" for _, p in events)


async def test_run_subagent_dag_tool_omits_tool_call_id_when_host_sets_none(tmp_path: Path) -> None:
    """Hosts that never call set_tool_call_id (IM channels, tests) must not get
    a null key smuggled into the payload -- the web consumer folds the payload
    verbatim into its projection."""
    tool = SubAgentDagTool(
        workspace=tmp_path,
        third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    tool.set_context("im", "default", "im:sess1")
    events: list[dict] = []

    async def sink(conversation, name, payload):
        events.append(payload)

    tool.set_progress_sink(sink)
    await tool.execute(nodes=[{"id": "a", "subagent": "echo", "prompt_template": "hi"}])

    assert events
    assert all("tool_call_id" not in p for p in events)


class TestCapabilityGate:
    """The roster's tags are a promise about what a node may ask of an agent.
    Enforced here, before dispatch: both mistakes otherwise *run* and come back
    with output that silently pretends the capability was there."""

    @staticmethod
    def _tool(tmp_path: Path) -> SubAgentDagTool:
        return SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[
                ThirdPartyCliSubagentConfig(name="stateless_local", command="cat"),
                ThirdPartyCliSubagentConfig(name="boxed", command="cat", reads_local_files=False),
                ThirdPartyCliSubagentConfig(
                    name="resumable",
                    command="cat {agent_id}",
                    resume_command="cat {agent_id}",
                ),
            ],
        )

    async def test_reused_handle_on_a_stateless_agent_runs_no_node(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        out = await tool.execute(
            nodes=[
                {"id": "draft", "subagent": "stateless_local", "prompt_template": "write", "instance": "author"},
                {"id": "revise", "subagent": "stateless_local", "prompt_template": "revise", "instance": "author"},
            ]
        )

        assert out.startswith("Error: invalid DAG")
        assert "No sub-agent was run." in out
        assert f'read_skill("{GUIDE_SKILL_ID}")' in out
        # Nothing was dispatched, so no run directory exists to read back.
        assert not (tmp_path / "sessions").exists()

    async def test_path_placeholder_to_a_boxed_agent_runs_no_node(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        out = await tool.execute(
            nodes=[
                {"id": "a", "subagent": "stateless_local", "prompt_template": "upstream"},
                {
                    "id": "b",
                    "subagent": "boxed",
                    "prompt_template": "read {{ a.output_path }}",
                    "depends_on": ["a"],
                },
            ]
        )

        assert out.startswith("Error: invalid DAG")
        assert "{{ a.output }}" in out  # the fix, not just the complaint
        assert "No sub-agent was run." in out
        assert not (tmp_path / "sessions").exists()

    async def test_a_graph_that_respects_the_tags_still_runs(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        out = await tool.execute(
            nodes=[
                {"id": "a", "subagent": "stateless_local", "prompt_template": "hello world"},
                {"id": "b", "subagent": "boxed", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        )

        assert "2 completed" in out.model_text
        assert "hello world" in out.model_text

    async def test_reused_handle_is_allowed_on_a_stateful_agent(self, tmp_path: Path) -> None:
        """The gate must not reject the shape the guide's own pipeline example
        teaches — it only fires when the agent cannot honour the handle."""
        tool = self._tool(tmp_path)
        out = await tool.execute(
            nodes=[
                {"id": "draft", "subagent": "resumable", "prompt_template": "write", "instance": "author"},
                {
                    "id": "revise",
                    "subagent": "resumable",
                    "prompt_template": "{{ draft.output }}",
                    "depends_on": ["draft"],
                    "instance": "author",
                },
            ]
        )

        # A rejection is a plain error string; a graph that ran comes back as a
        # ToolResult carrying the transcript label alongside the model text.
        assert isinstance(out, ToolResult), f"gate rejected the graph: {out}"

    async def test_hot_applied_config_moves_the_gate_with_the_roster(self, tmp_path: Path) -> None:
        """The capability map is rebuilt in the same pass as the roster, so a
        webui config change can never leave the two describing different agents."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="agent", command="cat")],
        )
        tool.set_third_party_subagents(
            [ThirdPartyCliSubagentConfig(name="agent", command="cat", reads_local_files=False)]
        )

        assert "agent [stateless, no-local-files]" in tool.description
        out = await tool.execute(
            nodes=[
                {"id": "a", "subagent": "agent", "prompt_template": "upstream"},
                {"id": "b", "subagent": "agent", "prompt_template": "{{ a.output_path }}", "depends_on": ["a"]},
            ]
        )
        assert out.startswith("Error: invalid DAG")


class TestValidationErrorGuidesRetry:
    """A rejected graph is the one moment we know the model got the shape wrong,
    and the description it read once is long past. The result has to carry the
    way back itself."""

    async def test_structural_failure_points_at_the_guide(self, tmp_path: Path) -> None:
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        )
        out = await tool.execute(
            nodes=[
                {"id": "a", "subagent": "echo", "prompt_template": "{{ b.output }}", "depends_on": ["b"]},
                {"id": "b", "subagent": "echo", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        )

        assert "cycle" in out
        assert f'read_skill("{GUIDE_SKILL_ID}")' in out
        assert "retry" in out
        assert not (tmp_path / "sessions").exists()

    async def test_no_guide_installed_means_no_dead_end_advice(self, tmp_path: Path) -> None:
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            guide_skill_id=None,
        )
        out = await tool.execute(nodes=[{"id": "a", "subagent": "nope", "prompt_template": "hi"}])

        assert out.startswith("Error: invalid DAG")
        assert "read_skill" not in out

    async def test_one_sentence_boundary_between_detail_and_advice(self, tmp_path: Path) -> None:
        """Validation messages are written without a trailing period, so the
        renderer supplies one — else the detail runs into the next sentence."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        )
        out = await tool.execute(
            nodes=[
                {"id": "a", "subagent": "echo", "prompt_template": "hi"},
                {"id": "a", "subagent": "echo", "prompt_template": "hi"},
            ]
        )

        assert "node ids must be unique. No sub-agent was run." in out
