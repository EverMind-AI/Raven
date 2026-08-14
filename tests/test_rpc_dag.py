"""Tests for ``dag.get`` / ``dag.node`` RPC handlers.

The live ``dag.*`` progress events are not replayed anywhere, so these two are
what a TUI that missed them uses instead: ``dag.get`` to rebuild (or repair) a
graph off the run dir, ``dag.node`` to pull one node's rendered prompt and its
full output, which the graph itself never carries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.subagent import instances as instances_mod
from raven.rpc.errors import RpcError
from raven.rpc.methods.dag import dag_get, dag_node

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
        return {"run_id": run_id, "node": node_id, "prompt": "rendered prompt", "output": "node output"}


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
    assert result["run"]["summary"] == {"total": 2, "completed": 1, "failed": 0, "skipped": 0}


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


async def test_dag_node_without_an_agent_loop_is_a_typed_rpc_error() -> None:
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
