"""``subagent.list`` / ``subagent.context`` over the on-disk spawn record.

Named ``_calls`` rather than ``test_rpc_subagent.py`` on purpose: the plural
``subagents.*`` surface already has ``test_rpc_subagents.py``, and two files one
letter apart is how the wrong one gets edited.

The listing tests drive a **real** ``SubagentManager.spawn`` rather than writing
the record by hand. The reader and the writer live in different modules and were
written for different designs, so a fixture shaped like the record would only
assert my idea of it: the directory names, the status vocabulary and the epoch
stamps all come from ``raven/agent/subagent/history.py``, and a test that mints
its own cannot notice when they change.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods.subagent import subagent_context, subagent_list

SESSION = "tui:live"


@pytest.fixture
def workspace(tmp_path: Path):
    """A workspace of our own, reached the way the handlers reach it."""
    import raven.config.loader as loader

    previous = loader._current_config_path
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"agents": {"defaults": {"workspace": str(ws)}}}))
    loader.set_config_path(cfg)
    yield ws
    loader._current_config_path = previous


class _Provider:
    def get_default_model(self) -> str:
        return "m"


def _manager(workspace: Path):
    from raven.agent.subagent.manager import SubagentManager

    mgr = SubagentManager(provider=_Provider(), workspace=workspace)
    mgr.set_submit(lambda _req: None)
    return mgr


async def _spawn(workspace: Path, task: str = "count the files", *, label: str | None = "counting", backend=None):
    """One real spawn, awaited to completion, with a stand-in backend."""

    class _Answers:
        async def run(self, task: str, **_kw: Any) -> str:
            return f"answer to {task}"

    mgr = _manager(workspace)
    mgr.registry.set_builtin_builder(lambda _row, _build, _b=backend or _Answers(): _b)
    await mgr.spawn(task, task_summary=label, session_key=SESSION)
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)
    return mgr


class TestListing:
    async def test_a_real_spawn_shows_up_as_a_row(self, workspace: Path) -> None:
        await _spawn(workspace)

        rows = (await subagent_list({"session_id": SESSION}))["items"]

        assert len(rows) == 1
        row = rows[0]
        assert row["label"] == "counting"
        assert row["status"] == "ok"
        assert row["message_count"] == 2
        assert row["started_at"] and row["ended_at"], "a finished call has both, which is what stops the live timer"

    async def test_the_stamps_are_iso_because_that_is_what_a_client_parses(self, workspace: Path) -> None:
        """The record stores epoch millis; ``new Date(ms_as_string)`` is Invalid
        Date, and the panel would show no duration at all."""
        from datetime import datetime

        await _spawn(workspace)

        row = (await subagent_list({"session_id": SESSION}))["items"][0]

        assert datetime.fromisoformat(row["started_at"]).year >= 2024
        assert datetime.fromisoformat(row["ended_at"]) >= datetime.fromisoformat(row["started_at"])

    async def test_another_conversation_s_calls_are_not_here(self, workspace: Path) -> None:
        """Scoped by construction rather than by filter: the record lives inside
        the session's own directory."""
        await _spawn(workspace)

        assert await subagent_list({"session_id": "tui:someone-else"}) == {"items": []}

    async def test_no_session_and_a_session_with_no_calls_are_both_empty(self, workspace: Path) -> None:
        assert await subagent_list({}) == {"items": []}
        assert await subagent_list({"session_id": SESSION}) == {"items": []}

    async def test_newest_first(self, workspace: Path) -> None:
        await _spawn(workspace, "first", label="first")
        await _spawn(workspace, "second", label="second")

        rows = (await subagent_list({"session_id": SESSION}))["items"]

        assert [r["label"] for r in rows] == ["second", "first"]

    async def test_a_spawn_without_a_label_still_has_a_title(self, workspace: Path) -> None:
        """The manager labels every call before the record is opened (the first 30
        characters of the task), so the row is never blank. Asserted because the
        row's only title is ``label`` and the reader has no say in it."""
        await _spawn(workspace, "summarise the release notes and nothing else", label=None)

        row = (await subagent_list({"session_id": SESSION}))["items"][0]

        assert row["label"] == "summarise the release notes an..."


class TestStatus:
    async def test_a_failed_call_reads_as_error(self, workspace: Path) -> None:
        class _Boom:
            async def run(self, task: str, **_kw: Any) -> str:
                raise RuntimeError("the agent died")

        await _spawn(workspace, backend=_Boom())

        row = (await subagent_list({"session_id": SESSION}))["items"][0]
        assert row["status"] == "error"
        ctx = await subagent_context({"id": row["id"], "session_id": SESSION})
        assert "the agent died" in ctx["messages"][-1]["text"]

    async def test_an_in_flight_call_is_listed_while_it_runs(self, workspace: Path) -> None:
        """The prompt is written before dispatch, so a call that never returns is
        still visible -- and the panel needs ``run`` to keep polling it."""
        released = asyncio.Event()

        class _Hangs:
            async def run(self, task: str, **_kw: Any) -> str:
                await released.wait()
                return "done"

        mgr = _manager(workspace)
        mgr.registry.set_builtin_builder(lambda _row, _build, _b=_Hangs(): _b)
        await mgr.spawn("a long one", task_summary="long", session_key=SESSION)
        for _ in range(100):
            if (await subagent_list({"session_id": SESSION}))["items"]:
                break
            await asyncio.sleep(0.02)

        row = (await subagent_list({"session_id": SESSION}))["items"][0]
        assert row["status"] == "run"
        assert row["ended_at"] is None
        assert row["message_count"] == 1, "nothing came back yet"

        ctx = await subagent_context({"id": row["id"], "session_id": SESSION})
        assert [m["role"] for m in ctx["messages"]] == ["user"]
        assert ctx["status"] == "run"

        released.set()
        await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)
        assert (await subagent_list({"session_id": SESSION}))["items"][0]["status"] == "ok"

    async def test_a_record_with_an_unreadable_status_is_derived_from_its_files(self, workspace: Path) -> None:
        """Trusting a missing meta would leave a finished call pulsing as live."""
        await _spawn(workspace)
        row = (await subagent_list({"session_id": SESSION}))["items"][0]
        directory = _call_dir_on_disk(workspace, row["id"])
        (directory / "meta.json").unlink()

        after = (await subagent_list({"session_id": SESSION}))["items"][0]
        assert after["status"] == "ok", "its answer is on disk, so it ended"
        # And the title too: with no meta there is no label, and a row whose only
        # title is empty draws as a blank line in the panel.
        assert after["label"] == "count the files"

    async def test_a_row_prefers_the_task_summary(self, workspace: Path) -> None:
        await _spawn(workspace)
        row = (await subagent_list({"session_id": SESSION}))["items"][0]
        directory = _call_dir_on_disk(workspace, row["id"])
        (directory / "meta.json").write_text(json.dumps({"task_summary": "compare the two vendors"}))

        after = (await subagent_list({"session_id": SESSION}))["items"][0]
        assert after["label"] == "compare the two vendors"

    async def test_a_row_still_reads_a_legacy_label(self, workspace: Path) -> None:
        """Records written before the rename keep their row instead of degrading to
        the prompt's first line."""
        await _spawn(workspace)
        row = (await subagent_list({"session_id": SESSION}))["items"][0]
        directory = _call_dir_on_disk(workspace, row["id"])
        (directory / "meta.json").write_text(json.dumps({"label": "older spawn"}))

        after = (await subagent_list({"session_id": SESSION}))["items"][0]
        assert after["label"] == "older spawn"

    async def test_a_row_with_neither_falls_back_to_the_prompt(self, workspace: Path) -> None:
        await _spawn(workspace, "first line of the prompt\nsecond line")
        row = (await subagent_list({"session_id": SESSION}))["items"][0]
        directory = _call_dir_on_disk(workspace, row["id"])
        (directory / "meta.json").write_text(json.dumps({}))

        after = (await subagent_list({"session_id": SESSION}))["items"][0]
        assert after["label"] == "first line of the prompt"


class TestContext:
    async def test_the_transcript_is_what_was_asked_and_what_came_back(self, workspace: Path) -> None:
        await _spawn(workspace, "count the files")

        row = (await subagent_list({"session_id": SESSION}))["items"][0]
        out = await subagent_context({"id": row["id"], "session_id": SESSION})

        assert [(m["role"], m["text"]) for m in out["messages"]] == [
            ("user", "count the files"),
            ("assistant", "answer to count the files"),
        ]

    async def test_an_unknown_id_is_empty_rather_than_not_found(self, workspace: Path) -> None:
        out = await subagent_context({"id": "20260101T000000Z-nope", "session_id": SESSION})

        assert out["messages"] == []
        assert out["status"] is None

    async def test_a_missing_id_or_session_is_an_error_not_an_empty_transcript(self, workspace: Path) -> None:
        """A caller that forgets one would otherwise get the same silent answer as
        a stale id, which is the failure that kept this surface missing."""
        with pytest.raises(ConfigValidationError, match="id"):
            await subagent_context({"session_id": SESSION})
        with pytest.raises(ConfigValidationError, match="session_id"):
            await subagent_context({"id": "20260101T000000Z-abc"})

    @pytest.mark.parametrize(
        "crafted",
        ["../../../../etc/passwd", "..", "sub/../../..", "/etc/passwd"],
    )
    async def test_an_id_cannot_walk_out_of_the_history_root(self, workspace: Path, crafted: str) -> None:
        """The id is minted by raven but arrives off the wire, and it is joined
        into a path."""
        out = await subagent_context({"id": crafted, "session_id": SESSION})

        assert out["messages"] == []


async def test_the_declared_shapes_match_what_the_handlers_return(workspace: Path) -> None:
    """The fourth side of the contract: params, result, schema, and the handler."""
    from raven.rpc.models import METHOD_MODELS

    await _spawn(workspace)
    listed = await subagent_list({"session_id": SESSION})
    METHOD_MODELS["subagent.list"][1].model_validate(listed)
    METHOD_MODELS["subagent.context"][1].model_validate(
        await subagent_context({"id": listed["items"][0]["id"], "session_id": SESSION})
    )


def test_both_methods_are_registered(workspace: Path) -> None:
    """They were declared and unregistered once, and the panel swallowed the
    -32601 into an empty list."""
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.subagent import register_subagent_methods

    dispatcher = Dispatcher()
    register_subagent_methods(dispatcher)

    assert {"subagent.list", "subagent.context"} <= set(dispatcher._handlers)


def _call_dir_on_disk(workspace: Path, call_id: str) -> Path:
    from raven.agent.subagent.history import spawn_root
    from raven.session.manager import SessionManager

    return spawn_root(SessionManager(workspace).session_dir(SESSION)) / call_id


# ---------------------------------------------------------------------------
# What a run DID, not only what it answered.
#
# Driven through a real spawn again: the account is written by the manager,
# published by the backend and read by the handler, and a fixture shaped like
# meta.json would assert only my idea of the three agreeing.
# ---------------------------------------------------------------------------


class _Busy:
    """A backend that works before it answers, and says so the way one does."""

    def __init__(self, *, tools=("read_file", "exec"), usage=({"prompt_tokens": 120, "completion_tokens": 30},)):
        self._tools, self._usage = tools, usage

    async def run(self, task: str, **_kw: Any) -> str:
        from raven.agent.subagent import activity

        for name in self._tools:
            activity.note_tool_call(name)
        for report in self._usage:
            activity.note_usage(report)
        return "done"


class _Silent:
    """A backend with no per-step visibility at all -- the cli lane."""

    async def run(self, task: str, **_kw: Any) -> str:
        return "done"


async def test_a_run_in_flight_serves_the_transcript_being_collected(workspace: Path) -> None:
    """The record lands when the run finishes, so a watching panel used to see
    a prompt and nothing until the end. While the run is live its account is
    the activity being collected in this process, and the context read serves
    exactly that -- then switches to the disk record once the run settles."""
    from raven.agent.subagent import activity

    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowBackend:
        async def run(self, task: str, **_kw: Any) -> str:
            activity.note_transcript(
                [
                    {"role": "assistant", "content": "", "reasoning_content": "thinking"},
                    {"role": "assistant", "content": "half an answer"},
                ]
            )
            started.set()
            await release.wait()
            return "done"

    mgr = _manager(workspace)
    mgr.registry.set_builtin_builder(lambda _row, _build, _b=_SlowBackend(): _b)
    await mgr.spawn("count the files", task_summary="counting", session_key=SESSION)
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        row = (await subagent_list({"session_id": SESSION}))["items"][0]
        ctx = await subagent_context({"id": row["id"], "session_id": SESSION})
        assert ctx["status"] == "run"
        assert [m.get("text") for m in ctx["messages"]][-1] == "half an answer"
        assert ctx["messages"][1]["reasoning_content"] == "thinking"
    finally:
        release.set()
        await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)

    ctx2 = await subagent_context({"id": row["id"], "session_id": SESSION})
    assert ctx2["status"] == "ok"
    assert ctx2["messages"][-1]["text"] == "done"


async def test_a_run_in_flight_serves_its_console_tail(workspace: Path) -> None:
    """The cli lane streams no transcript; its only in-flight account is its
    own output. The context read carries that tail as a `console` entry while
    the run is live, and drops it once the record's answer takes over."""
    from raven.agent.subagent import activity

    started = asyncio.Event()
    release = asyncio.Event()

    class _ConsoleBackend:
        async def run(self, task: str, **_kw: Any) -> str:
            activity.note_console("searching the web...\n")
            activity.note_console("reading 3 sources\n")
            started.set()
            await release.wait()
            return "done"

    mgr = _manager(workspace)
    mgr.registry.set_builtin_builder(lambda _row, _build, _b=_ConsoleBackend(): _b)
    await mgr.spawn("research it", task_summary="researching", session_key=SESSION)
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        row = (await subagent_list({"session_id": SESSION}))["items"][0]
        ctx = await subagent_context({"id": row["id"], "session_id": SESSION})
        console = [m for m in ctx["messages"] if m["role"] == "console"]
        assert len(console) == 1
        assert "searching the web..." in console[0]["text"]
        assert "reading 3 sources" in console[0]["text"]
    finally:
        release.set()
        await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)

    ctx2 = await subagent_context({"id": row["id"], "session_id": SESSION})
    assert [m for m in ctx2["messages"] if m["role"] == "console"] == []
    assert ctx2["messages"][-1]["text"] == "done"


async def test_the_transcript_carries_the_runs_own_turns_when_recorded(workspace: Path) -> None:
    """A backend that saw the run's steps (acp) leaves transcript.jsonl, and
    the context read splices those turns between the prompt and the answer in
    the same wire shape session.resume uses -- tool calls included."""

    class _SeeingBackend:
        async def run(self, task: str, **_kw: Any) -> str:
            from raven.agent.subagent import activity

            activity.note_transcript(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "let me look",
                        "tool_calls": [
                            {
                                "id": "t1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "t1", "content": "the file's head"},
                ]
            )
            return "done"

    mgr = await _spawn(workspace, backend=_SeeingBackend())
    row = (await subagent_list({"session_id": SESSION}))["items"][0]
    ctx = await subagent_context({"id": row["id"], "session_id": SESSION})

    roles = [m["role"] for m in ctx["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    call_msg = ctx["messages"][1]
    assert call_msg["reasoning_content"] == "let me look"
    assert call_msg["tool_calls"][0]["name"] == "read_file"
    assert ctx["messages"][2]["tool_call_id"] == "t1"
    assert ctx["messages"][3]["text"] == "done"
    del mgr


async def test_the_context_read_names_a_stored_call_in_ravens_vocabulary(workspace: Path) -> None:
    """The record keeps the transport's name and so does the wire: a claude_code
    row is rendered under Claude Code's own names, so a direct chat reads as a
    Claude Code conversation. Raven's own tools keep raven's vocabulary."""

    class _TransportNamedBackend:
        async def run(self, task: str, **_kw: Any) -> str:
            from raven.agent.subagent import activity

            activity.note_transcript(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "t1",
                                "type": "function",
                                "function": {"name": "Bash", "arguments": '{"command": "ls"}'},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "t1", "content": "a.py"},
                ]
            )
            return "done"

    mgr = await _spawn(workspace, backend=_TransportNamedBackend())
    row = (await subagent_list({"session_id": SESSION}))["items"][0]

    ctx = await subagent_context({"id": row["id"], "session_id": SESSION})

    call = next(m for m in ctx["messages"] if m.get("tool_calls"))
    assert call["tool_calls"][0]["name"] == "Bash"
    assert call["tool_calls"][0]["arguments"] == '{"command": "ls"}'
    del mgr


async def test_a_row_carries_what_the_run_cost(workspace: Path) -> None:
    await _spawn(workspace, backend=_Busy())

    row = (await subagent_list({"session_id": SESSION}))["items"][0]

    assert row["tokens"] == 150, "input and output are one number on a row"
    assert row["tool_call_count"] == 2


async def test_a_transport_that_cannot_report_says_nothing_rather_than_zero(workspace: Path) -> None:
    """Zero would be a claim about the agent. Null is the truth about the record:
    the cli lane parses stdout and never sees a token count."""
    await _spawn(workspace, backend=_Silent())

    row = (await subagent_list({"session_id": SESSION}))["items"][0]

    assert row["tokens"] is None
    assert row["tool_call_count"] is None


async def test_usage_accumulates_across_a_run_s_iterations(workspace: Path) -> None:
    """The in-process loop calls the model once per round, so the cost of the run
    is the sum of its reports -- not the last one."""
    await _spawn(workspace, backend=_Busy(usage=({"prompt_tokens": 10}, {"prompt_tokens": 5, "completion_tokens": 7})))

    assert (await subagent_list({"session_id": SESSION}))["items"][0]["tokens"] == 22


async def test_the_transcript_says_what_it_did_between_the_two_messages(workspace: Path) -> None:
    """The whole gap this closes: prompt in, answer out, and no account of the
    work in between -- which for an ACP agent was collected and then dropped."""
    await _spawn(workspace, backend=_Busy())
    listed = await subagent_list({"session_id": SESSION})

    ctx = await subagent_context({"id": listed["items"][0]["id"], "session_id": SESSION})

    assert ctx["tool_calls"] == ["read_file", "exec"], "in the order they happened"
    assert ctx["tokens"] == 150 and ctx["tokens_in"] == 120 and ctx["tokens_out"] == 30


async def test_a_failed_run_keeps_the_account_of_how_far_it_got(workspace: Path) -> None:
    """The failure case is the one worth keeping: what it called before it broke
    is the first thing a reader asks for."""

    class _Breaks:
        async def run(self, task: str, **_kw: Any) -> str:
            from raven.agent.subagent import activity

            activity.note_tool_call("exec")
            activity.note_usage({"prompt_tokens": 40})
            raise RuntimeError("no")

    await _spawn(workspace, backend=_Breaks())
    listed = await subagent_list({"session_id": SESSION})

    assert listed["items"][0]["status"] == "error"
    assert listed["items"][0]["tool_call_count"] == 1
    ctx = await subagent_context({"id": listed["items"][0]["id"], "session_id": SESSION})
    assert ctx["tool_calls"] == ["exec"] and ctx["tokens"] == 40


async def test_publishing_outside_a_run_is_not_an_error(workspace: Path) -> None:
    """A backend is usable outside either delegation path -- a probe, a test, a
    direct call -- and must not have to know whether anything is collecting."""
    from raven.agent.subagent import activity

    activity.note_tool_call("read_file")
    activity.note_usage({"prompt_tokens": 1})
    activity.note_steps({"tool_call": 1})
    activity.note_thoughts(5)

    assert activity.current() is None


async def test_an_unreadable_usage_report_does_not_fail_the_run(workspace: Path) -> None:
    """The record must never take down the work it is describing."""
    from raven.agent.subagent import activity

    with activity.collecting() as did:
        activity.note_usage("not a dict")
        activity.note_usage({"prompt_tokens": "twelve"})
        activity.note_usage({"prompt_tokens": 3})

    assert did.tokens == 3


# ---------------------------------------------------------------------------
# Graph nodes in the same list.
#
# Written as run dirs rather than by running a graph: run_dag needs configured
# third-party agents and a workspace backend, and what is under test here is the
# reader over `graph.json` + `manifest.json`, whose shape comes from
# raven/agent/subagent/dag_store.py.
# ---------------------------------------------------------------------------


def _write_run(workspace: Path, run_id: str, *, manifest: dict | None = None, nodes: list[dict] | None = None) -> Path:
    from raven.agent.subagent.history import dag_root
    from raven.session.manager import SessionManager

    run = dag_root(SessionManager(workspace).session_dir(SESSION)) / run_id
    run.mkdir(parents=True)
    (run / "graph.json").write_text(
        json.dumps(
            {
                "nodes": nodes
                if nodes is not None
                else [
                    {"id": "survey", "subagent": "Researcher", "depends_on": []},
                    {"id": "write", "subagent": "Writer", "depends_on": ["survey"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    if manifest is not None:
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run


async def test_the_panel_lists_the_agents_a_graph_ran(workspace: Path) -> None:
    """The list's question is "which agents worked on this conversation", and it
    used to answer with only the spawned half."""
    _write_run(
        workspace,
        "20260812T120000Z-deadbeef",
        manifest={
            "survey": {
                "status": "completed",
                "subagent": "Researcher",
                "started_at": 1_700_000_000_000,
                "ended_at": 1_700_000_004_000,
                "output_file": "/x/survey.out.md",
                "tokens_in": 90,
                "tokens_out": 10,
                "tool_calls": ["read_file"],
            },
            "write": {"status": "running", "subagent": "Writer", "started_at": 1_700_000_005_000},
        },
    )

    items = (await subagent_list({"session_id": SESSION}))["items"]

    by_node = {i["node"]: i for i in items if i["kind"] == "dag"}
    assert set(by_node) == {"survey", "write"}
    assert by_node["survey"]["agent"] == "Researcher"
    assert by_node["survey"]["status"] == "ok"
    assert by_node["survey"]["tokens"] == 100
    assert by_node["survey"]["tool_call_count"] == 1
    assert by_node["write"]["status"] == "run"
    assert by_node["write"]["ended_at"] is None


async def test_a_skipped_node_is_not_a_row(workspace: Path) -> None:
    """A skipped node never ran -- no transcript, no cost, no clock -- so the
    panel does not list it. The graph view is where "skipped because its
    upstream failed" reads as structure instead of noise."""
    _write_run(
        workspace,
        "20260812T120000Z-deadbeef",
        manifest={
            "survey": {"status": "failed", "subagent": "Researcher", "started_at": 1_700_000_000_000},
            "write": {"status": "skipped", "subagent": "Writer"},
        },
    )

    items = (await subagent_list({"session_id": SESSION}))["items"]
    by_node = {i["node"]: i for i in items if i["kind"] == "dag"}
    assert set(by_node) == {"survey"}
    assert by_node["survey"]["status"] == "error"


async def test_a_cancelled_node_keeps_its_row(workspace: Path) -> None:
    """A cancelled node ran -- it has a transcript, a cost and a clock -- so
    unlike a skipped one its row opens onto something and stays on the panel."""
    _write_run(
        workspace,
        "20260812T120000Z-deadbeef",
        manifest={
            "survey": {
                "status": "cancelled",
                "subagent": "Researcher",
                "started_at": 1_700_000_000_000,
                "ended_at": 1_700_000_004_000,
            },
            "write": {"status": "skipped", "subagent": "Writer"},
        },
    )

    items = (await subagent_list({"session_id": SESSION}))["items"]

    by_node = {i["node"]: i for i in items if i["kind"] == "dag"}
    assert set(by_node) == {"survey"}
    assert by_node["survey"]["status"] == "cancelled"


async def test_an_exception_node_is_reported_as_error(workspace: Path) -> None:
    """A node with exception status (waiting for the caller to decide) is
    reported as 'error' over the wire: lossy but honest that this one needs
    the caller's attention."""
    _write_run(
        workspace,
        "20260812T120000Z-deadbeef",
        manifest={
            "survey": {
                "status": "exception",
                "subagent": "Researcher",
                "started_at": 1_700_000_000_000,
                "ended_at": 1_700_000_004_000,
            },
        },
    )

    items = (await subagent_list({"session_id": SESSION}))["items"]

    by_node = {i["node"]: i for i in items if i["kind"] == "dag"}
    assert by_node["survey"]["status"] == "error"


async def test_a_dag_row_is_addressed_by_run_and_node(workspace: Path) -> None:
    """A node id is unique inside its run and nowhere else, so the pair travels
    and the id is only ever a label."""
    _write_run(workspace, "20260812T120000Z-deadbeef", manifest={"survey": {"status": "completed"}})

    row = next(i for i in (await subagent_list({"session_id": SESSION}))["items"] if i.get("node") == "survey")

    assert row["run_id"] == "20260812T120000Z-deadbeef"
    assert row["id"] == "20260812T120000Z-deadbeef/survey"


async def test_a_dag_row_is_named_by_its_node_summary(workspace: Path) -> None:
    """`graph.json` is this row's only source for what the node was asked, and it
    has carried `node_summary` since the field landed on the node model."""
    _write_run(
        workspace,
        "20260812T120000Z-deadbeef",
        nodes=[{"id": "scan", "subagent": "x", "node_summary": "read the pricing pages"}],
    )

    items = (await subagent_list({"session_id": SESSION}))["items"]

    assert items[0]["label"] == "read the pricing pages"


async def test_a_dag_row_from_an_older_run_still_shows_its_node_id(workspace: Path) -> None:
    _write_run(workspace, "20260812T120000Z-deadbeef", nodes=[{"id": "scan", "subagent": "x"}])

    items = (await subagent_list({"session_id": SESSION}))["items"]

    assert items[0]["label"] == "scan"


async def test_an_in_flight_run_lists_its_nodes_as_queued_only_while_something_executes_it(workspace: Path) -> None:
    """manifest.json is written once, at finalize, so a run that is still going
    has structure and no state. Queued is honest for a run the live tool is
    executing; for anything else it is a lie that never resolves -- the gateway
    died mid-run, and these rows used to say "queued" forever."""
    _write_run(workspace, "20260812T120000Z-deadbeef")

    class _Tool:
        def active_run_ids(self):
            return ["20260812T120000Z-deadbeef"]

    class _Loop:
        tools = {"run_subagent_dag": _Tool()}

    live = (await subagent_list({"session_id": SESSION}, agent_loop_factory=lambda: _Loop()))["items"]
    assert {i["status"] for i in live if i["kind"] == "dag"} == {"queued"}

    # No live tool (or a tool that is not executing this run): the same rows
    # must read as interrupted rather than queued-forever.
    dead = (await subagent_list({"session_id": SESSION}))["items"]
    assert {i["status"] for i in dead if i["kind"] == "dag"} == {"error"}


async def test_a_dead_run_keeps_what_the_registry_recorded(workspace: Path, monkeypatch) -> None:
    """The instance registry is the only durable record of how far an
    unfinalized run got: a node it saw complete stays completed, with its
    stamps, while the rest read interrupted."""
    from raven.rpc.methods import subagent as subagent_module

    _write_run(workspace, "20260812T120000Z-deadbeef")

    class _Registry:
        def list_instances(self, session_key=None):
            return [
                {
                    "kind": "dag-node",
                    "runId": "20260812T120000Z-deadbeef",
                    "nodeId": "survey",
                    "status": "completed",
                    "createdAtMs": 1_700_000_000_000,
                    "updatedAtMs": 1_700_000_004_000,
                }
            ]

    monkeypatch.setattr(subagent_module, "get_registry", lambda: _Registry())

    items = (await subagent_list({"session_id": SESSION}))["items"]
    by_node = {i["node"]: i for i in items if i["kind"] == "dag"}
    assert by_node["survey"]["status"] == "ok"
    assert by_node["survey"]["started_at"] is not None
    assert by_node["survey"]["ended_at"] is not None
    assert by_node["write"]["status"] == "error"


async def test_spawned_calls_and_graph_nodes_share_one_ordering(workspace: Path) -> None:
    """The reader's question is chronological and does not distinguish the two."""
    _write_run(
        workspace,
        "20260101T000000Z-aaaaaaaa",
        manifest={"survey": {"status": "completed", "started_at": 1_500_000_000_000}},
    )
    await _spawn(workspace)

    items = (await subagent_list({"session_id": SESSION}))["items"]

    assert [i["kind"] for i in items][0] == "spawn", "the spawn happened now; the graph run in 2017"
    assert {i["kind"] for i in items} == {"spawn", "dag"}


async def test_a_graph_node_s_own_order_is_pinned_not_incidental(workspace: Path) -> None:
    """Nodes of one run can share a started_at to the second, and the panel polls
    every few seconds: with nothing but the stamp to sort on, the rows swapped
    places under the reader's cursor.

    Asserted as the order itself rather than by comparing two polls -- two samples
    of an unstable sort agree about half the time, so that version of this test
    passed against a deliberate shuffle.
    """
    _write_run(
        workspace,
        "20260812T120000Z-deadbeef",
        manifest={
            "survey": {"status": "completed", "started_at": 1_700_000_000_000},
            "write": {"status": "completed", "started_at": 1_700_000_000_000},
        },
    )

    items = (await subagent_list({"session_id": SESSION}))["items"]

    assert [i["node"] for i in items] == ["write", "survey"], "the id is the tie-break, descending like the stamp"
