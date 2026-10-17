"""Frame reading and relaying for the sub-agent bridge."""

import asyncio
import io
import os
import socket
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from raven.mcp import bridge
from raven.mcp.bridge import iter_raw_frames


async def _reader_from(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_yields_one_frame_per_line_undecoded():
    reader = await _reader_from(b'{"a":1}\n{"b":2}\n')
    assert [f async for f in iter_raw_frames(reader)] == [b'{"a":1}', b'{"b":2}']


@pytest.mark.asyncio
async def test_blank_lines_are_not_frames():
    reader = await _reader_from(b'\n\n{"a":1}\n')
    assert [f async for f in iter_raw_frames(reader)] == [b'{"a":1}']


@pytest.mark.asyncio
async def test_a_frame_far_past_the_stream_reader_default_limit_survives():
    # asyncio.StreamReader.readline caps a line at 64 KiB and raises rather than
    # growing its buffer. An MCP response carrying an image passes that in one
    # hop, so this is the case the hand-rolled buffer exists for.
    big = b'{"blob":"' + b"x" * (1024 * 1024) + b'"}'
    reader = await _reader_from(big + b"\n")
    frames = [f async for f in iter_raw_frames(reader)]
    assert frames == [big]


@pytest.mark.asyncio
async def test_a_frame_over_the_cap_raises_rather_than_buffering_forever():
    reader = await _reader_from(b"x" * 2048 + b"\n")
    with pytest.raises(ValueError, match="frame exceeds"):
        [f async for f in iter_raw_frames(reader, max_frame_bytes=1024)]


@pytest.mark.asyncio
async def test_a_truncated_final_line_is_not_a_frame():
    reader = await _reader_from(b'{"a":1}\n{"b":2')
    assert [f async for f in iter_raw_frames(reader)] == [b'{"a":1}']


@pytest.fixture
def windows_stdin(monkeypatch):
    read_fd, write_fd = os.pipe()
    threads = []

    def make_thread(*args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(bridge, "sys", SimpleNamespace(platform="win32", stdin=SimpleNamespace(fileno=lambda: read_fd)))
    monkeypatch.setattr(bridge, "threading", SimpleNamespace(Thread=make_thread, Event=threading.Event))
    try:
        yield SimpleNamespace(write_fd=write_fd, threads=threads)
    finally:
        os.close(write_fd)
        for thread in threads:
            thread.join(timeout=2)
        os.close(read_fd)
        assert all(not thread.is_alive() for thread in threads)


async def test_windows_stdin_reads_a_frame_while_the_pipe_remains_open(windows_stdin):
    payload = b'{"id":1,"method":"ping"}\n'
    async with bridge._stdin_reader() as reader:
        os.write(windows_stdin.write_fd, payload)
        assert await asyncio.wait_for(reader.readexactly(len(payload)), 2) == payload
        assert windows_stdin.threads[0].daemon
    assert not reader.at_eof(), "the consumer can leave while inherited stdin is still open"


@pytest.mark.parametrize("ending", ["eof", "read-error"])
async def test_windows_stdin_ends_the_reader_when_the_input_ends(windows_stdin, monkeypatch, ending):
    def read(fd, size):
        if ending == "read-error":
            raise OSError("stdin closed")
        return b""

    monkeypatch.setattr(bridge, "os", SimpleNamespace(read=read))
    async with bridge._stdin_reader() as reader:
        assert await asyncio.wait_for(reader.read(), 2) == b""


@pytest.mark.parametrize("ending", ["consume", "close"])
async def test_windows_stdin_backpressure_bounds_buffering_and_releases_the_worker(windows_stdin, monkeypatch, ending):
    chunk = b"x" * bridge._READ_CHUNK
    chunks = iter([chunk] * 8)
    exhausted = threading.Event()

    def read(fd, size):
        data = next(chunks, b"")
        if not data:
            exhausted.set()
        return data

    monkeypatch.setattr(bridge, "os", SimpleNamespace(read=read))
    async with bridge._stdin_reader() as reader:
        async with asyncio.timeout(2):
            while not reader._paused and not exhausted.is_set():
                await asyncio.sleep(0)
        assert not exhausted.is_set(), "a stalled consumer must stop the stdin producer"
        assert len(reader._buffer) <= 4 * bridge._READ_CHUNK
        if ending == "consume":
            assert await asyncio.wait_for(reader.readexactly(len(chunk) * 8), 2) == chunk * 8
            assert await asyncio.wait_for(reader.read(), 2) == b""

    for thread in windows_stdin.threads:
        thread.join(timeout=2)
        assert not thread.is_alive(), "closing a paused reader must release its worker without waiting for stdin EOF"
    assert exhausted.is_set() is (ending == "consume")


async def test_windows_stdin_backpressure_also_bounds_pending_callbacks(windows_stdin, monkeypatch):
    """Hold the loop so buffering cannot hide in callbacks waiting to run."""
    chunk = b"x" * bridge._READ_CHUNK
    first_read, read_ahead = threading.Event(), threading.Event()

    def read(fd, size):
        if first_read.is_set():
            read_ahead.set()
            return b""
        first_read.set()
        return chunk

    monkeypatch.setattr(bridge, "os", SimpleNamespace(read=read))
    async with bridge._stdin_reader() as reader:
        assert first_read.wait(timeout=2)
        assert not read_ahead.wait(timeout=0.1), "stdin must wait for loop delivery before reading again"
        assert await asyncio.wait_for(reader.readexactly(len(chunk)), 2) == chunk


async def test_posix_stdin_closes_its_transport_when_the_consumer_fails(monkeypatch):
    closed = []
    stdin = object()

    async def connect(factory, stream):
        assert stream is stdin
        protocol = factory()
        protocol.data_received(b"request\n")
        return SimpleNamespace(close=lambda: closed.append(True)), protocol

    monkeypatch.setattr(bridge, "sys", SimpleNamespace(platform="linux", stdin=stdin))
    monkeypatch.setattr(asyncio.get_running_loop(), "connect_read_pipe", connect)
    with pytest.raises(RuntimeError, match="consumer stopped"):
        async with bridge._stdin_reader() as reader:
            assert await reader.readline() == b"request\n"
            raise RuntimeError("consumer stopped")
    assert closed == [True]


class _Output(io.BytesIO):
    """Expose the moment a frame becomes visible to the bridge's stdio peer."""

    def __init__(self):
        super().__init__()
        self.flushed = asyncio.Event()

    def flush(self):
        super().flush()
        self.flushed.set()


@pytest.fixture
async def bridge_io(monkeypatch):
    accepted = asyncio.get_running_loop().create_future()
    server = await asyncio.start_server(lambda reader, writer: accepted.set_result((reader, writer)), "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    peer_reader, peer_writer = await accepted
    stdin = asyncio.StreamReader()
    entered, released = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def open_stdin():
        entered.set()
        try:
            yield stdin
        finally:
            released.set()

    connect = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(bridge, "open_endpoint", connect)
    monkeypatch.setattr(bridge, "_stdin_reader", open_stdin)
    try:
        yield SimpleNamespace(
            stdin=stdin,
            writer=writer,
            peer_reader=peer_reader,
            peer_writer=peer_writer,
            entered=entered,
            released=released,
            connect=connect,
            stdout=_Output(),
        )
    finally:
        writer.close()
        peer_writer.close()
        await asyncio.gather(writer.wait_closed(), peer_writer.wait_closed(), return_exceptions=True)
        server.close()
        await server.wait_closed()


async def test_bridge_relays_both_directions_and_exits_on_endpoint_eof_with_stdin_open(bridge_io):
    request = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    response = b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'
    task = asyncio.create_task(bridge.run_bridge("endpoint", bridge_io.stdout))
    try:
        bridge_io.stdin.feed_data(request)
        assert await asyncio.wait_for(bridge_io.peer_reader.readline(), 2) == request
        bridge_io.peer_writer.write(response)
        await bridge_io.peer_writer.drain()
        await asyncio.wait_for(bridge_io.stdout.flushed.wait(), 2)
        assert bridge_io.stdout.getvalue() == response
        bridge_io.peer_writer.close()
        assert await asyncio.wait_for(task, 2) == 0
        assert not bridge_io.stdin.at_eof()
        assert bridge_io.writer.is_closing() and bridge_io.released.is_set()
        bridge_io.connect.assert_awaited_once_with("endpoint")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("ending", ["stdin-eof", "cancel"])
async def test_bridge_reaps_both_pumps_and_closes_the_endpoint_on_exit(bridge_io, ending):
    before = asyncio.all_tasks()
    task = asyncio.create_task(bridge.run_bridge("endpoint", bridge_io.stdout))
    try:
        await asyncio.wait_for(bridge_io.entered.wait(), 2)
        if ending == "stdin-eof":
            bridge_io.stdin.feed_eof()
            assert await asyncio.wait_for(task, 2) == 0
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert bridge_io.released.is_set()
        assert bridge_io.writer.is_closing()
        assert await asyncio.wait_for(bridge_io.peer_reader.read(), 2) == b""
        assert not (asyncio.all_tasks() - before), "no bridge pump may outlive the run"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_bridge_closes_the_endpoint_when_stdin_setup_fails(bridge_io, monkeypatch, capsys):
    @asynccontextmanager
    async def failed_stdin():
        raise OSError("cannot attach stdin")
        yield

    monkeypatch.setattr(bridge, "_stdin_reader", failed_stdin)
    assert await bridge.run_bridge("endpoint", bridge_io.stdout) == 1
    assert "cannot attach stdin" in capsys.readouterr().err
    assert bridge_io.writer.is_closing()
    assert await asyncio.wait_for(bridge_io.peer_reader.read(), 2) == b""


async def test_bridge_reports_a_failed_output_pump_and_reaps_the_connection(bridge_io, capsys):
    class BrokenOutput:
        def write(self, data):
            raise BrokenPipeError("stdout closed")

    task = asyncio.create_task(bridge.run_bridge("endpoint", BrokenOutput()))
    try:
        await asyncio.wait_for(bridge_io.entered.wait(), 2)
        bridge_io.peer_writer.write(b'{"result":{}}\n')
        await bridge_io.peer_writer.drain()
        assert await asyncio.wait_for(task, 2) == 1
        assert "BrokenPipeError: stdout closed" in capsys.readouterr().err
        assert bridge_io.released.is_set() and bridge_io.writer.is_closing()
        assert await asyncio.wait_for(bridge_io.peer_reader.read(), 2) == b""
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("ending", ["cancel", "output-error"])
async def test_bridge_exits_when_a_stalled_endpoint_has_pending_writes(bridge_io, monkeypatch, ending):
    monkeypatch.setattr(bridge, "_CLOSE_TIMEOUT_S", 0.05, raising=False)
    bridge_io.writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    bridge_io.peer_writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)

    class BrokenOutput:
        def write(self, data):
            raise BrokenPipeError("stdout closed")

    output = BrokenOutput() if ending == "output-error" else bridge_io.stdout
    task = asyncio.create_task(bridge.run_bridge("endpoint", output))
    try:
        await asyncio.wait_for(bridge_io.entered.wait(), 2)
        # The endpoint stops reading before anything is sent, so what the
        # bridge writes can never drain. Left to stop by its own flow control,
        # the endpoint first trickles 128 KiB through those 4 KiB buffers,
        # which took 1.87 s on Linux.
        bridge_io.peer_writer.transport.pause_reading()
        frame = b'{"data":"' + b"x" * 65000 + b'"}\n'
        bridge_io.stdin.feed_data(frame * 128)
        async with asyncio.timeout(2):
            while bridge_io.writer.transport.get_write_buffer_size() == 0:
                await asyncio.sleep(0)
        await asyncio.sleep(0.1)
        assert bridge_io.writer.transport.get_write_buffer_size() > 0
        if ending == "cancel":
            task.cancel()
        else:
            bridge_io.peer_writer.write(b'{"result":{}}\n')
            await bridge_io.peer_writer.drain()
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, "bridge teardown must not depend on the endpoint reading again"
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                task.result()
        else:
            assert task.result() == 1
        assert bridge_io.released.is_set()
        assert bridge_io.writer.transport.get_write_buffer_size() == 0
    finally:
        bridge_io.writer.transport.abort()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
