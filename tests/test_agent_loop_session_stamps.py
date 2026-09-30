"""Persisted session messages carry a wall-clock timestamp.

The real agent loop persists turns through ``AgentLoop._save_turn`` which
appends raw dicts via the ``Session.record`` choke point. These tests drive
the real loop path (stubbed LLM) and assert the JSONL lines on disk carry a
``timestamp`` and no longer carry the dropped per-message ``received_at`` /
``turn_id`` — pinning the simplified stamping contract at the level that
reproduces a real TUI/CLI turn.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from raven.agent import workdir
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.tools import command_writes
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.contracts.tool import FileRemoval, Tool, ToolResult
from raven.providers.base import LLMProvider, LLMResponse
from raven.sandbox import ExecResult, SandboxExecutor
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
        assert not (isinstance(m.get("notice"), dict) and m["notice"].get("kind") == "question_unanswered")


@pytest.mark.asyncio
async def test_an_unanswered_question_is_stored_as_a_notice(workspace):
    """The line the model is told is not what a reopened session can draw.

    Noted during the model call, which is after the inbound message is filed,
    so the notice belongs to the turn's own save and not to a second copy of it.
    """
    from raven.permissions.turn import UNANSWERED_KIND, note_unanswered, start_permission_turn

    class Noting(StubProvider):
        async def chat(self, *args, **kwargs):
            note_unanswered("Which base branch?")
            note_unanswered("Which base branch?")
            return await super().chat(*args, **kwargs)

    start_permission_turn(None, conversation_id="tui:chat1", turn_id="t")
    agent = AgentLoop(
        provider=Noting(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    await agent._process_message(_make_msg("hello"))

    notices = [m for m in _persisted_messages(workspace) if isinstance(m.get("notice"), dict)]
    assert len(notices) == 1, notices
    assert notices[0]["notice"] == {
        "kind": UNANSWERED_KIND,
        "detail": "Which base branch?",
    }
    assert "_notice" not in notices[0]
    assert "Which base branch?" in notices[0]["content"]
    assert "best judgment" in notices[0]["content"]


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


class _PythonExecutor(SandboxExecutor):
    """Runs a Python action where the shell would run, behind the real ``exec``.

    The tool around it is the one the loop wired -- its fence, its removal
    watch, its listing and its shadow repo all run as served -- so each test
    states the change it wants instead of depending on a shell's own behaviour.
    """

    def __init__(self, action: Any, *, stdout: str = "ran") -> None:
        self._action = action
        self._stdout = stdout

    @property
    def is_sandboxed(self) -> bool:
        return False

    async def exec(
        self, command: str, cwd: str | None = None, timeout: int | None = None, env: dict[str, str] | None = None
    ) -> ExecResult:
        self._action()
        return ExecResult(stdout=self._stdout, stderr="", exit_code=0)


def _command_agent(
    workspace: Path,
    script: list[LLMResponse],
    action: Any = None,
    *,
    checkpoint: bool = False,
    provider: LLMProvider | None = None,
) -> AgentLoop:
    """A loop with its own ``exec``, running ``action`` in place of the shell when given.

    The checkpoint is off unless a test asks for it: its shadow repo is what a
    command's diff is read against, and without it a rewrite is reported with
    no measure of what changed.
    """
    agent = AgentLoop(
        provider=provider or ScriptedProvider(script),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=4, interactive=False),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="always" if checkpoint else "never"))
        ),
    )
    if action is not None:
        agent.tools.get("exec")._executor = _PythonExecutor(action)
    return agent


async def _run_command_turn(
    workspace: Path,
    work: Path,
    script: list[LLMResponse],
    action: Any = None,
    *,
    checkpoint: bool = False,
    provider: LLMProvider | None = None,
):
    """One real turn whose working directory is ``work``, as a served turn has.

    Bound rather than defaulted so the command runs in, and is measured in, the
    directory the test prepared and not the session store beside it.
    """
    agent = _command_agent(workspace, script, action, checkpoint=checkpoint, provider=provider)
    completes: list[dict[str, Any]] = []

    async def on_tool_event(phase: str, info: dict[str, Any]) -> None:
        if phase == "complete":
            completes.append(info)

    with workdir.bind(work):
        out = await agent._process_message(_make_msg("run it"), on_tool_event=on_tool_event)
    assert out is not None
    return completes


def _command_script(command: str = "do it", **arguments: Any) -> list[LLMResponse]:
    return [
        _tool_call("c1", "exec", {"command": command, **arguments}),
        LLMResponse(content="done", finish_reason="stop"),
    ]


@pytest.mark.asyncio
async def test_a_file_a_command_created_reaches_the_event_and_the_stored_entry(workspace):
    """A command reports its output and nothing else, so the file it wrote has
    no record at all unless the directory is read either side of the call.

    The line count is the created file's own: a client draws an added file with
    how much arrived, and the command's output never says. Without a shadow
    repo there are no rules to say which files may be stored, so the text of
    the file stays out of the entry and only its counts go."""
    work = workspace / "work"
    work.mkdir()
    made = work / "made.txt"

    completes = await _run_command_turn(
        workspace, work, _command_script(), lambda: made.write_text("one\ntwo\n", encoding="utf-8")
    )

    written = completes[0]["file_written"]
    assert len(written) == 1, written
    assert Path(written[0]["path"]).resolve() == made.resolve()
    assert written[0]["created"] is True
    assert (written[0]["lines"], written[0]["added"], written[0]["removed"]) == (2, 2, 0)
    assert written[0]["size"] == len("one\ntwo\n")
    assert "diff" not in written[0]
    tool_entry = next(m for m in _persisted_messages(workspace) if m.get("role") == "tool")
    assert "_file_written" not in tool_entry, "the in-flight key must be renamed at save time"
    assert tool_entry["file_written"] == written


@pytest.mark.asyncio
async def test_a_file_a_command_rewrote_is_not_reported_as_a_new_one(workspace):
    """Without a shadow repo a rewrite carries no count. The listing holds sizes,
    never contents, so the old text was never known and a number against it
    would be invented -- and a client that drew this as a creation would claim
    the whole file is new."""
    work = workspace / "work"
    work.mkdir()
    kept = work / "kept.txt"
    kept.write_text("one\n", encoding="utf-8")

    completes = await _run_command_turn(
        workspace, work, _command_script(), lambda: kept.write_text("three\nfour\nfive\n", encoding="utf-8")
    )

    written = completes[0]["file_written"]
    assert len(written) == 1, written
    assert Path(written[0]["path"]).resolve() == kept.resolve()
    assert written[0]["created"] is False
    assert written[0]["lines"] is None
    assert written[0]["size"] == len("three\nfour\nfive\n")
    assert "added" not in written[0] and "removed" not in written[0] and "diff" not in written[0]


@pytest.mark.asyncio
async def test_a_file_a_command_rewrote_carries_its_diff_when_the_shadow_repo_held_it(workspace):
    """The tree staged in front of the command holds what the file said, so the
    change is measured the way a file tool's is: counts and a unified diff of
    only what the command did, not the whole file again. And it is stored with
    the entry, so a reload draws what the live page drew."""
    work = workspace / "work"
    work.mkdir()
    kept = work / "kept.txt"
    kept.write_text("one\ntwo\nthree\n", encoding="utf-8")

    completes = await _run_command_turn(
        workspace,
        work,
        _command_script(),
        lambda: kept.write_text("one\n2\nthree\nfour\n", encoding="utf-8"),
        checkpoint=True,
    )

    written = completes[0]["file_written"]
    assert len(written) == 1, written
    assert written[0]["created"] is False
    assert (written[0]["added"], written[0]["removed"]) == (2, 1)
    body = written[0]["diff"].splitlines()
    assert "-two" in body and "+2" in body and "+four" in body
    assert " one" in body, "unchanged lines are context, not a rewrite"
    tool_entry = next(m for m in _persisted_messages(workspace) if m.get("role") == "tool")
    assert tool_entry["file_written"] == written


@pytest.mark.asyncio
async def test_a_file_a_command_created_carries_its_diff(workspace):
    """A new file needs no earlier copy: everything in it was added. The same
    shape as a rewrite's, so a client draws both the one way."""
    work = workspace / "work"
    work.mkdir()
    made = work / "made.txt"

    completes = await _run_command_turn(
        workspace,
        work,
        _command_script(),
        lambda: made.write_text("one\ntwo\n", encoding="utf-8"),
        checkpoint=True,
    )

    written = completes[0]["file_written"]
    assert (written[0]["added"], written[0]["removed"], written[0]["lines"]) == (2, 0, 2)
    assert written[0]["diff"].splitlines()[2:] == ["@@ -0,0 +1,2 @@", "+one", "+two"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [".env", "local.secret"])
async def test_a_created_file_the_shadow_repo_would_not_store_carries_no_text(workspace, name):
    """The checkpoint keeps credentials and whatever the user's .gitignore names
    out of storage, and a diff is stored with the conversation. A command that
    creates one of those files is reported with its counts and none of its text."""
    work = workspace / "work"
    work.mkdir()
    (work / ".gitignore").write_text("*.secret\n", encoding="utf-8")
    made = work / name

    completes = await _run_command_turn(
        workspace,
        work,
        _command_script(),
        lambda: made.write_text("API_KEY=top-secret\n", encoding="utf-8"),
        checkpoint=True,
    )

    written = completes[0]["file_written"]
    assert len(written) == 1, written
    assert "diff" not in written[0]
    assert (written[0]["added"], written[0]["removed"]) == (1, 0)
    assert "top-secret" not in json.dumps(_persisted_messages(workspace))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["rewrite", "remove"])
async def test_a_file_the_repo_already_held_shows_no_text_once_a_rule_ignores_it(workspace, change):
    """The index keeps updating a path it already tracks after an ignore rule
    names it, so the staged tree still holds the file's old text. What may be
    shown is judged by the rules, not by what the tree happens to hold: the
    rewrite goes out bare and the removal without its body."""
    work = workspace / "work"
    work.mkdir()
    keys = work / "keys.txt"
    keys.write_text("OLD_SECRET=aaa\n", encoding="utf-8")

    def _act() -> None:
        (work / ".gitignore").write_text("keys.txt\n", encoding="utf-8")
        if change == "rewrite":
            keys.write_text("NEW_SECRET=bbb\n", encoding="utf-8")
        else:
            keys.unlink()

    completes = await _run_command_turn(workspace, work, _command_script(), _act, checkpoint=True)

    written = {Path(w["path"]).name: w for w in completes[0]["file_written"]}
    assert "+keys.txt" in written[".gitignore"]["diff"].splitlines()
    if change == "rewrite":
        assert "diff" not in written["keys.txt"] and "added" not in written["keys.txt"]
    else:
        assert [r.get("before") for r in completes[0]["file_removed"]] == [None]
    assert "SECRET" not in json.dumps(completes) + json.dumps(_persisted_messages(workspace))


@pytest.mark.asyncio
async def test_a_command_that_ran_before_this_one_is_not_part_of_its_diff(workspace):
    """The tree is staged in front of each command, not once per turn: a second
    command's diff is what the second command did, against the file as the
    first one left it."""
    work = workspace / "work"
    work.mkdir()
    kept = work / "kept.txt"
    kept.write_text("one\n", encoding="utf-8")
    steps = iter(["one\ntwo\n", "one\ntwo\nthree\n"])
    script = [
        _tool_call("c1", "exec", {"command": "first"}),
        _tool_call("c2", "exec", {"command": "second"}),
        LLMResponse(content="done", finish_reason="stop"),
    ]

    completes = await _run_command_turn(
        workspace, work, script, lambda: kept.write_text(next(steps), encoding="utf-8"), checkpoint=True
    )

    assert [(c["file_written"][0]["added"], c["file_written"][0]["removed"]) for c in completes] == [(1, 0), (1, 0)]
    assert "+three" in completes[1]["file_written"][0]["diff"].splitlines()
    assert "+two" not in completes[1]["file_written"][0]["diff"].splitlines()


@pytest.mark.asyncio
async def test_a_rewrite_the_shadow_repo_does_not_hold_is_reported_without_a_diff(workspace):
    """A file the shadow repo excludes (here a ``.env``, kept out as a likely
    credential) has no earlier copy, so its rewrite is reported bare rather
    than measured against nothing."""
    work = workspace / "work"
    work.mkdir()
    secret = work / ".env"
    secret.write_text("A=1\n", encoding="utf-8")

    completes = await _run_command_turn(
        workspace, work, _command_script(), lambda: secret.write_text("A=2\n", encoding="utf-8"), checkpoint=True
    )

    written = completes[0]["file_written"]
    assert len(written) == 1, written
    assert "added" not in written[0] and "diff" not in written[0]


@pytest.mark.asyncio
async def test_a_file_a_command_removed_without_naming_it_is_still_reported(workspace):
    """The turn never wrote this file, so the watch on its own writes cannot see
    it go and the command named nothing the fence could resolve. The listing is
    the only witness, and without a shadow repo it has no body to offer: the
    file was gone before anything read it."""
    work = workspace / "work"
    work.mkdir()
    doomed = work / "doomed.txt"
    doomed.write_text("one\ntwo\n", encoding="utf-8")

    completes = await _run_command_turn(workspace, work, _command_script("find . -name '*.txt' -delete"), doomed.unlink)

    removed = completes[0]["file_removed"]
    assert len(removed) == 1, removed
    assert Path(removed[0]["path"]).resolve() == doomed.resolve()
    assert "before" not in removed[0]
    assert completes[0]["file_written"] is None
    tool_entry = next(m for m in _persisted_messages(workspace) if m.get("role") == "tool")
    assert tool_entry["file_removed"] == [{"path": removed[0]["path"], "del": 0}]


@pytest.mark.asyncio
async def test_a_file_a_command_removed_carries_what_it_held_when_the_shadow_repo_had_it(workspace):
    """The listing sees the file go after it is gone; the staged tree still has
    it, which is the body a deletion row draws."""
    work = workspace / "work"
    work.mkdir()
    doomed = work / "doomed.txt"
    doomed.write_text("one\ntwo\n", encoding="utf-8")

    completes = await _run_command_turn(
        workspace, work, _command_script("find . -name '*.txt' -delete"), doomed.unlink, checkpoint=True
    )

    removed = completes[0]["file_removed"]
    assert len(removed) == 1, removed
    assert removed[0]["before"] == "one\ntwo\n"
    tool_entry = next(m for m in _persisted_messages(workspace) if m.get("role") == "tool")
    assert tool_entry["file_removed"] == [{"path": removed[0]["path"], "del": 2}]


@pytest.mark.asyncio
async def test_a_removal_the_command_named_is_not_reported_twice(workspace):
    """The listing sees the same deletion the command's own watch caught by
    name. Reported once: two rows for one file read as two files, and the row
    that carries the file's last contents is the one worth keeping."""
    work = workspace / "work"
    work.mkdir()
    doomed = work / "doomed.txt"
    doomed.write_text("one\ntwo\n", encoding="utf-8")

    completes = await _run_command_turn(workspace, work, _command_script(f"rm {doomed}"), doomed.unlink)

    removed = completes[0]["file_removed"]
    assert len(removed) == 1, removed
    assert Path(removed[0]["path"]).resolve() == doomed.resolve()
    assert removed[0]["before"] == "one\ntwo\n"


@pytest.mark.asyncio
async def test_a_file_this_turn_wrote_keeps_its_text_when_a_command_removes_it_unseen(workspace):
    """The listing reports the deletion without a body when no shadow repo held
    the file. The turn itself wrote it, though, so what it wrote is still the
    last thing anyone knew the file to hold."""
    work = workspace / "work"
    work.mkdir()
    made = work / "made.txt"
    script = [
        _tool_call("c1", "write_file", {"path": str(made), "content": "x\ny\n"}),
        _tool_call("c2", "exec", {"command": "find . -name '*.txt' -delete"}),
        LLMResponse(content="done", finish_reason="stop"),
    ]

    completes = await _run_command_turn(workspace, work, script, made.unlink)

    removed = completes[1]["file_removed"]
    assert len(removed) == 1, removed
    assert removed[0]["before"] == "x\ny\n"


@pytest.mark.asyncio
async def test_a_command_whose_output_reads_as_an_error_still_reports_its_files(workspace):
    """The registry treats a result that begins with "Error" as a failure and
    rebuilds it. What the command did to the disk happened all the same, and
    has no other carrier."""
    work = workspace / "work"
    work.mkdir()
    made = work / "made.txt"
    agent = _command_agent(workspace, _command_script())
    agent.tools.get("exec")._executor = _PythonExecutor(
        lambda: made.write_text("one\n", encoding="utf-8"), stdout="Error: half of it failed"
    )
    completes: list[dict[str, Any]] = []

    async def on_tool_event(phase: str, info: dict[str, Any]) -> None:
        if phase == "complete":
            completes.append(info)

    with workdir.bind(work):
        await agent._process_message(_make_msg("run it"), on_tool_event=on_tool_event)

    assert completes[0]["ok"] is False
    assert [Path(w["path"]).resolve() for w in completes[0]["file_written"]] == [made.resolve()]


@pytest.mark.asyncio
async def test_a_command_that_changed_nothing_carries_neither_key(workspace):
    """Most commands read rather than write, and a payload that grew a null key
    under every one of them would change the shape the wire already had."""
    work = workspace / "work"
    work.mkdir()
    (work / "kept.txt").write_text("one\n", encoding="utf-8")

    completes = await _run_command_turn(workspace, work, _command_script("ls"), lambda: None)

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
    """The executor above stands in for the shell. The one agreement the
    feature rests on is that the shell runs in the directory the listing
    walks, and only the shell itself can show it: ``ExecTool`` resolves its cwd
    from the same binding this turn is under."""
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
async def test_the_real_command_tool_rewrite_is_measured_against_the_staged_tree(workspace):
    """A real shell writes through its own cwd, which is the tree the stage
    covers -- the one agreement between the shell and the shadow repo only the
    real tool can show."""
    work = workspace / "work"
    work.mkdir()
    (work / "keep.md").write_text("one\n", encoding="utf-8")

    completes = await _run_command_turn(workspace, work, _command_script("echo two >> keep.md"), checkpoint=True)

    written = completes[0]["file_written"]
    assert [Path(w["path"]).resolve() for w in written] == [(work / "keep.md").resolve()]
    assert (written[0]["added"], written[0]["removed"]) == (1, 0)
    assert "+two" in written[0]["diff"].splitlines()


@pytest.mark.asyncio
async def test_a_created_file_that_is_not_text_is_reported_without_a_count(workspace):
    """A command writes images and archives as readily as it writes text, and a
    row for one still has to say it arrived. Unknown rather than zero: zero is a
    file with nothing in it, which is a different thing to tell the reader."""
    work = workspace / "work"
    work.mkdir()
    made = work / "out.bin"

    completes = await _run_command_turn(
        workspace, work, _command_script(), lambda: made.write_bytes(b"\xff\xfe\x00\x01")
    )

    written = completes[0]["file_written"]
    assert len(written) == 1, written
    assert written[0]["created"] is True
    assert written[0]["lines"] is None
    assert written[0]["size"] == 4
    assert "added" not in written[0]


@pytest.mark.asyncio
async def test_a_created_file_past_the_reading_cap_is_reported_without_a_count(workspace):
    """Perfectly readable text, and still no number: reading a build artifact
    whole to number it costs the call more than the count is worth to the row,
    so past the cap the count is unknown by decision rather than by failure."""
    work = workspace / "work"
    work.mkdir()
    made = work / "big.txt"
    line = "a" * 63 + "\n"
    body = line * (command_writes.TEXT_MAX_BYTES // len(line) + 1)
    assert len(body.encode()) > command_writes.TEXT_MAX_BYTES

    completes = await _run_command_turn(
        workspace, work, _command_script(), lambda: made.write_text(body, encoding="utf-8")
    )

    written = completes[0]["file_written"]
    assert len(written) == 1, written
    assert written[0]["created"] is True
    assert written[0]["lines"] is None
    assert written[0]["size"] == len(body)


@pytest.mark.asyncio
async def test_the_files_a_command_wrote_reach_the_spine_event_a_served_turn_emits(workspace):
    """``_process_message`` hands the payload to the callback the tests above
    read. Every served lane -- CLI, TUI, WebUI -- reads the ``ToolEvent``
    ``run_turn`` emits instead, and that is a second hop the payload has to make
    by hand, beside the diff and the removals it travels with."""
    work = workspace / "work"
    work.mkdir()
    made = work / "made.txt"
    agent = _command_agent(workspace, _command_script(), lambda: made.write_text("one\ntwo\n", encoding="utf-8"))
    events: list[Any] = []

    async def emit(event: Any) -> None:
        events.append(event)

    with workdir.bind(work):
        await agent.run_turn(_make_msg("run it"), emit, lambda: [], stream=False)

    complete = next(e for e in events if isinstance(e, ToolEvent) and e.phase is ToolPhase.COMPLETE)
    assert complete.file_written is not None, complete
    assert Path(complete.file_written[0]["path"]).resolve() == made.resolve()
    assert complete.file_written[0]["lines"] == 2


@pytest.mark.asyncio
async def test_a_command_run_in_another_directory_is_listed_there(workspace):
    """``exec`` takes a ``working_dir`` of its own, and a command sent to one
    writes its files there and nowhere near the turn's directory. The listing
    has to follow it, or a supported call leaves the diff empty."""
    work = workspace / "work"
    work.mkdir()
    other = workspace / "other"
    other.mkdir()

    completes = await _run_command_turn(
        workspace, work, _command_script("printf 'x\\ny\\n' > side.txt", working_dir=str(other))
    )

    written = completes[0]["file_written"]
    assert written is not None, completes[0]
    assert [Path(w["path"]).resolve() for w in written] == [(other / "side.txt").resolve()]
    assert written[0]["created"] is True
    assert written[0]["lines"] == 2


@pytest.mark.asyncio
async def test_a_command_outside_the_shadow_repo_is_reported_without_a_diff(workspace, monkeypatch):
    """``working_dir`` can point anywhere, and the turn's shadow repo only holds
    its own directory. Out there a rewrite is reported bare, as it always was,
    rather than read against a tree that never held the file -- and the command
    does not wait on a staging of a directory it is not running in."""
    from raven.agent.loop.checkpoint import CheckpointService

    stagings: list[Path] = []
    real_stage = CheckpointService.stage_tree

    async def _stage(self: CheckpointService) -> str | None:
        stagings.append(self._workspace)
        return await real_stage(self)

    monkeypatch.setattr(CheckpointService, "stage_tree", _stage)
    work = workspace / "work"
    work.mkdir()
    other = workspace / "other"
    other.mkdir()
    kept = other / "kept.txt"
    kept.write_text("one\n", encoding="utf-8")

    completes = await _run_command_turn(
        workspace,
        work,
        _command_script("printf 'two\\n' >> kept.txt", working_dir=str(other)),
        checkpoint=True,
    )

    written = completes[0]["file_written"]
    assert [Path(w["path"]).resolve() for w in written] == [kept.resolve()]
    assert "added" not in written[0] and "diff" not in written[0]
    assert not (other / ".raven").exists(), "no shadow repo is made for a directory the turn does not own"
    assert stagings == []


@pytest.mark.asyncio
async def test_a_command_run_on_another_machine_takes_no_listing(workspace, monkeypatch):
    """Not an empty listing but none: two walks of this tree around a command
    that ran elsewhere would attribute to it whatever else was written here in
    the meantime, and cost the call the walks for nothing."""
    from raven.agent.tools import machine_exec, snapshot

    roots: list[Any] = []
    real = snapshot.take

    def watched(root: Any) -> Any:
        roots.append(root)
        return real(root)

    work = workspace / "work"
    work.mkdir()
    made = work / "meanwhile.txt"

    async def _remote(command: str, *, connection: str, cwd: str | None = None) -> str:
        made.write_text("written by someone else\n", encoding="utf-8")
        return "ran"

    monkeypatch.setattr(snapshot, "take", watched)
    monkeypatch.setattr(machine_exec, "run_on_machine", _remote)

    completes = await _run_command_turn(workspace, work, _command_script("make", machine="prod"))

    assert roots == []
    assert completes[0]["file_written"] is None


class _SlowFirstReply(ScriptedProvider):
    """A model that takes its time over the first reply, the way a real one does."""

    def __init__(self, script, delay: float) -> None:
        super().__init__(script)
        self._delay = delay

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        if self._delay:
            await asyncio.sleep(self._delay)
            self._delay = 0
        return await super().chat(*args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.production_timing  # a slow first reply against a slow first staging is the property
async def test_the_first_command_in_a_cold_directory_is_measured_when_the_model_took_its_time(workspace, monkeypatch):
    """The first staging in a directory the shadow repo has never indexed hashes
    the whole tree -- seconds on a large one. A session opened by its first
    message was never warmed as it opened, so the turn starts the staging, and
    it runs while the model writes its first reply: the command that follows
    finds it done well inside its wait."""
    import subprocess

    import raven.agent.loop.checkpoint as cp_module

    work = workspace / "work"
    work.mkdir()
    kept = work / "kept.txt"
    kept.write_text("one\n", encoding="utf-8")
    real = subprocess.run
    cold = [True]

    def _cold_first(cmd, **kwargs):
        if "add" in cmd and cold[0]:
            cold[0] = False
            time.sleep(1.0)
        return real(cmd, **kwargs)

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(cp_module, "_STAGE_WAIT_SECONDS", 0.6)
    monkeypatch.setattr(cp_module.subprocess, "run", _cold_first)
    script = _command_script()

    completes = await _run_command_turn(
        workspace,
        work,
        script,
        lambda: kept.write_text("one\ntwo\n", encoding="utf-8"),
        checkpoint=True,
        provider=_SlowFirstReply(script, delay=2.5),
    )

    written = completes[0]["file_written"]
    assert (written[0]["added"], written[0]["removed"]) == (1, 0), written


@pytest.mark.asyncio
@pytest.mark.production_timing  # a staging slower than the wait budget is the property
async def test_a_command_whose_snapshot_is_not_ready_in_time_runs_without_a_diff(workspace, monkeypatch):
    """The command matters more than its diff. Past the wait it runs all the
    same, reported as a bare rewrite, and the staging carries on, so a later
    command finds it done and is measured."""
    import subprocess

    import raven.agent.loop.checkpoint as cp_module

    work = workspace / "work"
    work.mkdir()
    kept = work / "kept.txt"
    kept.write_text("one\n", encoding="utf-8")
    real = subprocess.run
    cold = [True]

    def _cold_first(cmd, **kwargs):
        if "add" in cmd and cold[0]:
            cold[0] = False
            time.sleep(0.5)
        return real(cmd, **kwargs)

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(cp_module, "_STAGE_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(cp_module.subprocess, "run", _cold_first)
    steps = iter(["one\ntwo\n", "one\ntwo\nthree\n"])
    script = [
        _tool_call("c1", "exec", {"command": "echo two >> kept.txt"}),
        _tool_call("c2", "exec", {"command": "echo three >> kept.txt"}),
        LLMResponse(content="done", finish_reason="stop"),
    ]

    class _ThenPatient(ScriptedProvider):
        async def chat(self, *args: Any, **kwargs: Any) -> Any:
            if len(self._script) == 2:
                # By the second command the cold staging has finished, and the
                # wait only has to cover a warm one, however loaded the machine.
                await asyncio.sleep(0.6)
                monkeypatch.setattr(cp_module, "_STAGE_WAIT_SECONDS", 30.0)
            return await super().chat(*args, **kwargs)

    completes = await _run_command_turn(
        workspace,
        work,
        script,
        lambda: kept.write_text(next(steps), encoding="utf-8"),
        checkpoint=True,
        provider=_ThenPatient(script),
    )

    first, later = completes
    assert first["ok"] is True
    assert [Path(w["path"]).resolve() for w in first["file_written"]] == [kept.resolve()]
    assert "added" not in first["file_written"][0] and "diff" not in first["file_written"][0]
    assert later["ok"] is True
    assert (later["file_written"][0]["added"], later["file_written"][0]["removed"]) == (1, 0)
    assert kept.read_text(encoding="utf-8") == "one\ntwo\nthree\n"


@pytest.mark.asyncio
async def test_a_file_saved_between_turns_is_not_counted_as_the_commands_change(workspace):
    """Every command stages the tree afresh, so what the user saved between two
    turns is already in the tree the second command is measured against, and
    is not shown as that command's own edit."""
    work = workspace / "work"
    work.mkdir()
    kept = work / "kept.txt"
    kept.write_text("one\n", encoding="utf-8")

    await _run_command_turn(
        workspace, work, _command_script(), lambda: kept.write_text("one\ntwo\n", encoding="utf-8"), checkpoint=True
    )
    kept.write_text("one\ntwo\nsaved by the user\n", encoding="utf-8")
    completes = await _run_command_turn(
        workspace,
        work,
        _command_script(),
        lambda: kept.write_text("one\ntwo\nsaved by the user\ncmd\n", encoding="utf-8"),
        checkpoint=True,
    )

    written = completes[0]["file_written"]
    assert (written[0]["added"], written[0]["removed"]) == (1, 0), written


@pytest.mark.asyncio
async def test_opening_a_session_starts_staging_the_directory_it_works_in(workspace, monkeypatch):
    """The loop's half of warming a session as it opens: handed the session's
    directory before any turn has bound one, the command tool stages it in the
    background against that directory's own shadow repo, and returns without
    waiting for the staging."""
    import raven.agent.loop.checkpoint as cp_module

    monkeypatch.setattr(cp_module, "_STAGING", {})
    work = workspace / "project"
    work.mkdir()
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    agent = _command_agent(workspace, [], checkpoint=True)

    await agent.tools.get("exec").warm(work)

    staged = [index for index in cp_module._STAGING if index.is_relative_to(work.resolve())]
    assert len(staged) == 1
    assert await asyncio.wrap_future(cp_module._STAGING[staged[0]]) is not None


@pytest.mark.asyncio
async def test_a_session_with_the_checkpoint_off_warms_nothing(workspace, monkeypatch):
    """Only a command's diff is read against the staged tree, and with the
    checkpoint off there is no tree to stage into."""
    import raven.agent.loop.checkpoint as cp_module

    monkeypatch.setattr(cp_module, "_STAGING", {})
    agent = _command_agent(workspace, [])

    await agent.tools.get("exec").warm(workspace)

    assert cp_module._STAGING == {}
    assert not (workspace / ".raven" / "shadow.git").exists()
