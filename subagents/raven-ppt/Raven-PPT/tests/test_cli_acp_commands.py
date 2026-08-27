"""CLI tests for ``raven acp`` -- the process shell around the ACP server.

Nothing here starts a server: what the shell owns is the stdio channel and the
log destination, and each of those has a failure mode that only shows up as a
hang or as a corrupted frame stream. The protocol itself is covered by
``test_acp_methods.py`` / ``test_acp_spine.py``.
"""

import asyncio
import os
import time

from raven.cli import acp_commands
from raven.cli.acp_commands import _file_log_level, _open_stdin, _spawn_stdin_feeder


def test_the_command_is_registered_on_the_cli():
    from raven.cli.commands import app

    assert "acp" in {command.name or command.callback.__name__ for command in app.registered_commands}


def test_the_log_level_defaults_to_info_and_is_overridable(monkeypatch):
    """DEBUG is expensive here in a way a level usually is not: a provider client
    writes whole requests as single records, and those lines also reach the client
    over fd 2."""
    monkeypatch.delenv("RAVEN_ACP_LOG_LEVEL", raising=False)
    assert _file_log_level() == "INFO"
    monkeypatch.setenv("RAVEN_ACP_LOG_LEVEL", " debug ")
    assert _file_log_level() == "DEBUG"


async def test_a_regular_file_stdin_is_read_on_a_thread(tmp_path, monkeypatch):
    """``raven acp < script.jsonl`` is how anyone first tries this by hand, and
    ``connect_read_pipe`` refuses a regular file outright -- so without the
    fallback it dies with a traceback before reading a byte."""
    script = tmp_path / "script.jsonl"
    script.write_bytes(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    with script.open("rb") as handle:
        monkeypatch.setattr(acp_commands.sys, "stdin", handle)
        async with _open_stdin(1024) as reader:
            assert await asyncio.wait_for(reader.readline(), 5) == script.read_bytes()
            # EOF rather than a hang: the feeder must say the stream ended.
            assert await asyncio.wait_for(reader.read(), 5) == b""


async def test_the_feeder_reports_eof_when_its_read_fails(monkeypatch):
    """A read error is EOF as far as the protocol is concerned. Without this the
    frame loop waits for bytes that are never coming."""

    class _Broken:
        def read1(self, _n):
            raise OSError("descriptor went away")

    monkeypatch.setattr(acp_commands.sys, "stdin", _Broken())
    reader = asyncio.StreamReader()
    thread = _spawn_stdin_feeder(reader)
    await asyncio.wait_for(asyncio.to_thread(thread.join, 5), 10)
    assert await asyncio.wait_for(reader.read(), 5) == b""


async def test_the_feeder_is_a_daemon_thread(monkeypatch):
    """``run_in_executor`` would not do: the read is blocking and uncancellable,
    and asyncio waits for the default executor when it closes the loop -- which
    turns an idle pipe into a process that will not exit."""

    class _Blocking:
        def read1(self, _n):
            time.sleep(30)
            return b""

    monkeypatch.setattr(acp_commands.sys, "stdin", _Blocking())
    assert _spawn_stdin_feeder(asyncio.StreamReader()).daemon is True


def test_the_jobs_directory_is_named_once():
    """The launcher and the server have to agree on it, so there is one spelling."""
    assert acp_commands.JOBS_SUBDIR == "acp_jobs"
    assert os.sep not in acp_commands.JOBS_SUBDIR
