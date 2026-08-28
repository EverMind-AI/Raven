"""Making stdout safe to speak a protocol on.

The descriptor, not the Python object. Replacing ``sys.stdout`` would cover only
writers that go through Python's own object, which is precisely the set that was
never the problem: this build shells out to ``soffice`` for every render, and that
child inherits fd 1. So these tests write through ``os.write(1, ...)`` -- what a
C extension, a subprocess, or an embedded logger with its own stream does -- and
assert the frame stream stays clean.

Real descriptors are moved here, and restored in a finally. Nothing is captured
through pytest's own capture, which would hide exactly the hazard under test.
"""

import json
import os

from raven.acp.protocol import decode
from raven.acp.stdio import claim_stdout, write_frame


def _redirected(tmp_path):
    """fd 1 and fd 2 pointed at files, with the originals kept for restore."""
    wire = tmp_path / "fd1"
    log = tmp_path / "fd2"
    saved = (os.dup(1), os.dup(2))
    handles = (
        os.open(wire, os.O_WRONLY | os.O_CREAT | os.O_TRUNC),
        os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC),
    )
    os.dup2(handles[0], 1)
    os.dup2(handles[1], 2)
    return wire, log, saved, handles


def _restore(saved, handles):
    os.dup2(saved[0], 1)
    os.dup2(saved[1], 2)
    for fd in (*saved, *handles):
        os.close(fd)


def test_a_write_to_fd_one_lands_in_the_log_not_the_frame_stream(tmp_path):
    """One line from any other writer is a frame the client cannot decode. What it
    sees is not "the agent logged something"; it is a protocol violation, and the
    session it was half way through is gone."""
    wire, log, saved, handles = _redirected(tmp_path)
    try:
        with claim_stdout() as out:
            write_frame(out, {"jsonrpc": "2.0", "id": 1, "result": {}})
            os.write(1, b"a stray line from somebody else\n")
            write_frame(out, {"jsonrpc": "2.0", "id": 2, "result": {}})
    finally:
        _restore(saved, handles)

    frames = [decode(line) for line in wire.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [f["id"] for f in frames] == [1, 2]
    assert "a stray line" in log.read_text(encoding="utf-8")


def test_fd_one_is_put_back_the_way_it_was_found(tmp_path):
    """The block is scoped, so a caller that keeps running after it -- or a test
    suite that does -- must not be left with its stdout pointed at stderr."""
    wire, _log, saved, handles = _redirected(tmp_path)
    try:
        with claim_stdout():
            pass
        os.write(1, b"after the block\n")
    finally:
        _restore(saved, handles)
    assert wire.read_text(encoding="utf-8") == "after the block\n"


def test_each_frame_is_flushed_before_write_frame_returns(tmp_path):
    """A frame left in a buffer is a client waiting forever for a reply that was
    already computed, which reads as a hang rather than as slowness and is the
    harder of the two to diagnose."""
    wire, _log, saved, handles = _redirected(tmp_path)
    try:
        with claim_stdout() as out:
            write_frame(out, {"jsonrpc": "2.0", "id": 1, "result": {}})
            # Read from an independent descriptor: the bytes are on the file only
            # if the flush already happened.
            assert json.loads(wire.read_text(encoding="utf-8"))["id"] == 1
    finally:
        _restore(saved, handles)


def test_a_non_ascii_frame_survives_the_round_trip(tmp_path):
    wire, _log, saved, handles = _redirected(tmp_path)
    try:
        with claim_stdout() as out:
            write_frame(out, {"jsonrpc": "2.0", "id": 1, "result": {"title": "季度回顾"}})
    finally:
        _restore(saved, handles)
    assert decode(wire.read_text(encoding="utf-8"))["result"]["title"] == "季度回顾"
