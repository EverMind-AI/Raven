"""AcpClient: what a connection taken over from a process before this one needs -- ids to go on from, an answer to await, the id of what goes out."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from raven.acp_client import remote
from raven.acp_client.client import AcpClient
from raven.acp_client.protocol import AcpConnectionError, AcpTimeoutError


class _Pipe:
    """An endpoint fed by the test: what the agent says goes in, what raven says comes out."""

    pid = 1

    def __init__(self) -> None:
        self._in: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._err: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.written: list[dict[str, Any]] = []
        self.returncode: int | None = None
        self._done = asyncio.Event()

    def describe(self) -> str:
        return "a pipe"

    async def write(self, data: bytes) -> None:
        self.written.append(json.loads(data.decode().strip()))

    async def readline(self) -> bytes:
        item = await self._in.get()
        return b"" if item is None else item

    async def readerr(self) -> bytes:
        item = await self._err.get()
        return b"" if item is None else item

    async def end(self) -> None:
        self.returncode = 0
        self._in.put_nowait(None)
        self._err.put_nowait(None)
        self._done.set()

    async def wait(self) -> int | None:
        await self._done.wait()
        return self.returncode

    def ended(self) -> str | None:
        return None

    def release(self) -> None:
        pass

    def says(self, frame: dict[str, Any]) -> None:
        self._in.put_nowait(json.dumps(frame).encode() + b"\n")


@pytest.fixture
async def pipe():
    endpoint = _Pipe()
    client = AcpClient.over(endpoint, name="taken-over")
    yield endpoint, client
    await client.close()


async def test_an_answer_to_a_request_another_process_sent_is_awaited_here(pipe):
    endpoint, client = pipe
    waiting = asyncio.ensure_future(client.await_response(7, method="session/prompt", session="s1", timeout=5))
    await asyncio.sleep(0.01)
    assert client.prompting, "the awaited prompt is the turn in flight"

    endpoint.says({"jsonrpc": "2.0", "id": 7, "result": {"stopReason": "end_turn"}})

    assert await waiting == {"stopReason": "end_turn"}
    assert endpoint.written == [], "nothing was sent to get it"
    assert not client.prompting


async def test_an_answer_that_does_not_come_in_time_is_a_timeout_and_a_closed_connection_is_said_so(pipe):
    endpoint, client = pipe
    with pytest.raises(AcpTimeoutError, match="session/prompt timed out"):
        await client.await_response(8, method="session/prompt", timeout=0.05)

    await client.close()
    with pytest.raises(AcpConnectionError, match="connection is not open"):
        await client.await_response(9, method="session/prompt")


async def test_request_ids_go_on_from_where_the_other_process_left_them(pipe):
    endpoint, client = pipe
    assert client.next_request_id == 1
    client.continue_ids_from(41)
    assert client.next_request_id == 41
    client.continue_ids_from(3)
    assert client.next_request_id == 41, "never back"
    gone_out: list[int] = []

    asked = asyncio.ensure_future(client.request("ping", {}, timeout=5, sent=gone_out.append))
    await asyncio.sleep(0.01)
    endpoint.says({"jsonrpc": "2.0", "id": 41, "result": {}})

    assert await asked == {} and gone_out == [41] and endpoint.written[0]["id"] == 41


async def test_a_cancelled_prompt_is_not_cancelled_on_the_agent_while_this_process_is_leaving(pipe):
    endpoint, client = pipe
    endpoint.stays = True
    asked = asyncio.ensure_future(client.request("session/prompt", {"sessionId": "s1"}, timeout=5, cancel_session="s1"))
    await asyncio.sleep(0.01)

    asked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asked

    assert [frame["method"] for frame in endpoint.written] == ["session/prompt"], "no session/cancel went out"


def test_leaving_is_said_once_and_can_be_unsaid():
    assert remote.leaving() is False
    remote.leave_agents_running()
    try:
        assert remote.leaving() is True
    finally:
        remote.staying()
    assert remote.leaving() is False


async def test_an_answer_awaited_on_a_connection_whose_far_side_ended_is_said_so():
    endpoint = _Pipe()
    client = AcpClient.over(endpoint, name="ended")
    await endpoint.end()
    await asyncio.sleep(0.05)

    with pytest.raises(AcpConnectionError, match="connection ended"):
        await client.await_response(3, method="session/prompt")
    await client.close()


async def test_a_second_adopted_connection_of_a_name_serves_its_pick_up_only():
    from raven.acp_client.pool import close_pool, get_pool

    pool = get_pool()
    try:
        first, held = await pool.adopt(
            name="adopted", endpoint=_Pipe(), initialize={}, launch="k", config="c", next_id=50
        )
        second, held_too = await pool.adopt(
            name="adopted", endpoint=_Pipe(), initialize={}, launch="k", config="c", next_id=50
        )

        assert held is True and held_too is False
        assert pool.connections("adopted") == [first] and first.client.next_request_id == 50
        await second.client.close()
    finally:
        await close_pool()


def test_a_run_is_tracked_only_over_a_channel_to_a_machine_and_a_record_that_cannot_be_written_is_named(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from loguru import logger

    from raven.acp_client import remote_runs
    from raven.acp_client.acp_agent import AcpAgentBackend

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    backend = AcpAgentBackend(name="stub@box", command="stub", machine="box")
    connection = SimpleNamespace(initialize={}, launch_key="k", config_key="c")
    track = dict(session_id="s", skey="tui:s1", handle="h", task_id="t", session_cwd="/w")

    local = SimpleNamespace(endpoint=SimpleNamespace(pid=1), next_request_id=1)
    assert backend._track_on_machine(local, connection, **track) is None, "a child process here is no run on a machine"

    channel = SimpleNamespace(task="stub@box-1", watchers=[], received=0, stderr_received=5)
    over_channel = SimpleNamespace(endpoint=channel, next_request_id=1)
    tracker = backend._track_on_machine(over_channel, connection, **track)
    assert tracker is not None and channel.watchers == [tracker.received]
    assert [run.task for run in remote_runs.pending()] == ["stub@box-1"]
    assert tracker.run.stderr_received == 5
    channel.stderr_received = 9
    tracker.prompt_sent(1)
    assert tracker.run.stderr_received == 9, "the endpoint's stderr count, read as the prompt goes out"
    backend._untrack(over_channel, tracker, keep=False)
    assert channel.watchers == [] and remote_runs.pending() == []
    backend._untrack(over_channel, tracker, keep=False)  # a watcher already gone is not an error

    def refuse(run, **_kw):
        raise OSError("read-only home")

    records: list[str] = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="WARNING")
    try:
        monkeypatch.setattr(remote_runs, "Tracker", refuse)
        assert backend._track_on_machine(over_channel, connection, **track) is None
    finally:
        logger.remove(sink)
    assert any("could not be recorded: read-only home" in message for message in records)
