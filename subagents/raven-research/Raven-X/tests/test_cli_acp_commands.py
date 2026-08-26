"""Tests for the ``raven acp`` CLI entry."""

from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from raven.cli.acp_commands import _open_stdin, _spawn_stdin_feeder, acp_app
from raven.cli.commands import app

runner = CliRunner()


def test_acp_is_registered_on_the_main_app():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "acp" in result.output


def test_acp_help_does_not_start_serving():
    result = runner.invoke(acp_app, ["--help"])
    assert result.exit_code == 0
    assert "stdio" in result.output


async def test_open_stdin_falls_back_to_a_thread_for_a_regular_file(tmp_path, monkeypatch):
    # ``raven acp < script.jsonl`` is how anyone first tries this by hand:
    # connect_read_pipe refuses a regular file, and the feeder thread must
    # deliver the same bytes and the EOF.
    script = tmp_path / "in.jsonl"
    script.write_bytes(b'{"id": 1}\n{"id": 2}\n')
    with open(script, "rb") as fake_stdin:
        # A BufferedReader over a regular file: fileno() exists, so
        # connect_read_pipe gets far enough to refuse it (ValueError), which is
        # exactly the shape of a shell redirection.
        monkeypatch.setattr("raven.cli.acp_commands.sys.stdin", fake_stdin)
        async with _open_stdin() as reader:
            data = await asyncio.wait_for(reader.read(), timeout=5)
    assert data == b'{"id": 1}\n{"id": 2}\n'


async def test_stdin_feeder_reports_eof_once_the_stream_ends(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    with open(empty, "rb") as fake_stdin:

        class _Stdin:
            buffer = fake_stdin

        monkeypatch.setattr("raven.cli.acp_commands.sys.stdin", _Stdin())
        reader = asyncio.StreamReader()
        _spawn_stdin_feeder(reader)
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""


# -- the backend lifecycle around _serve ----------------------------------------


class _FakeBackend:
    def __init__(self, events: list[str], *, refuse: Exception | None = None) -> None:
        self._events = events
        self._refuse = refuse
        self.stops = 0

    async def start(self) -> None:
        self._events.append("start")
        if self._refuse is not None:
            raise self._refuse

    async def stop(self) -> None:
        self.stops += 1
        self._events.append("stop")


def _wire_serve_stubs(monkeypatch, events: list[str], backend) -> None:
    """Point _serve at stubs so it exercises only the lifecycle under test."""
    import contextlib as _ctx
    import io

    from types import SimpleNamespace

    from raven.cli import acp_commands

    @_ctx.contextmanager
    def fake_claim_stdout():
        yield io.BytesIO()

    @_ctx.asynccontextmanager
    async def fake_open_stdin():
        yield object()

    async def fake_serve(reader, out, *, agent_loop_factory):
        events.append("serve")
        # The factory must hand back the loop _serve already built and
        # started the backend on -- not construct a second one.
        assert agent_loop_factory() is loop

    loop = SimpleNamespace(backend=backend)
    monkeypatch.setattr(acp_commands, "redirect_loguru_to_file", lambda *a, **k: "acp.log")
    monkeypatch.setattr(acp_commands, "install_crash_handlers", lambda: None)
    monkeypatch.setattr(acp_commands, "claim_stdout", fake_claim_stdout)
    monkeypatch.setattr(acp_commands, "_open_stdin", fake_open_stdin)
    monkeypatch.setattr(acp_commands, "serve", fake_serve)
    monkeypatch.setattr(acp_commands, "_build_acp_agent_loop", lambda config=None: loop)


async def test_the_backend_is_started_before_serving_and_stopped_after(monkeypatch):
    """maybe_build_memory_backend returns an UNSTARTED backend: start() owns the
    /health probe, the recall warm-up and require_service enforcement, and this
    surface used to skip it entirely (every other surface calls it)."""
    from raven.cli.acp_commands import _serve

    events: list[str] = []
    _wire_serve_stubs(monkeypatch, events, _FakeBackend(events))

    await _serve(None)

    assert events == ["start", "serve", "stop"]


async def test_a_loop_without_a_backend_serves_anyway(monkeypatch):
    from raven.cli.acp_commands import _serve

    events: list[str] = []
    _wire_serve_stubs(monkeypatch, events, None)

    await _serve(None)

    assert events == ["serve"]


async def test_a_refused_start_fails_the_process_and_never_serves(monkeypatch):
    """require_service semantics: the operator asked to stop rather than write
    into nothing, and the consuming raven reports the dead agent."""
    import pytest

    from raven.memory_engine.backend import MemoryServiceUnavailableError

    from raven.cli.acp_commands import _serve

    events: list[str] = []
    backend = _FakeBackend(events, refuse=MemoryServiceUnavailableError("everos down"))
    _wire_serve_stubs(monkeypatch, events, backend)

    with pytest.raises(MemoryServiceUnavailableError):
        await _serve(None)

    assert events == ["start", "stop"]
    assert backend.stops == 1


async def test_any_other_start_failure_degrades_and_still_serves(monkeypatch):
    from raven.cli.acp_commands import _serve

    events: list[str] = []
    backend = _FakeBackend(events, refuse=RuntimeError("transient"))
    _wire_serve_stubs(monkeypatch, events, backend)

    await _serve(None)

    assert events == ["start", "serve", "stop"]
