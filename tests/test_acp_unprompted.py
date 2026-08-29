"""The recorder for what an ACP agent does between prompts.

Every sink in `_SessionRouter` is attached for one run and detached at its end,
which reads the stream as request/response. An on-call agent breaks that: it
wakes on its own schedule, works, and reports with nobody having asked. These
pin the two outputs that make such a turn exist for the operator -- the wire
events a pane renders live, and the instance-log row a fresh entry reads back --
because each shipped broken while the other looked finished.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from raven.agent.acp_client.unprompted import UnpromptedRecorder


class _Registry:
    def __init__(self, session_key="tui:sess-1", handle="h-1", agent="Oncall", agent_id="acp:abc"):
        self._rows = {(session_key, agent, handle): {"agentId": agent_id}}
        self.marks: list[tuple[str, str, str]] = []

    def _load(self):
        return self._rows

    async def upsert_spawn(self, session_key, agent, handle, status):
        self.marks.append((session_key, handle, status))


def _frame(kind: str, text: str | None = None, session="acp:abc", call_id: str | None = None):
    update: dict = {"sessionUpdate": kind}
    if text is not None:
        update["content"] = {"text": text}
    if call_id is not None:
        update["toolCallId"] = call_id
    return "session/update", {"sessionId": session, "update": update}


def _recorder(tmp_path: Path, registry=None):
    emitted: list[tuple[str, dict]] = []
    rec = UnpromptedRecorder(
        "Oncall",
        registry or _Registry(),
        lambda session_key: tmp_path,
        emit=lambda sk, ev: emitted.append((sk, ev)),
    )
    return rec, emitted


def _log_rows(tmp_path: Path) -> list[dict]:
    from raven.agent.subagent.instance_log import transcript_path

    path = transcript_path(tmp_path, "Oncall", "h-1")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def test_a_turn_reaches_the_pane_as_the_three_events_a_typed_turn_uses(tmp_path):
    """The client demultiplexes message.start / token.delta / message.complete on
    ``target`` -- the same three a typed direct-chat turn produces -- and it must
    not be able to tell this turn from one it asked for. That is what makes the
    fix zero client change on the front ends that consume it (ui-tui
    dispatchDirect), and it is the half that was missing while the log write looked
    finished: rounds ran, the log grew, the pane sat on the first reply."""
    rec, emitted = _recorder(tmp_path)

    await rec(*_frame("agent_thought_chunk", "thinking"))
    await rec(*_frame("agent_message_chunk", "70kN submitted"))
    await rec(*_frame("usage_update"))

    kinds = [ev["type"] for _, ev in emitted]
    assert kinds == ["message.start", "token.delta", "message.complete"]
    assert all(ev["payload"]["target"] == {"agent": "Oncall", "handle": "h-1"} for _, ev in emitted)
    assert {sk for sk, _ in emitted} == {"tui:sess-1"}
    # start and complete carry the same turn id, which is what lets the client
    # release the running slot it took.
    assert emitted[0][1]["payload"]["turn_id"] == emitted[-1][1]["payload"]["turn_id"]


async def test_the_logged_turn_has_a_user_boundary_row(tmp_path):
    """The conversation folder starts a message at a ``user`` row, so a turn
    without one damages its NEIGHBOURS: it merges into the previous turn on a
    fresh read, and the next typed turn is drawn as its continuation. The DAG
    lane shipped exactly this hole (prompt=None while its siblings passed the
    task) -- this lane must not reintroduce it."""
    rec, _ = _recorder(tmp_path)

    await rec(*_frame("agent_message_chunk", "the round's findings"))
    await rec(*_frame("usage_update"))

    rows = _log_rows(tmp_path)
    roles = [r.get("role") for r in rows if r.get("role") in ("user", "assistant")]
    assert roles == ["user", "assistant"], rows
    said = [r for r in rows if r.get("role") == "assistant"]
    assert said[0]["content"] == "the round's findings"


async def test_registry_status_walks_running_then_completed(tmp_path):
    """The registry row is what the instance strip and a freshly entered pane
    read; the wire events are what an open pane reacts to. Both, or one of the
    two views goes stale."""
    registry = _Registry()
    rec, _ = _recorder(tmp_path, registry)

    await rec(*_frame("agent_message_chunk", "x"))
    await rec(*_frame("usage_update"))

    assert registry.marks == [("tui:sess-1", "h-1", "running"), ("tui:sess-1", "h-1", "completed")]


async def test_a_takeover_flush_closes_the_turn_before_the_prompt_runs(tmp_path):
    """A run taking the session over ends whatever the agent was saying on its
    own account. Left buffered it would splice onto the NEXT unprompted turn,
    with the prompted one sitting between them in the log."""
    rec, emitted = _recorder(tmp_path)

    await rec(*_frame("agent_message_chunk", "half a report"))
    await rec.flush("acp:abc")

    assert [ev["type"] for _, ev in emitted] == ["message.start", "token.delta", "message.complete"]
    rows = _log_rows(tmp_path)
    assert any(r.get("role") == "assistant" and r.get("content") == "half a report" for r in rows)
    # And the buffer is gone: a second flush must not write the turn twice.
    await rec.flush("acp:abc")
    assert len(_log_rows(tmp_path)) == len(rows)


async def test_a_session_no_instance_claims_is_not_written_and_not_announced(tmp_path):
    """The registry is the only thing relating an ACP session id to an instance.
    No row means nowhere truthful to put the turn -- writing under a guessed
    handle would be worse than the silence this module exists to fix."""
    rec, emitted = _recorder(tmp_path, _Registry(agent_id="acp:someone-else"))

    await rec(*_frame("agent_message_chunk", "orphan"))
    await rec(*_frame("usage_update"))

    assert emitted == []
    assert _log_rows(tmp_path) == []


async def test_silence_ends_a_turn_that_reports_no_usage(tmp_path, monkeypatch):
    """``message.complete`` produces a wire frame only when there is usage to
    report, so a turn without one ends in no frame at all -- waited on alone,
    one live wake round was buffered forever. Silence is the fallback ending."""
    import raven.agent.acp_client.unprompted as mod

    monkeypatch.setattr(mod, "_IDLE_S", 0.05)
    rec, emitted = _recorder(tmp_path)

    await rec(*_frame("agent_message_chunk", "no usage follows"))
    await asyncio.sleep(0.2)

    assert [ev["type"] for _, ev in emitted] == ["message.start", "token.delta", "message.complete"]
    assert any(r.get("role") == "assistant" for r in _log_rows(tmp_path))


async def test_a_resumed_session_does_not_replay_its_history_as_an_unprompted_turn(tmp_path):
    """`session/load` is not a getter -- the agent answers it by replaying the
    transcript as `session/update` frames. No run holds the session yet at that
    point, so without a guard those fall through to the resident recorder and
    every resumed turn logs its own history as work the agent did unasked."""
    from raven.agent.acp_client.capabilities import CapabilitySnapshot
    from raven.agent.acp_client.pool import _SessionRouter
    from raven.agent.subagent.backends.acp_agent import AcpAgentBackend

    router = _SessionRouter("Oncall")
    rec, emitted = _recorder(tmp_path)
    router.set_resident(rec)

    class _Client:
        async def request(self, method, params, timeout=None, **kw):
            assert method == "session/load"
            for text in ("an older answer", "and another"):
                await router.dispatch(*_frame("agent_message_chunk", text))
            await router.dispatch(*_frame("usage_update"))
            return {}

    class _Reg:
        async def lookup(self, session_key, agent, handle, *, kind="cli"):
            return "acp:abc"

    backend = AcpAgentBackend(
        name="Oncall",
        command="unused",
        registry=_Reg(),
        snapshot=CapabilitySnapshot(
            agent="Oncall",
            fingerprint="f",
            status="ready",
            detail="",
            measured_at_ms=0,
            can_resume=True,
            can_load=True,
        ),
    )
    session_id, resumed = await backend._open_session(
        _Client(),
        cwd=str(tmp_path),
        skey="tui:sess-1",
        handle="h-1",
        budget=5.0,
        mcp_servers=[],
        router=router,
    )

    assert (session_id, resumed) == ("acp:abc", True)
    assert emitted == []
    assert _log_rows(tmp_path) == []
    # And the resident sink is reachable again: the guard held the session for
    # the length of the call, it did not replace what the connection routes to.
    await router.dispatch(*_frame("agent_message_chunk", "a real wake"))
    await rec.flush("acp:abc")
    assert any(r.get("content") == "a real wake" for r in _log_rows(tmp_path))


async def test_a_wake_that_only_used_tools_is_recorded_as_having_said_nothing(tmp_path):
    """A wake often does its whole job through tools -- reads a ledger, sees the
    run is still going, arms the next look. Five of eight turns in one measured
    campaign said nothing at all. Recording no reply for those leaves the `user`
    boundary row standing alone, and a lone boundary does the damage the boundary
    exists to prevent one turn further on: the next reply is drawn as the answer
    to this marker."""
    rec, emitted = _recorder(tmp_path)

    # One call, as ACP actually sends it: an opening frame and the updates that
    # carry its status and its result, all against the same id.
    await rec(*_frame("tool_call", call_id="c1"))
    await rec(*_frame("tool_call_update", call_id="c1"))
    await rec(*_frame("tool_call_update", call_id="c1"))
    await rec(*_frame("usage_update"))

    rows = _log_rows(tmp_path)
    boundary = [i for i, r in enumerate(rows) if r.get("role") == "user"]
    assert boundary, "the turn was not recorded at all"
    after = rows[boundary[-1] + 1] if boundary[-1] + 1 < len(rows) else None
    assert after is not None and after.get("role") == "assistant", "boundary row left with no reply"
    # One call, said once, and singular: three frames are not three calls.
    assert after["content"] == "[unprompted: 1 tool call, no message]"


async def test_two_calls_are_two_however_many_frames_each_one_takes(tmp_path):
    """The count is over distinct `toolCallId`s, so a call that reports twice
    still counts once and a second call is still visible."""
    rec, _ = _recorder(tmp_path)

    await rec(*_frame("tool_call", call_id="c1"))
    await rec(*_frame("tool_call_update", call_id="c1"))
    await rec(*_frame("tool_call", call_id="c2"))
    await rec(*_frame("tool_call_update", call_id="c2"))
    await rec(*_frame("tool_call_update", call_id="c2"))
    await rec(*_frame("usage_update"))

    rows = _log_rows(tmp_path)
    assert any(r.get("content") == "[unprompted: 2 tool calls, no message]" for r in rows)


async def test_a_turn_that_said_something_records_the_words_not_the_count(tmp_path):
    """The fallback is for turns with nothing to quote; it must never displace
    what the agent actually said."""
    rec, _ = _recorder(tmp_path)

    await rec(*_frame("tool_call"))
    await rec(*_frame("agent_message_chunk", "the run passed the knee"))
    await rec(*_frame("usage_update"))

    rows = _log_rows(tmp_path)
    assert any(r.get("role") == "assistant" and r["content"] == "the run passed the knee" for r in rows)
    assert not any("no message" in str(r.get("content")) for r in rows)
