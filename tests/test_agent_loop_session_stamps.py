"""Persisted session messages carry a wall-clock timestamp.

The real agent loop persists turns through ``AgentLoop._save_turn`` which
appends raw dicts via the ``Session.record`` choke point. These tests drive
the real loop path (stubbed LLM) and assert the JSONL lines on disk carry a
``timestamp`` and no longer carry the dropped per-message ``received_at`` /
``turn_id`` — pinning the simplified stamping contract at the level that
reproduces a real TUI/CLI turn.
"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from raven.agent import workdir
from raven.agent.loop import AgentLoop
from raven.agent.loop._shared import _FILE_WRITTEN_TEXT_MAX_BYTES
from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.contracts.tool import FileChange, FileRemoval, Tool, ToolResult
from raven.providers.base import LLMProvider, LLMResponse
from raven.spine.events import ToolEvent, ToolPhase
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class StubProvider(LLMProvider):
    """Always returns a fixed assistant message. No tool calls."""

    def __init__(self, content: str = "stub response"):
        super().__init__(api_key="test")
        self._content = content

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content=self._content, finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _make_agent(workspace: Path) -> AgentLoop:
    return AgentLoop(
        provider=StubProvider(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
    )


def _make_msg(content: str = "hello") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel="tui",
            chat_id="chat1",
            sender_id="user",
            chat_type=ChatType.DM,
        ),
        text=content,
    )


def _persisted_messages(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "sessions" / "tui" / "chat1.jsonl"
    assert path.exists(), "session file was not persisted"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in records if r.get("_type") != "metadata"]


@pytest.mark.asyncio
async def test_persisted_messages_carry_timestamp_not_turn_fields(workspace):
    agent = _make_agent(workspace)
    out = await agent._process_message(_make_msg("hello"))
    assert out is not None

    msgs = _persisted_messages(workspace)
    roles = [m.get("role") for m in msgs]
    assert "user" in roles and "assistant" in roles

    for m in msgs:
        assert m.get("timestamp"), f"missing timestamp: {m}"
        assert "received_at" not in m, f"received_at should be dropped: {m}"
        assert "turn_id" not in m, f"turn_id should be dropped: {m}"


@pytest.mark.asyncio
async def test_a_delegated_turn_is_marked_on_disk_as_one(workspace):
    """A re-injected result is a user entry the RUNTIME wrote, and the stored
    line has to say so.

    Without the mark it is an ordinary user message, and the only reader that
    can tell the difference is one watching live (which hears
    ``subagent.delivered``). A reload has nothing to go on and draws the
    injection as a question the user asked -- prompt-injection fence and all.
    The private spelling must not survive the write: it exists to stay out of
    the provider payload, and ``session.resume`` puts the plain one on the wire.
    """
    agent = _make_agent(workspace)
    req = TurnRequest(
        origin=Origin.SUBAGENT,
        source=Source(channel="tui", chat_id="chat1", sender_id="subagent", chat_type=ChatType.DM),
        text="[BEGIN UNTRUSTED subagent #ab12cd34 - ...]\n3 completed\n[END UNTRUSTED subagent #ab12cd34]",
        delegated={"kind": "dag", "label": "run-7", "status": "ok", "run_id": "run-7"},
    )

    out = await agent._process_message(req, origin=Origin.SUBAGENT)
    assert out is not None

    msgs = _persisted_messages(workspace)
    users = [m for m in msgs if m.get("role") == "user"]
    assert len(users) == 1, users
    assert users[0]["delegated"] == {"kind": "dag", "label": "run-7", "status": "ok", "run_id": "run-7"}
    assert "_delegated" not in users[0]
    # The text is still there: the model reads it on its next turn. Only the
    # reader must not read it as prose.
    assert "3 completed" in users[0]["content"]


@pytest.mark.asyncio
async def test_an_ordinary_turn_carries_no_delegation_mark(workspace):
    """The mark means something, so it must be absent from a real question."""
    agent = _make_agent(workspace)
    await agent._process_message(_make_msg("what is the status"))
    for m in _persisted_messages(workspace):
        assert "delegated" not in m, m
        assert "_delegated" not in m, m


@pytest.mark.asyncio
async def test_the_user_message_is_stamped_at_turn_start_not_turn_end(workspace):
    """The regression that made every restored fold read "1s".

    ``_save_turn`` runs after the turn completes, so left alone it stamps the
    user message and the final answer with the same clock read. A restored
    transcript derives the turn's duration from exactly that gap, so the user
    entry must carry the wall clock at which the message *arrived*.
    """
    from datetime import datetime, timedelta

    base = datetime(2026, 8, 13, 12, 0, 0)
    ticks = {"n": 0}

    def fake_now() -> datetime:
        ticks["n"] += 1
        return base + timedelta(seconds=ticks["n"])

    agent = AgentLoop(
        provider=StubProvider(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2, now_fn=fake_now),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    out = await agent._process_message(_make_msg("hello"))
    assert out is not None

    msgs = _persisted_messages(workspace)
    user_ts = next(m["timestamp"] for m in msgs if m.get("role") == "user")
    answer_ts = next(m["timestamp"] for m in reversed(msgs) if m.get("role") == "assistant")
    assert user_ts < answer_ts, (
        f"user message must be stamped when it arrived, before the answer: {user_ts} !< {answer_ts}"
    )


@pytest.mark.asyncio
async def test_a_mid_turn_message_is_stamped_when_it_arrived(workspace):
    """A message merged into a running turn is stored at its own arrival time.

    Two clocks are wrong here and the request's is right. ``_save_turn`` stamps
    whatever has no timestamp with the clock at turn END, and the drain runs at
    the turn's next tool-loop GAP -- both of them minutes after the message was
    typed, on exactly the long turns people correct. The request carries the
    moment it reached the server (``turn.send``), and that is what is stored.
    """
    from dataclasses import replace
    from datetime import datetime, timedelta

    base = datetime(2026, 9, 22, 9, 0, 0)
    arrived = base + timedelta(seconds=30)
    gap = base + timedelta(minutes=30)
    ended = base + timedelta(hours=1)
    clock = {"now": base}

    class SlowProvider(LLMProvider):
        """Answers once, an hour after the turn opened."""

        def __init__(self):
            super().__init__(api_key="test")

        async def chat(
            self,
            messages,
            tools=None,
            model=None,
            max_tokens=4096,
            temperature=0.7,
            reasoning_effort=None,
            tool_choice=None,
        ):
            clock["now"] = ended
            return LLMResponse(content="stub response", finish_reason="stop")

        def get_default_model(self) -> str:
            return "stub"

    gaps = {"n": 0}

    def drain():
        gaps["n"] += 1
        if gaps["n"] > 1:
            return []
        clock["now"] = gap
        return [replace(_make_msg("actually, only the last quarter"), received_at=arrived.isoformat())]

    agent = AgentLoop(
        provider=SlowProvider(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2, now_fn=lambda: clock["now"]),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    out = await agent._process_message(_make_msg("summarise the report"), drain=drain)
    assert out is not None

    msgs = _persisted_messages(workspace)
    merged = [m for m in msgs if str(m.get("content") or "").startswith("actually,")]
    assert len(merged) == 1, msgs
    assert merged[0]["timestamp"] == arrived.isoformat()


@pytest.mark.asyncio
async def test_a_mid_turn_message_without_an_arrival_time_keeps_the_drain_clock(workspace):
    """A submitter that carries no arrival time still gets a timestamp: the
    channels and the ACP steer path reach the same mailbox without one."""
    from datetime import datetime, timedelta

    base = datetime(2026, 9, 22, 9, 0, 0)
    gap = base + timedelta(minutes=30)
    clock = {"now": base}

    class SlowProvider(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")

        async def chat(
            self,
            messages,
            tools=None,
            model=None,
            max_tokens=4096,
            temperature=0.7,
            reasoning_effort=None,
            tool_choice=None,
        ):
            clock["now"] = base + timedelta(hours=1)
            return LLMResponse(content="stub response", finish_reason="stop")

        def get_default_model(self) -> str:
            return "stub"

    gaps = {"n": 0}

    def drain():
        gaps["n"] += 1
        if gaps["n"] > 1:
            return []
        clock["now"] = gap
        return [_make_msg("actually, only the last quarter")]

    agent = AgentLoop(
        provider=SlowProvider(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2, now_fn=lambda: clock["now"]),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    out = await agent._process_message(_make_msg("summarise the report"), drain=drain)
    assert out is not None

    msgs = _persisted_messages(workspace)
    merged = [m for m in msgs if str(m.get("content") or "").startswith("actually,")]
    assert [m["timestamp"] for m in merged] == [gap.isoformat()]


class ScriptedProvider(LLMProvider):
    """Plays back a fixed list of responses, one per call."""

    def __init__(self, script):
        super().__init__(api_key="test")
        self._script = list(script)

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return self._script.pop(0)

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_file_tools_diff_is_stored_on_its_tool_entry(workspace):
    """The live tool event was the diff's only carrier, so a reloaded page
    could never number a change again. The stored tool entry keeps it -- under
    the plain key, with the in-flight private spelling gone."""
    from raven.providers.base import ToolCallRequest

    provider = ScriptedProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="c1", name="write_file", arguments={"path": "a.txt", "content": "one\ntwo\n"})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=3),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    out = await agent._process_message(_make_msg("write it"))
    assert out is not None

    msgs = _persisted_messages(workspace)
    tool_entry = next(m for m in msgs if m.get("role") == "tool")
    assert "_diff" not in tool_entry
    assert "+one" in (tool_entry.get("diff") or ""), f"stored tool entry carries no diff: {tool_entry}"


class _SweepTool(Tool):
    """Reports a removal the way ``exec`` does: read off the disk, not from a
    result of its own. No tool deletes as its purpose, so this is the shape the
    loop has to carry -- a ``ToolResult`` whose ``removed`` names the file."""

    @property
    def name(self) -> str:
        return "sweep"

    @property
    def description(self) -> str:
        return "removes the file it is given"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def execute(self, path: str = "", **kwargs: Any) -> ToolResult:
        target = Path(path)
        before = target.read_text(encoding="utf-8")
        target.unlink()
        return ToolResult(model_text="swept", removed=(FileRemoval(path=path, before=before),))


class _QuietUnlinkTool(Tool):
    """Removes a file and says nothing about it, which is every command that is
    not spelled ``rm <path>``: the only record left is that the file the turn
    wrote is no longer there."""

    @property
    def name(self) -> str:
        return "quiet_unlink"

    @property
    def description(self) -> str:
        return "removes the file it is given, reporting nothing"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def execute(self, path: str = "", **kwargs: Any) -> str:
        Path(path).unlink()
        return "done"


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]):
    from raven.providers.base import ToolCallRequest

    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=arguments)],
        finish_reason="tool_calls",
    )


async def _run_with_tool_events(workspace: Path, script: list[LLMResponse], *extra_tools: Tool):
    """One real turn, returning the ``complete`` payloads it emitted."""
    agent = AgentLoop(
        provider=ScriptedProvider(script),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=4),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    for tool in extra_tools:
        agent.tools.register(tool)
    completes: list[dict[str, Any]] = []

    async def on_tool_event(phase: str, info: dict[str, Any]) -> None:
        if phase == "complete":
            completes.append(info)

    out = await agent._process_message(_make_msg("do it"), on_tool_event=on_tool_event)
    assert out is not None
    return completes


@pytest.mark.asyncio
async def test_a_removed_file_reaches_the_tool_event_and_the_stored_entry(workspace):
    """A deletion has no other carrier. The command's arguments are a string,
    and the file it names is gone by the time anyone looks, so what the live
    event says and what the stored entry says is the whole record.

    The stored entry keeps the line count rather than the body: a reloaded page
    needs to know the file went and how big the hole is, and the text of a file
    nobody can open again is not worth a session's disk."""
    gone = workspace / "gone.txt"
    gone.write_text("one\ntwo\n")

    completes = await _run_with_tool_events(
        workspace,
        [_tool_call("c1", "sweep", {"path": str(gone)}), LLMResponse(content="done", finish_reason="stop")],
        _SweepTool(),
    )

    assert completes[0]["file_removed"] == [{"path": str(gone), "before": "one\ntwo\n"}]
    tool_entry = next(m for m in _persisted_messages(workspace) if m.get("role") == "tool")
    assert "_file_removed" not in tool_entry, "the in-flight key must be renamed at save time"
    assert tool_entry["file_removed"] == [{"path": str(gone), "del": 2}]


@pytest.mark.asyncio
async def test_a_file_this_turn_wrote_is_reported_by_the_call_that_unlinked_it(workspace):
    """The half no tool can see: the command that removed it named it in a way
    nothing could resolve, so the only witness is that the turn wrote the path
    and can no longer find it. Its ``before`` is what the turn itself wrote,
    which is the last thing anyone knew the file to hold."""
    completes = await _run_with_tool_events(
        workspace,
        [
            _tool_call("c1", "write_file", {"path": "kept.txt", "content": "x\ny\n"}),
            _tool_call("c2", "quiet_unlink", {"path": str(workspace / "kept.txt")}),
            LLMResponse(content="done", finish_reason="stop"),
        ],
        _QuietUnlinkTool(),
    )

    assert completes[0]["file_removed"] is None, "the write removed nothing"
    removed = completes[1]["file_removed"]
    assert len(removed) == 1
    assert Path(removed[0]["path"]).resolve() == (workspace / "kept.txt").resolve()
    assert removed[0]["before"] == "x\ny\n"


@pytest.mark.asyncio
async def test_a_call_that_removed_nothing_carries_no_removal_at_all(workspace):
    """``None`` and not an empty list: the outlet leaves the wire key off a
    payload that has none, so every payload the wire already carried keeps the
    shape it had before deletions were tracked."""
    completes = await _run_with_tool_events(
        workspace,
        [
            _tool_call("c1", "write_file", {"path": "a.txt", "content": "one\n"}),
            LLMResponse(content="done", finish_reason="stop"),
        ],
    )

    assert completes[0]["file_removed"] is None
    tool_entry = next(m for m in _persisted_messages(workspace) if m.get("role") == "tool")
    assert "file_removed" not in tool_entry and "_file_removed" not in tool_entry


class _CommandTool(Tool):
    """Stands in for ``exec``: it changes files and reports only that it ran.

    Registered under that name because the name is the decision under test --
    the loop lists the working directory around a command and around nothing
    else. What it runs is Python rather than a shell so each test states the
    change it wants instead of depending on a shell's own behaviour.
    """

    def __init__(self, action: Any, *, removed: Any = (), change: Any = None) -> None:
        self._action = action
        self._removed = removed
        self._change = change

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "runs a command"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}

    async def execute(self, command: str = "", **kwargs: Any) -> Any:
        self._action()
        if self._removed or self._change is not None:
            return ToolResult(model_text="ran", removed=tuple(self._removed), file_change=self._change)
        return "ran"


async def _run_command_turn(workspace: Path, work: Path, script: list[LLMResponse], *extra_tools: Tool):
    """One real turn whose working directory is ``work``, as a served turn has.

    Bound rather than defaulted so the listing covers the directory the command
    ran in and not the session store beside it. The checkpoint is off because
    its shadow repo is a second tree inside that same directory, built for a
    recovery nothing here tests.
    """
    agent = AgentLoop(
        provider=ScriptedProvider(script),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=4, interactive=False),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    for tool in extra_tools:
        agent.tools.register(tool)
    completes: list[dict[str, Any]] = []

    async def on_tool_event(phase: str, info: dict[str, Any]) -> None:
        if phase == "complete":
            completes.append(info)

    with workdir.bind(work):
        out = await agent._process_message(_make_msg("run it"), on_tool_event=on_tool_event)
    assert out is not None
    return completes


def _command_script(command: str = "do it") -> list[LLMResponse]:
    return [_tool_call("c1", "exec", {"command": command}), LLMResponse(content="done", finish_reason="stop")]


@pytest.mark.asyncio
async def test_a_file_a_command_created_reaches_the_event_and_the_stored_entry(workspace):
    """A command reports its output and nothing else, so the file it wrote has
    no record at all unless the directory is read either side of the call.

    The line count is the created file's own: a client draws an added file with
    how much arrived, and the command's output never says."""
    work = workspace / "work"
    work.mkdir()
    made = work / "made.txt"

    completes = await _run_command_turn(
        workspace, work, _command_script(), _CommandTool(lambda: made.write_text("one\ntwo\n", encoding="utf-8"))
    )

    written = completes[0]["file_written"]
    assert len(written) == 1, written
    assert Path(written[0]["path"]).resolve() == made.resolve()
    assert written[0]["created"] is True
    assert written[0]["lines"] == 2
    assert written[0]["size"] == len("one\ntwo\n")
    tool_entry = next(m for m in _persisted_messages(workspace) if m.get("role") == "tool")
    assert "_file_written" not in tool_entry, "the in-flight key must be renamed at save time"
    assert tool_entry["file_written"] == written


@pytest.mark.asyncio
async def test_a_file_a_command_rewrote_is_not_reported_as_a_new_one(workspace):
    """A rewrite carries no count. The listing holds sizes, never contents, so
    the old text was never known and a number against it would be invented --
    and a client that drew this as a creation would claim the whole file is new."""
    work = workspace / "work"
    work.mkdir()
    kept = work / "kept.txt"
    kept.write_text("one\n", encoding="utf-8")

    completes = await _run_command_turn(
        workspace,
        work,
        _command_script(),
        _CommandTool(lambda: kept.write_text("three\nfour\nfive\n", encoding="utf-8")),
    )

    written = completes[0]["file_written"]
    assert len(written) == 1, written
    assert Path(written[0]["path"]).resolve() == kept.resolve()
    assert written[0]["created"] is False
    assert written[0]["lines"] is None
    assert written[0]["size"] == len("three\nfour\nfive\n")


@pytest.mark.asyncio
async def test_a_file_a_command_removed_without_naming_it_is_still_reported(workspace):
    """The turn never wrote this file, so the watch on its own writes cannot see
    it go and the command named nothing the fence could resolve. The listing is
    the only witness, and it has no body to offer: the file was gone before
    anything read it."""
    work = workspace / "work"
    work.mkdir()
    doomed = work / "doomed.txt"
    doomed.write_text("one\ntwo\n", encoding="utf-8")

    completes = await _run_command_turn(
        workspace, work, _command_script("find . -name '*.txt' -delete"), _CommandTool(doomed.unlink)
    )

    removed = completes[0]["file_removed"]
    assert len(removed) == 1, removed
    assert Path(removed[0]["path"]).resolve() == doomed.resolve()
    assert "before" not in removed[0]
    assert completes[0]["file_written"] is None
    tool_entry = next(m for m in _persisted_messages(workspace) if m.get("role") == "tool")
    assert tool_entry["file_removed"] == [{"path": removed[0]["path"], "del": 0}]


@pytest.mark.asyncio
async def test_a_removal_the_command_reported_is_not_reported_twice(workspace):
    """The listing sees the same deletion the tool named. Reported once: two
    rows for one file read as two files, and the row that carries the file's
    last contents is the one worth keeping."""
    work = workspace / "work"
    work.mkdir()
    doomed = work / "doomed.txt"
    doomed.write_text("one\ntwo\n", encoding="utf-8")

    completes = await _run_command_turn(
        workspace,
        work,
        _command_script(f"rm {doomed}"),
        _CommandTool(doomed.unlink, removed=(FileRemoval(path=str(doomed), before="one\ntwo\n"),)),
    )

    assert completes[0]["file_removed"] == [{"path": str(doomed), "before": "one\ntwo\n"}]


@pytest.mark.asyncio
async def test_a_file_the_call_already_named_is_not_reported_a_second_time(workspace):
    """A call that reports its own write is believed over the listing: the
    result carries the contents, which a listing of sizes never can."""
    work = workspace / "work"
    work.mkdir()
    made = work / "made.txt"

    completes = await _run_command_turn(
        workspace,
        work,
        _command_script(),
        _CommandTool(
            lambda: made.write_text("one\ntwo\n", encoding="utf-8"),
            change=FileChange(path=str(made), before=None, after="one\ntwo\n"),
        ),
    )

    assert completes[0]["file_change"] == {"path": str(made), "after": "one\ntwo\n"}
    assert completes[0]["file_written"] is None


@pytest.mark.asyncio
async def test_a_command_that_changed_nothing_carries_neither_key(workspace):
    """Most commands read rather than write, and a payload that grew a null key
    under every one of them would change the shape the wire already had."""
    work = workspace / "work"
    work.mkdir()
    (work / "kept.txt").write_text("one\n", encoding="utf-8")

    completes = await _run_command_turn(workspace, work, _command_script("ls"), _CommandTool(lambda: None))

    assert completes[0]["file_written"] is None
    assert completes[0]["file_removed"] is None
    tool_entry = next(m for m in _persisted_messages(workspace) if m.get("role") == "tool")
    assert "file_written" not in tool_entry and "_file_written" not in tool_entry


@pytest.mark.asyncio
async def test_a_tool_that_is_not_a_command_is_never_worth_a_listing(workspace, monkeypatch):
    """Every other tool reports the file it touched. Walking the whole working
    directory twice around a call that already said what it did would cost the
    turn far more than the nothing it could add."""
    from raven.agent.tools import snapshot as snapshot_module

    roots: list[Any] = []
    monkeypatch.setattr(snapshot_module, "take", lambda root: roots.append(root))
    work = workspace / "work"
    work.mkdir()

    completes = await _run_command_turn(
        workspace,
        work,
        [
            _tool_call("c1", "write_file", {"path": str(work / "a.txt"), "content": "one\n"}),
            LLMResponse(content="done", finish_reason="stop"),
        ],
    )

    assert roots == []
    assert completes[0]["file_written"] is None


@pytest.mark.asyncio
async def test_the_real_command_tool_lists_the_directory_it_was_bound_to(workspace):
    """The stubs above stand in for ``exec`` and agree with the listing by
    construction. The one agreement the feature rests on is that the shell runs
    in the directory the listing walks, and only the shell itself can show it:
    ``ExecTool`` resolves its cwd from the same binding this turn is under."""
    work = workspace / "work"
    work.mkdir()
    (work / "keep.md").write_text("one\n", encoding="utf-8")

    completes = await _run_command_turn(
        workspace, work, _command_script("printf 'a\\nb\\n' > made.txt && echo more >> keep.md")
    )

    written = {Path(w["path"]).resolve(): w for w in completes[0]["file_written"]}
    assert set(written) == {(work / "made.txt").resolve(), (work / "keep.md").resolve()}
    assert written[(work / "made.txt").resolve()]["created"] is True
    assert written[(work / "made.txt").resolve()]["lines"] == 2
    assert written[(work / "keep.md").resolve()]["created"] is False


@pytest.mark.asyncio
async def test_a_created_file_that_is_not_text_is_reported_without_a_count(workspace):
    """A command writes images and archives as readily as it writes text, and a
    row for one still has to say it arrived. Unknown rather than zero: zero is a
    file with nothing in it, which is a different thing to tell the reader."""
    work = workspace / "work"
    work.mkdir()
    made = work / "out.bin"

    completes = await _run_command_turn(
        workspace, work, _command_script(), _CommandTool(lambda: made.write_bytes(b"\xff\xfe\x00\x01"))
    )

    written = completes[0]["file_written"]
    assert len(written) == 1, written
    assert written[0]["created"] is True
    assert written[0]["lines"] is None
    assert written[0]["size"] == 4


@pytest.mark.asyncio
async def test_a_created_file_past_the_reading_cap_is_reported_without_a_count(workspace):
    """Perfectly readable text, and still no number: reading a build artifact
    whole to number it costs the turn more than the count is worth to the row,
    so past the cap the count is unknown by decision rather than by failure."""
    work = workspace / "work"
    work.mkdir()
    made = work / "big.txt"
    line = "a" * 63 + "\n"
    body = line * (_FILE_WRITTEN_TEXT_MAX_BYTES // len(line) + 1)
    assert len(body.encode()) > _FILE_WRITTEN_TEXT_MAX_BYTES

    completes = await _run_command_turn(
        workspace, work, _command_script(), _CommandTool(lambda: made.write_text(body, encoding="utf-8"))
    )

    written = completes[0]["file_written"]
    assert len(written) == 1, written
    assert written[0]["created"] is True
    assert written[0]["lines"] is None
    assert written[0]["size"] == len(body)


@pytest.mark.asyncio
async def test_numbering_the_files_a_command_wrote_never_runs_on_the_event_loop(workspace, monkeypatch):
    """The two walks were put on a worker thread because every other session on
    this process waits behind whatever the loop does. The counting that follows
    reads each created file whole, which for a command that wrote a hundred of
    them is the larger stall of the two."""
    from raven.agent.loop import turn_path as turn_path_module

    real = turn_path_module._file_written_payload
    threads: list[int] = []

    def watched(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(turn_path_module, "_file_written_payload", watched)
    work = workspace / "work"
    work.mkdir()
    made = work / "made.txt"

    completes = await _run_command_turn(
        workspace, work, _command_script(), _CommandTool(lambda: made.write_text("one\n", encoding="utf-8"))
    )

    assert completes[0]["file_written"], completes[0]
    assert threads and threading.get_ident() not in threads


@pytest.mark.asyncio
async def test_the_files_a_command_wrote_reach_the_spine_event_a_served_turn_emits(workspace):
    """``_process_message`` hands the payload to the callback the tests above
    read. Every served lane -- CLI, TUI, WebUI -- reads the ``ToolEvent``
    ``run_turn`` emits instead, and that is a second hop the payload has to make
    by hand, beside the diff and the removals it travels with."""
    work = workspace / "work"
    work.mkdir()
    made = work / "made.txt"
    agent = AgentLoop(
        provider=ScriptedProvider(_command_script()),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=4, interactive=False),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    agent.tools.register(_CommandTool(lambda: made.write_text("one\ntwo\n", encoding="utf-8")))
    events: list[Any] = []

    async def emit(event: Any) -> None:
        events.append(event)

    with workdir.bind(work):
        await agent.run_turn(_make_msg("run it"), emit, lambda: [], stream=False)

    complete = next(e for e in events if isinstance(e, ToolEvent) and e.phase is ToolPhase.COMPLETE)
    assert complete.file_written is not None, complete
    assert Path(complete.file_written[0]["path"]).resolve() == made.resolve()
    assert complete.file_written[0]["lines"] == 2
