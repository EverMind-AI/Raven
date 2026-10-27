"""raven.acp_client.remote_resume: the reply a turn left on a machine owes, read off the machine's daemon."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from raven.acp_client import remote_resume, remote_runs
from raven.acp_client.acp_agent import AcpAgentBackend
from raven.acp_client.pool import close_pool, get_pool
from raven.ops import remote_path
from tests.test_acp_client_resume import _Pipe
from tests.test_node_client import row

HOST, PORT = "203.0.113.7", 58717


@pytest.fixture(autouse=True)
def _machines(tmp_path, monkeypatch):
    registry = tmp_path / "connections.json"
    registry.write_text(json.dumps({"connections": [row()]}), encoding="utf-8")
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(registry))
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(remote_path, "_NODES", None)


@pytest.fixture
async def _pool():
    yield get_pool()
    await close_pool()


class _Link:
    """A node pool with one task, whose attach hands over a pipe the test speaks the agent's part into."""

    def __init__(self, *, tasks: list[dict[str, Any]] | None = None, rc: int | None = None) -> None:
        self.rows = tasks if tasks is not None else [{"task": "stub@box-1", "state": "running"}]
        self.rc = rc
        self.pipe = _Pipe()
        self.attached: list[tuple[str, str, int, int]] = []
        self.refuse: Exception | None = None

    async def tasks(self, machine_id: str) -> list[dict[str, Any]]:
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows

    async def attach(self, machine_id: str, task: str, *, name: str, after: int = 0, stderr_after: int = 0):
        if self.refuse is not None:
            raise self.refuse
        self.attached.append((machine_id, task, after, stderr_after))
        return self.pipe, {"pid": 7, "frames": 9, "replayed": 0, "rc": self.rc}


def _run(**kw) -> remote_runs.RemoteRun:
    fields = dict(
        machine="box",
        task="stub@box-1",
        agent="stub@box",
        session_key="tui:s1",
        handle="t1",
        task_id="t1",
        session_id="sess-1",
        prompt_id=3,
        next_id=4,
        initialize={},
        launch_key="k",
        config_key="c",
        session_cwd="/home/worker/raven-work/t1",
        stateful=False,
        received=5,
    )
    fields.update(kw)
    return remote_runs.RemoteRun(**fields)


def _backend() -> AcpAgentBackend:
    return AcpAgentBackend(name="stub@box", command="stub", machine="box")


def _chunk(text: str, session: str = "sess-1") -> dict[str, Any]:
    update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    return {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session, "update": update}}


async def test_the_reply_is_read_off_the_frames_the_other_process_never_saw_and_the_agent_is_the_pool_s(_pool):
    link = _Link()
    picking = asyncio.ensure_future(remote_resume.pick_up(_run(), _backend(), pool=link))
    await asyncio.sleep(0.05)
    assert link.attached == [("box", "stub@box-1", 5, 0)], "from the frame the other process last read"
    link.pipe.says(_chunk("po"))
    link.pipe.says(_chunk("ng"))
    link.pipe.says(_chunk("not mine", session="other"))
    link.pipe.says({"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}})

    picked = await asyncio.wait_for(picking, timeout=5)

    assert picked.status == "ok" and picked.stop_reason == "end_turn"
    assert picked.reply.startswith("pong\n\n[raven] Ran on machine 'Lab box' (box), in scp://worker@")
    assert (
        "picked up after this computer restarted" in picked.reply and "deliver_files fetches one by it" in picked.reply
    )
    (connection,) = _pool.connections("stub@box")
    assert connection.client.endpoint is link.pipe and connection.client.next_request_id == 4 + remote_resume._ID_ROOM
    assert connection.initialize == {} and connection.launch_key == "k"


async def test_a_pick_up_asks_the_stderr_from_the_line_the_record_kept_as_the_prompt_went_out(_pool):
    """An ACP agent is one task across every prompt its process is sent: the earlier prompts' stderr is not
    read again into this turn's tail and journal."""
    link = _Link()
    picking = asyncio.ensure_future(remote_resume.pick_up(_run(stderr_received=12), _backend(), pool=link))
    await asyncio.sleep(0.05)
    assert link.attached == [("box", "stub@box-1", 5, 12)]
    link.pipe.says(_chunk("pong"))
    link.pipe.says({"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}})

    picked = await asyncio.wait_for(picking, timeout=5)

    assert picked.status == "ok" and picked.reply.startswith("pong")


async def test_a_turn_cut_short_is_marked_partial_as_a_turn_ended_here_would_be(_pool):
    link = _Link()
    picking = asyncio.ensure_future(remote_resume.pick_up(_run(), _backend(), pool=link))
    await asyncio.sleep(0.05)
    link.pipe.says(_chunk("half"))
    link.pipe.says({"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "max_tokens"}})

    picked = await asyncio.wait_for(picking, timeout=5)

    assert picked.status == "ok" and picked.reply.startswith("half\n\n[raven] stub@box stopped before finishing")


async def test_a_picked_up_copilot_reply_that_is_its_providers_refusal_is_an_error(_pool):
    """The pick-up reports a left run the way the turn would have ended here, and
    a Copilot turn whose whole reply is its provider's refusal fails here."""
    link = _Link()
    backend = AcpAgentBackend(name="stub@box", command="stub", machine="box", preset="github_copilot")
    picking = asyncio.ensure_future(remote_resume.pick_up(_run(), backend, pool=link))
    await asyncio.sleep(0.05)
    link.pipe.says(_chunk("Error: 402 Your credit balance is too low. Please add credits."))
    link.pipe.says({"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}})

    picked = await asyncio.wait_for(picking, timeout=5)

    assert picked.status == "error"
    assert "for want of credit" in picked.reply


async def test_a_machine_that_cannot_be_asked_or_a_link_that_cannot_attach_keeps_the_record(_pool):
    link = _Link(tasks=remote_path.NodeCallError("the Raven node on machine 'Lab box' (box) did not answer"))
    with pytest.raises(remote_resume.UnreachableError, match="did not answer"):
        await remote_resume.pick_up(_run(), _backend(), pool=link)

    link = _Link()
    link.refuse = remote_path.NodeCallError("the Raven node on machine 'Lab box' (box) did not answer agent/attach")
    with pytest.raises(remote_resume.UnreachableError, match="agent/attach"):
        await remote_resume.pick_up(_run(), _backend(), pool=link)

    with pytest.raises(remote_resume.UnreachableError, match="does not reach registered machines"):
        await remote_resume.pick_up(_run(), _backend(), pool=None)


async def test_a_run_the_machine_no_longer_has_or_never_got_a_prompt_for_is_an_error_without_an_attach(_pool):
    link = _Link(tasks=[])
    picked = await remote_resume.pick_up(_run(), _backend(), pool=link)
    assert picked.status == "error" and "no longer has the run of 'stub@box' (task stub@box-1)" in picked.reply

    link = _Link()
    picked = await remote_resume.pick_up(_run(prompt_id=0), _backend(), pool=link)
    assert picked.status == "error" and "ended before its prompt was sent" in picked.reply
    assert link.attached == []

    link = _Link()
    link.refuse = remote_path.NodeCallError("machine 'Lab box' (box) no longer runs 'stub@box' (task stub@box-1): gone")
    picked = await remote_resume.pick_up(_run(), _backend(), pool=link)
    assert picked.status == "error" and picked.reply.startswith("machine 'Lab box' (box) no longer runs")


async def test_an_unregistered_machine_is_an_error_in_one_sentence(_pool):
    picked = await remote_resume.pick_up(_run(machine="nobox"), _backend(), pool=_Link())

    assert picked.status == "error" and "cannot be picked up" in picked.reply and "nobox" in picked.reply


async def test_an_agent_that_ended_while_this_computer_was_away_is_said_so_with_its_exit(_pool):
    link = _Link(rc=137)
    picking = asyncio.ensure_future(remote_resume.pick_up(_run(), _backend(), pool=link))
    await asyncio.sleep(0.05)
    link.pipe.says(_chunk("started"))
    await link.pipe.end()

    picked = await asyncio.wait_for(picking, timeout=5)

    assert picked.status == "error"
    assert picked.reply.startswith(
        "the run of 'stub@box' on machine 'Lab box' (box) ended while this computer was away (exit 137)"
    )


async def test_an_answer_that_never_comes_is_waited_out_and_said_so(_pool, monkeypatch):
    monkeypatch.setattr(remote_resume, "_prompt_deadline", lambda configured: 0.1)
    picked = await asyncio.wait_for(remote_resume.pick_up(_run(), _backend(), pool=_Link()), timeout=5)

    assert picked.status == "error" and "was waited out" in picked.reply


async def test_a_turn_that_said_nothing_is_an_error_that_names_where_its_frames_are(_pool):
    link = _Link()
    picking = asyncio.ensure_future(remote_resume.pick_up(_run(), _backend(), pool=link))
    await asyncio.sleep(0.05)
    link.pipe.says({"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}})

    picked = await asyncio.wait_for(picking, timeout=5)

    assert picked.status == "error" and "ended its turn with no content" in picked.reply
    assert "task stub@box-1" in picked.reply


async def test_a_pick_up_beside_a_live_connection_of_the_name_closes_its_own_afterwards(_pool):
    live, _ = await _pool.adopt(name="stub@box", endpoint=_Pipe(), initialize={}, launch="k", config="c", next_id=1)
    link = _Link()
    picking = asyncio.ensure_future(remote_resume.pick_up(_run(), _backend(), pool=link))
    await asyncio.sleep(0.05)
    link.pipe.says(_chunk("pong"))
    link.pipe.says({"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}})

    picked = await asyncio.wait_for(picking, timeout=5)

    assert picked.status == "ok"
    assert _pool.connections("stub@box") == [live], "the live connection stays the pool's"
    assert link.pipe.returncode == 0, "the pick-up's own connection was closed"


async def test_a_connection_closed_while_this_computer_is_leaving_keeps_the_record(_pool):
    """The stop that closes the pools lands under a pick-up: the run is the next start's, not an error."""
    from raven.acp_client import remote

    link = _Link()
    picking = asyncio.ensure_future(remote_resume.pick_up(_run(), _backend(), pool=link))
    await asyncio.sleep(0.05)
    remote.leave_agents_running()
    try:
        await link.pipe.end()
        with pytest.raises(remote_resume.UnreachableError, match="stopped while the run of 'stub@box'"):
            await asyncio.wait_for(picking, timeout=5)
    finally:
        remote.staying()
