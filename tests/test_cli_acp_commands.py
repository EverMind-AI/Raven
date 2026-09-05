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
    """The callback's own arguments are passed explicitly here.

    ``acp`` is called directly rather than through Typer, and its
    ``typer.Option`` defaults are ``OptionInfo`` sentinels that only Typer
    resolves -- an omitted one arrives as an object that reads as a value the
    caller supplied. ``commands.py`` carries the same note for ``raven``'s own
    delegation to ``tui``.
    """

    def test_it_serves_when_no_subcommand_was_given(self, monkeypatch):
        served = []

        async def _fake_serve(*, verbose: bool = False) -> None:
            served.append(verbose)

        monkeypatch.setattr(acp_commands, "_serve", _fake_serve)

        acp_commands.acp(SimpleNamespace(invoked_subcommand=None), config=None, verbose=False)

        assert served == [False]

    def test_a_crash_is_reported_briefly_rather_than_as_a_rich_traceback(self, monkeypatch):
        """Typer's own handler renders a rich traceback with ``show_locals`` on --
        measured at 228 lines of stderr carrying the value of every local in every
        frame, on the stream an ACP client displays. The full traceback goes to
        the log file, where the sink is configured not to annotate it."""

        async def _explode(*, verbose: bool = False) -> None:
            raise RuntimeError("engine failed")

        monkeypatch.setattr(acp_commands, "_serve", _explode)

        with pytest.raises(typer.Exit) as caught:
            acp_commands.acp(SimpleNamespace(invoked_subcommand=None), config=None, verbose=False)

        assert caught.value.exit_code == 1

    def test_it_defers_to_a_subcommand(self, monkeypatch):
        """``acp`` is a Typer group, so a future ``raven acp <something>`` must
        not also start the server."""

        async def _must_not_run(*, verbose: bool = False) -> None:
            raise AssertionError("serving despite an explicit subcommand")

        monkeypatch.setattr(acp_commands, "_serve", _must_not_run)

        with contextlib.suppress(RuntimeWarning):
            acp_commands.acp(SimpleNamespace(invoked_subcommand="future-subcommand"), config=None, verbose=False)


class TestConfigOption:
    """``--config`` is what lets one host serve several of these at once.

    Without it every child comes up on the host's own config -- a different
    model, a different provider, a different identity than the one the caller
    meant to spawn.
    """

    @staticmethod
    def _dont_serve(monkeypatch):
        async def _fake_serve(*, verbose: bool = False) -> None:
            pass

        monkeypatch.setattr(acp_commands, "_serve", _fake_serve)

    def test_it_points_the_process_at_the_given_config(self, monkeypatch, tmp_path):
        cfg = tmp_path / "elsewhere" / "config.json"
        cfg.parent.mkdir()
        cfg.write_text("{}", encoding="utf-8")
        seen = []
        monkeypatch.setattr(acp_commands, "set_config_path", seen.append)
        self._dont_serve(monkeypatch)

        acp_commands.acp(SimpleNamespace(invoked_subcommand=None), config=str(cfg), verbose=False)

        assert seen == [cfg.resolve()]

    def test_the_default_leaves_the_process_on_its_own_config(self, monkeypatch):
        seen = []
        monkeypatch.setattr(acp_commands, "set_config_path", seen.append)
        self._dont_serve(monkeypatch)

        acp_commands.acp(SimpleNamespace(invoked_subcommand=None), config=None, verbose=False)

        assert seen == []

    def test_it_is_applied_before_the_server_starts(self, monkeypatch, tmp_path):
        """Order, not just effect: ``get_logs_dir`` hangs off the config's own
        directory, so a path set after the redirect would leave this instance's
        log in the default home while it served from somewhere else."""
        cfg = tmp_path / "config.json"
        cfg.write_text("{}", encoding="utf-8")
        order = []
        monkeypatch.setattr(acp_commands, "set_config_path", lambda p: order.append("config"))

        async def _fake_serve(*, verbose: bool = False) -> None:
            order.append("serve")

        monkeypatch.setattr(acp_commands, "_serve", _fake_serve)

        acp_commands.acp(SimpleNamespace(invoked_subcommand=None), config=str(cfg), verbose=False)

        assert order == ["config", "serve"]

    def test_a_missing_config_exits_2_rather_than_1(self, monkeypatch, tmp_path):
        """Exit 2 is a mistake in the spawn command; exit 1 is the agent having
        crashed. A client that can tell them apart can say so instead of
        retrying."""

        async def _must_not_run(*, verbose: bool = False) -> None:
            raise AssertionError("served despite a missing config")

        monkeypatch.setattr(acp_commands, "_serve", _must_not_run)

        with pytest.raises(typer.Exit) as caught:
            acp_commands.acp(
                SimpleNamespace(invoked_subcommand=None),
                config=str(tmp_path / "absent.json"),
                verbose=False,
            )

        assert caught.value.exit_code == 2

    def test_the_real_loader_resolves_to_it(self, monkeypatch, tmp_path):
        """Driven against the real loader, not the stub above.

        What the flag has to move is ``get_config_path``, because every runtime
        directory is derived from it -- including the log this command writes,
        which is the one a person goes looking for when an instance misbehaves.
        """
        from raven.config import loader
        from raven.config.paths import get_logs_dir

        cfg = tmp_path / "instance" / "config.json"
        cfg.parent.mkdir()
        cfg.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("raven.home._current_config_path", None)
        self._dont_serve(monkeypatch)

        acp_commands.acp(SimpleNamespace(invoked_subcommand=None), config=str(cfg), verbose=False)

        assert loader.get_config_path() == cfg
        assert get_logs_dir() == cfg.parent / "logs"

    def test_a_directory_is_not_a_config(self, monkeypatch, tmp_path):
        async def _must_not_run(*, verbose: bool = False) -> None:
            raise AssertionError("served despite being handed a directory")

        monkeypatch.setattr(acp_commands, "_serve", _must_not_run)

        with pytest.raises(typer.Exit) as caught:
            acp_commands.acp(SimpleNamespace(invoked_subcommand=None), config=str(tmp_path), verbose=False)

        assert caught.value.exit_code == 2


class TestFileLogLevel:
    def test_it_defaults_to_info(self, monkeypatch):
        monkeypatch.delenv("RAVEN_ACP_LOG_LEVEL", raising=False)

        assert acp_commands._file_log_level() == "INFO"

    def test_the_environment_can_raise_it(self, monkeypatch):
        monkeypatch.setenv("RAVEN_ACP_LOG_LEVEL", "debug")

        assert acp_commands._file_log_level() == "DEBUG"

    def test_verbose_outranks_the_environment(self, monkeypatch):
        """The flag is set per spawn by whoever is debugging this run; the
        variable is whatever the environment happened to carry in."""
        monkeypatch.setenv("RAVEN_ACP_LOG_LEVEL", "WARNING")

        assert acp_commands._file_log_level(verbose=True) == "DEBUG"

    async def test_verbose_reaches_the_sink(self, monkeypatch, tmp_path):
        TestServe._stub(monkeypatch, tmp_path, b"")
        levels = []

        def _redirect(_name, *, file_level, **_kw):
            levels.append(file_level)
            return tmp_path / "acp.log"

        monkeypatch.setattr(acp_commands, "redirect_loguru_to_file", _redirect)

        async def _serve(reader, out):
            pass

        monkeypatch.setattr(acp_commands, "serve", _serve)

        await acp_commands._serve(verbose=True)

        assert levels == ["DEBUG"]


class TestHelp:
    """The help text is the only place this command explains itself.

    A client spawns it, so there is no interactive surface to discover any of
    this from: what is not in ``--help`` is not anywhere the caller will look.
    """

    @staticmethod
    def _rendered() -> str:
        from typer.testing import CliRunner

        from raven.cli.commands import app

        result = CliRunner().invoke(app, ["acp", "--help"])
        assert result.exit_code == 0
        return result.output

    def test_it_does_not_promise_subcommands_it_has_none_of(self):
        """The group shape is deliberate -- see ``test_it_defers_to_a_subcommand``
        -- but Click's default metavar advertises a ``raven acp <command>`` that
        does not exist."""

        assert "COMMAND [ARGS]" not in self._rendered()

    def test_it_names_every_variable_that_changes_its_behaviour(self):
        out = self._rendered()

        for name in ("RAVEN_ACP_LOG_LEVEL", "RAVEN_CLI_DEBUG", "RAVEN_HOME"):
            assert name in out, f"{name} changes how this command behaves and is not in its help"

    def test_it_names_every_method_that_requires_a_cwd(self):
        """All three take one. A reader who took the working directory to be
        session/new's alone would omit it from load or resume and get -32602 --
        and this help is written to be driven by hand, so that reader is the one
        it exists for."""
        flowed = " ".join(self._rendered().split())

        assert "session/new, session/load and session/resume each require one" in flowed

    def test_it_offers_the_flags_a_caller_needs_to_place_an_instance(self):
        out = self._rendered()

        assert "--config" in out
        assert "--verbose" in out
