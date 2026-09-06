"""``raven acp``'s process shell: the channel, the reader, and the callback.

What the command owns is narrow on purpose -- claim fd 1, send the logs
elsewhere, open stdin, hand both to the server -- so what is pinned here is that
each of those happens and is undone. The protocol itself is
``tests/test_acp_methods.py``; the descriptor surgery is
``tests/test_acp_stdio.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
import threading
from types import SimpleNamespace

import pytest
import typer

from raven.cli import acp_commands


class TestServe:
    """The shell around the server, with the descriptors and the log stubbed.

    ``claim_stdout`` has its own tests against real descriptors and the protocol
    has its own module; what is left to pin here is that the writer and the
    reader the server is handed are the ones this command set up, and that both
    are released afterwards.
    """

    @staticmethod
    def _stub(monkeypatch, tmp_path, inbound: bytes) -> tuple[io.BytesIO, list]:
        written = io.BytesIO()
        claimed = []
        released = []

        @contextlib.contextmanager
        def _claim():
            claimed.append(written)
            try:
                yield written
            finally:
                released.append(written)

        reader = asyncio.StreamReader()
        reader.feed_data(inbound)
        reader.feed_eof()

        @contextlib.asynccontextmanager
        async def _open():
            yield reader

        monkeypatch.setattr(acp_commands, "claim_stdout", _claim)
        monkeypatch.setattr(acp_commands, "redirect_loguru_to_file", lambda *a, **k: tmp_path / "acp.log")
        monkeypatch.setattr(acp_commands, "_open_stdin", _open)
        return written, released

    async def test_the_server_is_handed_the_claimed_writer_and_the_opened_reader(self, monkeypatch, tmp_path):
        written, released = self._stub(monkeypatch, tmp_path, b'{"jsonrpc":"2.0","id":1,"method":"x"}\n')
        seen = {}

        async def _serve(reader, out):
            seen["out"] = out
            seen["first"] = await reader.readline()

        monkeypatch.setattr(acp_commands, "serve", _serve)

        await acp_commands._serve()

        assert seen["out"] is written, "the server must write to the claimed descriptor, not to sys.stdout"
        assert seen["first"] == b'{"jsonrpc":"2.0","id":1,"method":"x"}\n'
        assert released == [written], "the channel must be given back even on the happy path"

    async def test_the_channel_is_released_when_the_server_raises(self, monkeypatch, tmp_path):
        """Without this, a startup failure leaves fd 1 pointing at stderr for
        whatever runs next in the process -- and in a test run, for the rest of
        the session."""
        written, released = self._stub(monkeypatch, tmp_path, b"")

        async def _explode(reader, out):
            raise RuntimeError("engine failed to build")

        monkeypatch.setattr(acp_commands, "serve", _explode)

        try:
            await acp_commands._serve()
        except RuntimeError as exc:
            assert str(exc) == "engine failed to build"
        else:
            raise AssertionError("the failure must propagate; a silent exit reads as a clean shutdown")

        assert released == [written]

    async def test_crash_handlers_are_installed_before_the_channel_is_claimed(self, monkeypatch, tmp_path):
        """Ordering, not existence: a failure inside ``claim_stdout`` itself is
        exactly the one that needs the hook already in place."""
        order = []
        self._stub(monkeypatch, tmp_path, b"")
        real_claim = acp_commands.claim_stdout

        @contextlib.contextmanager
        def _claim():
            order.append("claim")
            with real_claim() as out:
                yield out

        monkeypatch.setattr(acp_commands, "claim_stdout", _claim)
        monkeypatch.setattr(acp_commands, "install_crash_handlers", lambda: order.append("handlers"))

        async def _serve(reader, out):
            return None

        monkeypatch.setattr(acp_commands, "serve", _serve)

        await acp_commands._serve()

        assert order == ["handlers", "claim"]


class TestOpenStdin:
    async def test_it_reads_the_process_stdin(self, monkeypatch):
        """Pinned because the wiring is easy to get subtly wrong: attaching to
        ``sys.stdin``'s buffer rather than the object, or to a descriptor that
        was already consumed, both yield a reader that simply never delivers."""
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"payload\n")
        os.close(write_fd)
        monkeypatch.setattr(sys, "stdin", os.fdopen(read_fd, "rb"))

        async with acp_commands._open_stdin() as reader:
            assert await reader.readline() == b"payload\n"

    async def test_the_transport_is_closed_on_the_way_out(self, monkeypatch):
        """An unclosed read transport is collected with the loop still holding
        its descriptor, which surfaces later as an unraisable error with no
        caller to report it to."""
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        monkeypatch.setattr(sys, "stdin", os.fdopen(read_fd, "rb"))

        captured = []
        real = asyncio.get_running_loop().connect_read_pipe

        async def _spy(factory, pipe):
            transport, proto = await real(factory, pipe)
            captured.append(transport)
            return transport, proto

        monkeypatch.setattr(asyncio.get_running_loop(), "connect_read_pipe", _spy)

        async with acp_commands._open_stdin():
            pass

        assert captured and captured[0].is_closing()

    async def test_a_regular_file_on_stdin_falls_back_to_a_thread(self, monkeypatch, tmp_path):
        """``connect_read_pipe`` refuses a regular file outright, so
        ``raven acp < script.jsonl`` -- how anyone first tries this by hand --
        would die with a traceback before reading a byte."""
        script = tmp_path / "script.jsonl"
        script.write_bytes(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
        handle = script.open("rb")
        monkeypatch.setattr(sys, "stdin", handle)

        try:
            async with acp_commands._open_stdin() as reader:
                assert await reader.readline() == b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
                assert await reader.read() == b"", "the fallback must also deliver EOF"
        finally:
            handle.close()

    async def test_the_fallback_thread_does_not_hold_the_loop_open(self, monkeypatch):
        """Measured, not predicted. ``run_in_executor`` was the first shape, and
        asyncio waits for the default executor when it closes the loop -- so an
        uncancellable blocking read on an idle stream became a process that would
        not exit. This test hung until it was killed. A daemon thread has no such
        hold, and nothing here waits for it."""
        read_fd, write_fd = os.pipe()
        monkeypatch.setattr(sys, "stdin", os.fdopen(read_fd, "rb"))
        loop = asyncio.get_running_loop()

        async def _refuse(factory, pipe):
            raise ValueError("Pipe transport is for pipes/sockets only")

        monkeypatch.setattr(loop, "connect_read_pipe", _refuse)

        async with acp_commands._open_stdin() as reader:
            os.write(write_fd, b"line\n")
            assert await asyncio.wait_for(reader.readline(), timeout=5.0) == b"line\n"

        thread = next((t for t in threading.enumerate() if t.name == "acp-stdin"), None)
        assert thread is not None and thread.daemon, "a non-daemon feeder blocks interpreter exit"
        os.close(write_fd)

    async def test_a_text_mode_stdin_is_encoded_rather_than_refused(self, monkeypatch):
        """An embedded interpreter can hand over a stdin with no ``buffer`` at
        all, whose reads return ``str``. Feeding that to a ``StreamReader``
        raises, so it is encoded here instead."""
        loop = asyncio.get_running_loop()

        async def _refuse(factory, pipe):
            raise ValueError("not a pipe")

        class _TextStdin:
            def __init__(self):
                self._left = ["hello\n", ""]

            def read(self, _n):
                return self._left.pop(0)

        monkeypatch.setattr(loop, "connect_read_pipe", _refuse)
        monkeypatch.setattr(sys, "stdin", _TextStdin())

        async with acp_commands._open_stdin() as reader:
            assert await asyncio.wait_for(reader.readline(), timeout=5.0) == b"hello\n"

    async def test_a_read_failure_is_reported_as_end_of_input(self, monkeypatch):
        """A truncated session and a finished one look identical to the frame
        loop, so the loop has to end rather than wait -- and the reason has to be
        somewhere."""
        loop = asyncio.get_running_loop()

        async def _refuse(factory, pipe):
            raise ValueError("not a pipe")

        class _Angry:
            def read(self, _n):
                raise OSError("device gone")

        monkeypatch.setattr(loop, "connect_read_pipe", _refuse)
        monkeypatch.setattr(sys, "stdin", _Angry())

        async with acp_commands._open_stdin() as reader:
            assert await asyncio.wait_for(reader.read(), timeout=5.0) == b""


class TestCallback:
    def test_it_serves_when_no_subcommand_was_given(self, monkeypatch):
        served = []

        async def _fake_serve() -> None:
            served.append(True)

        monkeypatch.setattr(acp_commands, "_serve", _fake_serve)

        acp_commands.acp(SimpleNamespace(invoked_subcommand=None))

        assert served == [True]

    def test_a_crash_is_reported_briefly_rather_than_as_a_rich_traceback(self, monkeypatch):
        """Typer's own handler renders a rich traceback with ``show_locals`` on --
        measured at 228 lines of stderr carrying the value of every local in every
        frame, on the stream an ACP client displays. The full traceback goes to
        the log file, where the sink is configured not to annotate it."""

        async def _explode() -> None:
            raise RuntimeError("engine failed")

        monkeypatch.setattr(acp_commands, "_serve", _explode)

        with pytest.raises(typer.Exit) as caught:
            acp_commands.acp(SimpleNamespace(invoked_subcommand=None))

        assert caught.value.exit_code == 1

    def test_it_defers_to_a_subcommand(self, monkeypatch):
        """``acp`` is a Typer group, so a future ``raven acp <something>`` must
        not also start the server."""

        async def _must_not_run() -> None:
            raise AssertionError("serving despite an explicit subcommand")

        monkeypatch.setattr(acp_commands, "_serve", _must_not_run)

        with contextlib.suppress(RuntimeWarning):
            acp_commands.acp(SimpleNamespace(invoked_subcommand="future-subcommand"))
