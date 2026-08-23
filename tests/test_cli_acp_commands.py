"""``raven acp``'s answering rules, before any ACP method exists.

What is pinned here is which inbound frames get a reply at all. Getting that
wrong is not a cosmetic bug: an unanswered request leaves the client's promise
pending for the life of the session, and an answer to something that was not a
request is a frame the client has nowhere to route.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
from types import SimpleNamespace

from raven.agent.acp import protocol
from raven.cli import acp_commands
from raven.cli.acp_commands import _answer


class TestAnswer:
    def test_a_request_gets_method_not_found_with_its_own_id(self):
        """Not implemented, answered, is a different failure from unanswered.

        The first lets a client tell the user the agent cannot do this. The
        second looks like a hang.
        """
        answer = _answer({"jsonrpc": "2.0", "id": 4, "method": "session/new", "params": {}})

        assert answer == protocol.error_response(4, protocol.METHOD_NOT_FOUND, "session/new is not implemented yet")

    def test_a_string_id_is_answered_with_the_same_string(self):
        """ACP ids are ``str | int``; echoing the wrong type is a correlation
        failure on the client side even though the reply arrived."""
        answer = _answer({"jsonrpc": "2.0", "id": "abc-1", "method": "initialize"})

        assert answer is not None
        assert answer["id"] == "abc-1"

    def test_a_notification_is_not_answered(self):
        assert _answer({"jsonrpc": "2.0", "method": "session/cancel", "params": {}}) is None

    def test_a_response_to_nothing_is_not_answered(self):
        """A frame with an id but no method answers a request this agent never
        sent. There is nothing to correlate it with, so there is nothing to
        say."""
        assert _answer({"jsonrpc": "2.0", "id": 9, "result": {}}) is None
        assert _answer({"jsonrpc": "2.0", "id": 9, "error": {"code": -1, "message": "x"}}) is None

    def test_a_non_string_method_is_rejected_rather_than_formatted_into_a_message(self):
        answer = _answer({"jsonrpc": "2.0", "id": 1, "method": 42})

        assert answer is not None
        assert answer["error"]["code"] == -32600
        assert answer["error"]["message"] == "method must be a string"


class TestServe:
    """The frame loop, with the channel and the log sink stubbed out.

    ``claim_stdout`` has its own tests against real descriptors; what is left to
    pin here is that the loop writes exactly the answers ``_answer`` produced and
    ends when the client closes.
    """

    @staticmethod
    def _stub(monkeypatch, tmp_path, inbound: bytes) -> io.BytesIO:
        written = io.BytesIO()

        @contextlib.contextmanager
        def _claim():
            yield written

        reader = asyncio.StreamReader()
        reader.feed_data(inbound)
        reader.feed_eof()

        @contextlib.asynccontextmanager
        async def _open():
            yield reader

        monkeypatch.setattr(acp_commands, "claim_stdout", _claim)
        monkeypatch.setattr(acp_commands, "redirect_loguru_to_file", lambda *a, **k: tmp_path / "acp.log")
        monkeypatch.setattr(acp_commands, "_open_stdin", _open)
        return written

    async def test_each_request_is_answered_and_notifications_are_not(self, monkeypatch, tmp_path):
        written = self._stub(
            monkeypatch,
            tmp_path,
            b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
            b'{"jsonrpc":"2.0","method":"session/cancel"}\n'
            b'{"jsonrpc":"2.0","id":2,"method":"session/new"}\n',
        )

        await acp_commands._serve()

        lines = written.getvalue().decode("utf-8").splitlines()
        assert [json.loads(line)["id"] for line in lines] == [1, 2], (
            "the notification in the middle must not have produced a frame"
        )

    async def test_a_protocol_error_is_reported_on_the_same_channel(self, monkeypatch, tmp_path):
        """The error has to go out the wire, not the log: the client is what has
        to learn that its frame was unreadable."""
        written = self._stub(monkeypatch, tmp_path, b'garbage\n{"jsonrpc":"2.0","id":3,"method":"x"}\n')

        await acp_commands._serve()

        frames = [json.loads(line) for line in written.getvalue().decode("utf-8").splitlines()]
        assert [f["id"] for f in frames] == [None, 3]
        assert frames[0]["error"]["code"] == -32700

    async def test_no_input_writes_nothing(self, monkeypatch, tmp_path):
        written = self._stub(monkeypatch, tmp_path, b"")

        await acp_commands._serve()

        assert written.getvalue() == b"", "a client that says nothing must be answered with nothing"


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


class TestCallback:
    def test_it_serves_when_no_subcommand_was_given(self, monkeypatch):
        served = []

        async def _fake_serve() -> None:
            served.append(True)

        monkeypatch.setattr(acp_commands, "_serve", _fake_serve)

        acp_commands.acp(SimpleNamespace(invoked_subcommand=None))

        assert served == [True]

    def test_it_defers_to_a_subcommand(self, monkeypatch):
        """``acp`` is a Typer group, so a future ``raven acp <something>`` must
        not also start the server."""

        async def _must_not_run() -> None:
            raise AssertionError("serving despite an explicit subcommand")

        monkeypatch.setattr(acp_commands, "_serve", _must_not_run)

        with contextlib.suppress(RuntimeWarning):
            acp_commands.acp(SimpleNamespace(invoked_subcommand="future-subcommand"))
