"""The ACP stdio channel: fd hygiene, framing, and recovery from bad input.

These pin the two properties the protocol cannot be spoken without. First, that
a write to stdout by any route lands in the log rather than in the frame stream.
Second, that a frame the agent cannot read produces an answer and a
resynchronised stream, rather than a dead agent and a client holding a promise
that will never settle.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from raven.acp.stdio import (
    INVALID_REQUEST,
    PARSE_ERROR,
    claim_stdout,
    read_frames,
    write_frame,
)


@contextlib.contextmanager
def _fds_to_files(out: Path, err: Path):
    """Point fd 1 and fd 2 at real files, so the test can read what landed where.

    pytest replaces ``sys.stdout`` with its own capture object, which never
    touches fd 1 -- so the descriptor has to be redirected here for the test to
    be about the thing the module actually moves.
    """
    saved_out, saved_err = os.dup(1), os.dup(2)
    out_fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    err_fd = os.open(str(err), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.dup2(out_fd, 1)
        os.dup2(err_fd, 2)
        os.close(out_fd)
        os.close(err_fd)
        yield
    finally:
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)


def _open_fd_count() -> int:
    """How many descriptors this process holds.

    A leak is invisible in behaviour until the process runs out, so it has to be
    counted rather than inferred.
    """
    count = 0
    for fd in range(3, 256):
        try:
            os.fstat(fd)
        except OSError:
            continue
        count += 1
    return count


def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def _collect(data: bytes, **kwargs: Any) -> tuple[list[dict], list[dict]]:
    errors: list[dict] = []
    frames = [f async for f in read_frames(_reader(data), errors.append, **kwargs)]
    return frames, errors


class TestClaimStdout:
    async def test_a_raw_write_to_fd_1_lands_on_stderr(self, tmp_path):
        """The descriptor is the shared resource, so moving it is the whole fix.

        ``os.write(1, ...)`` is the case that matters: it is what an embedded
        logger or a C extension does, and it is exactly what replacing
        ``sys.stdout`` would fail to catch.
        """
        out, err = tmp_path / "out", tmp_path / "err"
        with _fds_to_files(out, err):
            with claim_stdout() as writer:
                os.write(1, b"noise from somebody else\n")
                write_frame(writer, {"jsonrpc": "2.0", "id": 1, "result": {}})

        assert json.loads(out.read_text()) == {"jsonrpc": "2.0", "id": 1, "result": {}}
        assert "noise from somebody else" in err.read_text()

    async def test_fd_1_is_restored_afterwards(self, tmp_path):
        """The block is borrowing the descriptor, not keeping it."""
        out, err = tmp_path / "out", tmp_path / "err"
        with _fds_to_files(out, err):
            with claim_stdout():
                pass
            os.write(1, b"after the block\n")

        assert "after the block" in out.read_text()
        assert "after the block" not in err.read_text()

    async def test_a_buffered_python_write_does_not_survive_the_restore(self, tmp_path):
        """The stray ``print`` case, which the raw-write test cannot reach.

        ``sys.stdout`` is block-buffered when stdout is a pipe, so a ``print``
        inside the block leaves its bytes in that object's buffer. If fd 1 were
        restored before that buffer was flushed, those bytes would be written at
        interpreter shutdown -- when fd 1 is the protocol channel again. The
        route this covers is the one the module docstring names first, and it is
        invisible to the integration test because nothing there prints.
        """
        out, err = tmp_path / "out", tmp_path / "err"
        with _fds_to_files(out, err):
            # A real buffered writer on fd 1, standing in for the interpreter's
            # own: pytest's replacement never touches the descriptor.
            stdout_on_fd_1 = io.TextIOWrapper(io.FileIO(1, "wb", closefd=False), line_buffering=False)
            saved = sys.stdout
            sys.stdout = stdout_on_fd_1
            try:
                with claim_stdout() as writer:
                    print("stray debug print")
                    write_frame(writer, {"jsonrpc": "2.0", "id": 1, "result": {}})
                stdout_on_fd_1.flush()
            finally:
                sys.stdout = saved

        assert "stray debug print" not in out.read_text(), "a buffered print reached the protocol channel"
        assert "stray debug print" in err.read_text()
        assert json.loads(out.read_text()) == {"jsonrpc": "2.0", "id": 1, "result": {}}

    async def test_the_channel_raises_rather_than_truncating_a_frame_it_cannot_take(self, tmp_path):
        """A dropped short-write return value is a silently truncated frame.

        A raw ``FileIO.write`` is one ``write(2)`` and may write less than it was
        given; the next frame then concatenates onto the tail of this one, and
        the client sees neither an error nor a valid frame. Buffered plus flush
        turns the same condition into an exception the caller can act on.

        fd 1 is a non-blocking pipe here because that is the shape
        ``_open_stdin`` produces: ``connect_read_pipe`` sets ``O_NONBLOCK`` on
        fd 0's open file description, and a shell hands a foreground job fd 0, 1
        and 2 as dups of one description. The writer under test is the one
        ``claim_stdout`` builds, not one this test opened, so the assertion is
        about the channel rather than about ``BufferedWriter``.
        """
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        err = tmp_path / "err"
        err_fd = os.open(str(err), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        saved_out, saved_err = os.dup(1), os.dup(2)
        try:
            os.dup2(write_fd, 1)
            os.dup2(err_fd, 2)
            payload = {"jsonrpc": "2.0", "id": 1, "result": {"blob": "x" * (1024 * 1024)}}
            with claim_stdout() as writer:
                with pytest.raises(BlockingIOError):
                    write_frame(writer, payload)
        finally:
            os.dup2(saved_out, 1)
            os.dup2(saved_err, 2)
            for fd in (saved_out, saved_err, err_fd, read_fd, write_fd):
                with contextlib.suppress(OSError):
                    os.close(fd)

    async def test_a_failed_setup_restores_fd_1_and_keeps_no_descriptor(self, tmp_path):
        """The setup has to be as symmetric as the teardown.

        Two real failures live between ``dup(1)`` and the yield: ``dup2`` raises
        ``EBADF`` when fd 2 is closed, which ``raven acp 2>&-`` does, and
        ``fdopen`` can fail after fd 1 has already been moved. Unhandled, the
        caller would get an exception while the process carried on with its
        stdout pointed at stderr and nobody holding the original descriptor.

        The injected failure is the ``fdopen`` one, because it is the worse of
        the two -- fd 1 is already moved by then -- and because closing fd 2
        in-process fights pytest's own capture rather than testing this module.
        The ``EBADF`` behaviour was confirmed separately in a clean interpreter.
        """
        out, err = tmp_path / "out", tmp_path / "err"

        def _fdopen_fails(*a, **k):
            raise OSError(24, "Too many open files")

        with _fds_to_files(out, err):
            before = _open_fd_count()
            with patch("os.fdopen", _fdopen_fails):
                with pytest.raises(OSError):
                    with claim_stdout():
                        pytest.fail("must not yield when it could not take the channel")

            leaked = _open_fd_count() - before
            os.write(1, b"fd 1 still points at the protocol channel\n")

        assert leaked == 0, f"the failed setup leaked {leaked} descriptor(s)"
        assert "fd 1 still points at the protocol channel" in out.read_text()
        assert "fd 1 still points" not in err.read_text(), "fd 1 was left pointing at stderr"

    async def test_frames_are_not_left_in_a_buffer(self, tmp_path):
        """A frame still buffered when the client reads is indistinguishable
        from a hang, so ``write_frame`` flushes rather than leaving it to the
        caller to remember, or to the next frame to push this one out."""
        out, err = tmp_path / "out", tmp_path / "err"
        with _fds_to_files(out, err):
            with claim_stdout() as writer:
                write_frame(writer, {"jsonrpc": "2.0", "id": 7, "result": {"a": 1}})
                # Read it back before the context manager gets a chance to flush.
                assert out.read_bytes().endswith(b"\n"), "frame did not reach the fd"


class TestReadFrames:
    async def test_one_frame_per_line(self):
        data = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n{"jsonrpc":"2.0","id":2,"method":"session/new"}\n'
        frames, errors = await _collect(data)
        assert [f["id"] for f in frames] == [1, 2]
        assert errors == []

    async def test_non_ascii_survives_the_round_trip(self, tmp_path):
        """``encode`` sends UTF-8 rather than escapes, so the decoder has to
        agree; a mismatch here would corrupt every non-English prompt."""
        out, err = tmp_path / "out", tmp_path / "err"
        payload = {"jsonrpc": "2.0", "id": 1, "params": {"text": "你好, world"}}
        with _fds_to_files(out, err):
            with claim_stdout() as writer:
                write_frame(writer, payload)
        frames, errors = await _collect(out.read_bytes())
        assert frames == [payload]
        assert errors == []

    async def test_blank_lines_are_not_frames(self):
        frames, errors = await _collect(b'\n\n{"jsonrpc":"2.0","id":1}\n\n')
        assert [f["id"] for f in frames] == [1]
        assert errors == [], "a blank line is not something to complain to the client about"

    async def test_a_non_json_line_is_answered_and_the_stream_continues(self):
        """The answer matters as much as the survival: a client that gets no
        response keeps its promise pending forever."""
        frames, errors = await _collect(b'not json at all\n{"jsonrpc":"2.0","id":2}\n')
        assert [f["id"] for f in frames] == [2]
        assert len(errors) == 1
        assert errors[0]["id"] is None, "the id lived in the line that could not be read"
        assert errors[0]["error"]["code"] == PARSE_ERROR

    async def test_a_json_scalar_is_not_a_frame(self):
        frames, errors = await _collect(b'"just a string"\n')
        assert frames == []
        assert errors[0]["error"]["code"] == PARSE_ERROR

    async def test_an_oversized_frame_is_answered_and_the_next_one_survives(self):
        """The resynchronisation is the load-bearing half.

        Answering the oversized frame but resuming mid-frame would leave every
        following frame garbage, so the test asserts on what comes *after* the
        oversized one rather than on the error alone.
        """
        huge = b'{"jsonrpc":"2.0","id":1,"params":{"x":"' + b"a" * 4096 + b'"}}'
        data = huge + b'\n{"jsonrpc":"2.0","id":99,"method":"session/new"}\n'
        frames, errors = await _collect(data, max_frame_bytes=1024)

        assert [f["id"] for f in frames] == [99], "the frame after the oversized one was lost"
        assert len(errors) == 1
        assert errors[0]["id"] is None
        assert errors[0]["error"]["code"] == INVALID_REQUEST
        assert "1024" in errors[0]["error"]["message"]

    async def test_an_oversized_frame_spanning_many_chunks_still_resyncs(self):
        """The cap can be exceeded several reads before the newline arrives, and
        the drop must not be re-reported once per chunk."""
        huge = b"x" * (300 * 1024)
        data = huge + b'\n{"jsonrpc":"2.0","id":5}\n'
        frames, errors = await _collect(data, max_frame_bytes=64 * 1024)

        assert [f["id"] for f in frames] == [5]
        assert len(errors) == 1, f"expected one report for one oversized frame, got {len(errors)}"

    async def test_a_truncated_final_line_is_dropped_not_guessed_at(self):
        """A frame with no newline is the client dying mid-write. Acting on the
        fragment is how half a tool call gets executed."""
        frames, errors = await _collect(b'{"jsonrpc":"2.0","id":1}\n{"jsonrpc":"2.0","id":2,"met')
        assert [f["id"] for f in frames] == [1]
        assert errors == []

    async def test_eof_with_no_data_ends_quietly(self):
        frames, errors = await _collect(b"")
        assert frames == []
        assert errors == []


class TestInvalidUtf8:
    async def test_invalid_utf8_is_reported_rather_than_substituted(self):
        """``errors="replace"`` would let a corrupted frame through.

        The offending bytes become U+FFFD, and if they sat inside a JSON string
        the frame still parses -- so the request would be accepted and the
        corruption would travel into whatever the method does with that string.
        Reporting it is the only answer that does not act on damaged input.
        """
        line = b'{"jsonrpc":"2.0","id":1,"method":"session/prompt","params":{"text":"\xff\xfe"}}'
        frames, errors = await _collect(line + b'\n{"jsonrpc":"2.0","id":2}\n')

        assert [f["id"] for f in frames] == [2], "a frame with invalid UTF-8 was accepted"
        assert len(errors) == 1
        assert errors[0]["error"]["code"] == PARSE_ERROR
        assert "UTF-8" in errors[0]["error"]["message"]
