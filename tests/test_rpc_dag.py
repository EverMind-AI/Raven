"""Tests for ``dag.get`` / ``dag.node`` RPC handlers.

The live ``dag.*`` progress events are not replayed anywhere, so these two are
what a TUI that missed them uses instead: ``dag.get`` to rebuild (or repair) a
graph off the run dir, ``dag.node`` to pull one node's rendered prompt and its
full output, which the graph itself never carries.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.subagent import instances as instances_mod
from raven.rpc.errors import RpcError
from raven.rpc.methods.dag import dag_get, dag_node
from raven.rpc.models import METHOD_MODELS

RUN_ID = "20260730T060242Z-6b0b89a3"


class _FakeDagTool:
    def __init__(self, *, finalized: bool, live: bool = True) -> None:
        self._finalized = finalized
        self._live = live
        self.node_calls: list[tuple[str, str, int, str | None]] = []
        self.run_calls: list[tuple[str, str | None]] = []

    def active_run_ids(self) -> list[str]:
        return [RUN_ID] if self._live else []

    async def read_run(self, run_id: str, session_key: str | None = None) -> dict:
        self.run_calls.append((run_id, session_key))
        status = "completed" if self._finalized else "pending"
        return {
            "run_id": run_id,
            "dir": f"/w/.ravenx_dag/{run_id}",
            "finalized": self._finalized,
            "terminal_outputs": [],
            "files": [
                {
                    "node": nid,
                    "subagent": "claude_code",
                    "depends_on": [],
                    "instance": None,
                    "status": status,
                    "started_at": None,
                    "ended_at": None,
                    "prompt_file": None,
                    "output_file": None,
                    "error": None,
                    "prompt_template": f"do {nid}",
                }
                for nid in ("node-a", "node-b")
            ],
            "summary": {"total": 2, "completed": 2 if self._finalized else 0, "failed": 0, "skipped": 0},
        }

    async def read_node(
        self, run_id: str, node_id: str, *, max_output_chars: int = 20000, session_key: str | None = None
    ) -> dict:
        self.node_calls.append((run_id, node_id, max_output_chars, session_key))
        # The two counters are in every real answer -- the reader computes them
        # unconditionally -- so a stub without them lets a handler response pass
        # a model check it would fail in production.
        return {
            "run_id": run_id,
            "node": node_id,
            "prompt": "rendered prompt",
            "output": "node output",
            "output_chars": len("node output"),
            "output_truncated": False,
        }


class _Tools:
    def __init__(self, tool: object | None) -> None:
        self._tool = tool

    def get(self, name: str) -> object | None:
        return self._tool if name == "run_subagent_dag" else None


class _Agent:
    def __init__(self, tool: object | None) -> None:
        self.tools = _Tools(tool)


def _factory(tool: object | None):
    return lambda: _Agent(tool)


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instances_mod, "_registry", instances_mod.InstanceRegistry(path=tmp_path / "inst.json"))


async def test_dag_get_returns_a_finalized_run() -> None:
    result = await dag_get({"run_id": RUN_ID}, agent_loop_factory=_factory(_FakeDagTool(finalized=True)))

    assert result["run"]["run_id"] == RUN_ID
    assert {f["status"] for f in result["run"]["files"]} == {"completed"}


async def test_dag_get_overlays_registry_rows_on_an_unfinalized_run() -> None:
    # The whole point of the call: a TUI that reattached mid-run gets the
    # progress it missed, instead of a graph reset to all-pending.
    await instances_mod.get_registry().upsert_dag_node("tui:s1", RUN_ID, "node-a", "claude_code", "completed")

    result = await dag_get(
        {"run_id": RUN_ID, "session_key": "tui:s1"},
        agent_loop_factory=_factory(_FakeDagTool(finalized=False)),
    )

    by_node = {f["node"]: f for f in result["run"]["files"]}
    assert by_node["node-a"]["status"] == "completed"
    assert by_node["node-b"]["status"] == "pending"
    assert result["run"]["summary"] == {"total": 2, "completed": 1, "failed": 0, "skipped": 0, "cancelled": 0}


async def test_dag_get_reports_a_dead_runs_live_looking_node_as_interrupted() -> None:
    await instances_mod.get_registry().upsert_dag_node("tui:s1", RUN_ID, "node-a", "claude_code", "running")

    result = await dag_get(
        {"run_id": RUN_ID, "session_key": "tui:s1"},
        agent_loop_factory=_factory(_FakeDagTool(finalized=False, live=False)),
    )

    by_node = {f["node"]: f for f in result["run"]["files"]}
    assert by_node["node-a"]["status"] == "interrupted"


async def test_dag_node_returns_the_rendered_prompt_and_output() -> None:
    tool = _FakeDagTool(finalized=True)

    result = await dag_node({"run_id": RUN_ID, "node": "node-a"}, agent_loop_factory=_factory(tool))

    assert result["node"]["prompt"] == "rendered prompt"
    assert result["node"]["output"] == "node output"
    assert tool.node_calls == [(RUN_ID, "node-a", 20000, None)]


async def test_dag_node_honours_an_output_cap() -> None:
    tool = _FakeDagTool(finalized=True)

    await dag_node({"run_id": RUN_ID, "node": "node-a", "max_output_chars": 500}, agent_loop_factory=_factory(tool))

    assert tool.node_calls == [(RUN_ID, "node-a", 500, None)]


async def test_dag_node_forwards_the_session_key() -> None:
    """The run dir lives under the *session's* working directory, so the tool
    cannot find it without being told which session asked. This RPC is the one
    that carried no session key at all before."""
    tool = _FakeDagTool(finalized=True)

    await dag_node(
        {"run_id": RUN_ID, "node": "node-a", "session_key": "cli:s1"},
        agent_loop_factory=_factory(tool),
    )

    assert tool.node_calls == [(RUN_ID, "node-a", 20000, "cli:s1")]


async def test_dag_get_without_a_dag_tool_is_a_typed_rpc_error() -> None:
    # The tool is only registered when third-party sub-agents are configured, so
    # "no DAG tool" is a normal state the client must be able to tell apart from
    # a crash.
    with pytest.raises(RpcError):
        await dag_get({"run_id": RUN_ID}, agent_loop_factory=_factory(None))


async def test_dag_node_without_a_tool_or_a_session_cannot_find_the_run() -> None:
    """With no live tool the handler reads the run dir itself, and the only thing
    that says which conversation's dir to read is session_key."""
    with pytest.raises(RpcError):
        await dag_node({"run_id": RUN_ID, "node": "a"}, agent_loop_factory=lambda: None)


class _UnreadableDagTool(_FakeDagTool):
    """The run dir is gone (cleaned up, or the id was never real)."""

    async def read_run(self, run_id: str, session_key: str | None = None) -> dict:
        from raven.agent.subagent_dag._reader import DagReadError

        raise DagReadError(f"no readable DAG run at {run_id}")

    async def read_node(
        self, run_id: str, node_id: str, *, max_output_chars: int = 20000, session_key: str | None = None
    ) -> dict:
        from raven.agent.subagent_dag._reader import DagReadError

        raise DagReadError(f"invalid node id: {node_id!r}")


async def test_dag_get_on_a_missing_run_is_a_typed_rpc_error() -> None:
    # The reader raises its own ValueError subclass. Letting that escape the
    # handler reaches the client as an untyped -32603 with a traceback instead of
    # a message it can show.
    with pytest.raises(RpcError):
        await dag_get({"run_id": RUN_ID}, agent_loop_factory=_factory(_UnreadableDagTool(finalized=False)))


async def test_dag_node_on_a_bad_node_id_is_a_typed_rpc_error() -> None:
    with pytest.raises(RpcError):
        await dag_node(
            {"run_id": RUN_ID, "node": "../etc/passwd"},
            agent_loop_factory=_factory(_UnreadableDagTool(finalized=True)),
        )


# ---------------------------------------------------------------------------
# dag.node with no live tool.
#
# The tool exists only while third-party sub-agents are configured, and a run
# that already happened does not stop having happened when that config changes.
# Without this the graph view went blank after a config edit, and every node
# `subagent.list` offers would have been unopenable.
# ---------------------------------------------------------------------------


@pytest.fixture
def one_run_on_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real run dir, in a real session dir, reached the way the handler reaches it."""
    import json

    from raven.agent.subagent_history import dag_root
    from raven.session.manager import SessionManager

    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"agents": {"defaults": {"workspace": str(ws)}}}), encoding="utf-8")
    import raven.config.loader as loader

    previous = loader._current_config_path
    loader.set_config_path(cfg)

    run = dag_root(SessionManager(ws).session_dir("tui:live")) / RUN_ID
    run.mkdir(parents=True)
    (run / "graph.json").write_text(json.dumps({"nodes": [{"id": "survey", "subagent": "R"}]}), encoding="utf-8")
    (run / "survey.prompt.md").write_text("look at the notes", encoding="utf-8")
    (run / "survey.out.md").write_text("the notes say yes", encoding="utf-8")
    yield run
    loader._current_config_path = previous


async def test_dag_node_reads_the_run_dir_when_no_tool_is_live(one_run_on_disk) -> None:
    answer = await dag_node(
        {"run_id": RUN_ID, "node": "survey", "session_key": "tui:live"},
        agent_loop_factory=_factory(None),
    )

    assert answer["node"]["prompt"] == "look at the notes"
    assert answer["node"]["output"] == "the notes say yes"


async def test_the_fallback_honours_the_output_cap(one_run_on_disk) -> None:
    answer = await dag_node(
        {"run_id": RUN_ID, "node": "survey", "session_key": "tui:live", "max_output_chars": 4},
        agent_loop_factory=_factory(None),
    )

    assert answer["node"]["output"] == "the "
    assert answer["node"]["output_truncated"] is True


async def test_the_fallback_refuses_an_id_that_walks_out_of_the_run_dir(one_run_on_disk) -> None:
    """The ids are minted by raven, but they arrive here off a web request, and
    the fallback joins them into a real local path."""
    with pytest.raises(RpcError):
        await dag_node(
            {"run_id": RUN_ID, "node": "../../../../etc/passwd", "session_key": "tui:live"},
            agent_loop_factory=_factory(None),
        )
    with pytest.raises(RpcError):
        await dag_node(
            {"run_id": "../..", "node": "survey", "session_key": "tui:live"},
            agent_loop_factory=_factory(None),
        )


async def test_a_live_tool_still_wins(one_run_on_disk) -> None:
    """The fallback is for when there is nothing better, not instead of it: only
    the live tool can reach a remote workspace backend."""
    tool = _FakeDagTool(finalized=True)

    answer = await dag_node(
        {"run_id": RUN_ID, "node": "survey", "session_key": "tui:live"},
        agent_loop_factory=_factory(tool),
    )

    assert tool.node_calls, "it read the disk instead of asking the live tool"
    assert answer["node"]["prompt"] != "look at the notes"


# ---------------------------------------------------------------------------
# the reader must accept the ids the minter actually mints
# ---------------------------------------------------------------------------


def test_a_freshly_minted_run_id_passes_the_reader() -> None:
    """`RUN_ID` above is a literal, and every reader test uses it, so nothing
    here ever fed the reader an id from the minter.

    That gap cost a release once: widening the stamp for ordering changed the
    format, `_RUN_ID_RE` still matched the old one, and every graph run minted
    from that commit refused to open -- while old run dirs kept working, so it
    read as "the graph stopped working for new runs only".
    """
    from raven.agent.subagent_dag._reader import _check_run_id
    from raven.agent.subagent_dag._store import make_run_id

    assert _check_run_id(make_run_id())
    # And the literal above, so run dirs written before the widening stay readable.
    assert _check_run_id(RUN_ID)


@pytest.mark.parametrize(
    "bad",
    ["../etc/passwd", "20260730T060242Z-XXXXXXXX", "20260730T06024Z-6b0b89a3", "", "20260730T060242Z-6b0b89a3/.."],
)
def test_the_reader_still_refuses_an_id_that_could_walk_out(bad: str) -> None:
    """The pattern is a containment check, not a formatting nicety -- these ids
    arrive off a web request and are joined into a path."""
    from raven.agent.subagent_dag._reader import DagReadError, _check_run_id

    with pytest.raises(DagReadError):
        _check_run_id(bad)


# ---------------------------------------------------------------------------
# the off-disk fallback must read where the run was written
# ---------------------------------------------------------------------------


async def test_the_fallback_reads_the_directory_the_run_was_written_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`raven serve` builds its manager with a project slug taken from the launch
    directory, and `session_dir()` puts the run under `sessions/<slug>/`. A
    manager built here without that resolves `sessions/<channel>/` -- a
    directory the run was never written to.

    `one_run_on_disk` above cannot see it: it builds `SessionManager(ws)` with
    no slug, which is the only configuration in which the two agree. So this one
    uses a slugged manager, which is what the served page actually has, and the
    node has to come back with its content rather than empty.
    """
    import json

    from raven.agent.subagent_history import dag_root
    from raven.session.manager import SessionManager

    ws = tmp_path / "ws"
    ws.mkdir()
    project = tmp_path / "myproject"
    project.mkdir()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"agents": {"defaults": {"workspace": str(ws)}}}), encoding="utf-8")
    import raven.config.loader as loader

    previous = loader._current_config_path
    loader.set_config_path(cfg)
    try:
        live = SessionManager(ws, project_slug="myproject", project_dir=project)
        run = dag_root(live.session_dir("tui:live")) / RUN_ID
        run.mkdir(parents=True)
        (run / "graph.json").write_text(json.dumps({"nodes": [{"id": "survey", "subagent": "R"}]}), encoding="utf-8")
        (run / "survey.prompt.md").write_text("look at the notes", encoding="utf-8")
        (run / "survey.out.md").write_text("the notes say yes", encoding="utf-8")

        # No dag tool on the loop, so dag.node takes the off-disk path -- but the
        # loop still carries the manager that knows where the run is.
        loop = SimpleNamespace(sessions=live)
        out = await dag_node(
            {"run_id": RUN_ID, "node": "survey", "session_key": "tui:live"},
            agent_loop_factory=lambda: loop,
        )

        assert out["node"]["prompt"] == "look at the notes"
        assert out["node"]["output"] == "the notes say yes"
    finally:
        loader._current_config_path = previous


# ---------------------------------------------------------------------------
# a node's own transcript
#
# A node reached the panel as two strings -- the prompt and the answer -- so it
# drew as two bubbles with nothing in between, while the equivalent spawned call
# drew every tool it called. Worse, nothing of a node reaches disk until it
# ends, so a node being watched mid-run had nothing to show at all.
# ---------------------------------------------------------------------------


class _TranscribedDagTool(_FakeDagTool):
    def __init__(self, transcript: list[dict]) -> None:
        super().__init__(finalized=True)
        self._transcript = transcript

    async def read_node(
        self, run_id: str, node_id: str, *, max_output_chars: int = 20000, session_key: str | None = None
    ) -> dict:
        node = await super().read_node(run_id, node_id, max_output_chars=max_output_chars, session_key=session_key)
        return {**node, "transcript": self._transcript}


async def test_dag_node_draws_what_the_node_did_between_the_two_messages() -> None:
    tool = _TranscribedDagTool(
        [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "read"}}]},
            {"role": "tool", "name": "read", "content": "file body"},
        ]
    )

    answer = await dag_node({"run_id": RUN_ID, "node": "node-a"}, agent_loop_factory=_factory(tool))

    roles = [m["role"] for m in answer["node"]["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    # Against the declared model, not just the shape this test expects: the
    # models forbid extras, so a key added to a handler and not to the contract
    # is a response no typed client can accept -- and schema-vs-model tests
    # cannot see it, because both halves stay self-consistent while the handler
    # walks away from them.
    _, model = METHOD_MODELS["dag.node"]
    model.model_validate(answer)


async def test_a_dag_nodes_stored_call_reaches_the_panel_in_ravens_vocabulary() -> None:
    """One renderer draws a node and a spawned call, and its verb table is keyed
    by raven's names -- so a node whose rows kept the transport's would draw the
    same tool under a different verb from the panel next to it."""
    tool = _TranscribedDagTool(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "ls"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "a.py"},
        ]
    )

    answer = await dag_node({"run_id": RUN_ID, "node": "node-a"}, agent_loop_factory=_factory(tool))

    call = next(m for m in answer["node"]["messages"] if m.get("tool_calls"))
    assert call["tool_calls"][0]["name"] == "exec"
    assert call["tool_calls"][0]["arguments"] == '{"command": "ls"}'


async def test_a_node_in_flight_is_served_from_the_activity_being_collected() -> None:
    """Nothing of a node reaches disk until it ends, so a panel watching one had
    only its prompt to show -- for the whole run."""
    from raven.agent.subagent import activity
    from raven.agent.subagent_dag._store import node_live_key

    tool = _TranscribedDagTool([])
    with activity.collecting(live_key=node_live_key(RUN_ID, "node-a")) as did:
        activity.note_transcript([{"role": "tool", "name": "read", "content": "file body"}])
        answer = await dag_node({"run_id": RUN_ID, "node": "node-a"}, agent_loop_factory=_factory(tool))
    assert did.transcript

    roles = [m["role"] for m in answer["node"]["messages"]]
    assert roles == ["user", "tool", "assistant"]


async def test_a_finished_nodes_own_file_wins_over_a_live_key_left_behind() -> None:
    """Two runs of one graph reuse the node id; only the run dir tells them apart."""
    from raven.agent.subagent import activity
    from raven.agent.subagent_dag._store import node_live_key

    tool = _TranscribedDagTool([{"role": "tool", "name": "on_disk", "content": "x"}])
    with activity.collecting(live_key=node_live_key(RUN_ID, "node-a")):
        activity.note_transcript([{"role": "tool", "name": "in_flight", "content": "y"}])
        answer = await dag_node({"run_id": RUN_ID, "node": "node-a"}, agent_loop_factory=_factory(tool))

    names = [m.get("name") for m in answer["node"]["messages"] if m["role"] == "tool"]
    assert names == ["on_disk"]


async def test_the_fallback_reads_a_nodes_transcript_off_disk(one_run_on_disk) -> None:
    """A node that already ran keeps its account in its own file, so the panel
    shows the same work after a restart as it did while the run was going.
    A malformed line is skipped rather than losing the rest: the file is
    appended to while the run is still writing it."""
    (one_run_on_disk / "survey.transcript.jsonl").write_text(
        '{"role": "tool", "name": "read", "content": "file body"}\nnot json\n',
        encoding="utf-8",
    )

    answer = await dag_node(
        {"run_id": RUN_ID, "node": "survey", "session_key": "tui:live"},
        agent_loop_factory=_factory(None),
    )

    assert [m["role"] for m in answer["node"]["messages"]] == ["user", "tool", "assistant"]


def test_the_dag_event_models_accept_a_cancelled_node() -> None:
    from raven.rpc.models import DagRunCompletedPayload

    payload = DagRunCompletedPayload(
        run_id="r1",
        dir="/w/.raven_dag/r1",
        summary={"total": 2, "completed": 0, "failed": 0, "skipped": 1, "cancelled": 1},
        files=[
            {"node": "a", "status": "cancelled"},
            {"node": "b", "status": "skipped"},
        ],
    )
    assert payload.summary.cancelled == 1
    assert payload.files[0].status == "cancelled"


class _EngineOwnedRun(_Agent):
    """A loop whose *private* graph tool owns the run.

    Which is what a `mode: dag` playbook produces: the registered tool -- the
    only one `_Agent` exposes -- has never heard of the run, because
    `active_run_ids` reads a per-instance cancel map.
    """

    def __init__(self, tool: object | None) -> None:
        super().__init__(tool)

    def active_dag_run_ids(self) -> set[str]:
        return {RUN_ID}


async def test_dag_get_asks_liveness_of_every_graph_tool() -> None:
    """A run the playbook engine dispatched is live, and its nodes say running.

    The registered tool answers "not executing that" for it, and the overlay
    turns every unfinished node into ``interrupted`` -- so a backgrounded
    playbook run, reopened from history, came back as a graph of failures with
    the clock still running on them. Fixed for the instance rows first; this is
    the path that reads a run back, and it took liveness off the tool it was
    handed rather than off the loop.
    """
    await instances_mod.get_registry().upsert_dag_node("tui:s1", RUN_ID, "node-a", "research-raven", "running")

    result = await dag_get(
        {"run_id": RUN_ID, "session_key": "tui:s1"},
        # The tool says the run is not its own; the loop knows better.
        agent_loop_factory=lambda: _EngineOwnedRun(_FakeDagTool(finalized=False, live=False)),
    )

    by_node = {f["node"]: f for f in result["run"]["files"]}
    assert by_node["node-a"]["status"] == "running", "the engine's run is live even so"


async def test_a_loop_that_cannot_answer_still_falls_back_to_the_tool() -> None:
    """The negative half. Without it, "always live" passes the test above."""
    await instances_mod.get_registry().upsert_dag_node("tui:s1", RUN_ID, "node-a", "research-raven", "running")

    result = await dag_get(
        {"run_id": RUN_ID, "session_key": "tui:s1"},
        agent_loop_factory=_factory(_FakeDagTool(finalized=False, live=False)),
    )

    assert {f["node"]: f for f in result["run"]["files"]}["node-a"]["status"] == "interrupted"
