"""Tests for the ``raven acp`` CLI entry."""

from __future__ import annotations

import asyncio

import pytest
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

    async def fake_serve(reader, out, *, loops, user_pool):
        events.append("serve")
        # The registry serves the shared pieces _serve already built and started
        # the backend on, and builds one engine per session on top of them.
        assert loops.session_manager is shared.session_manager
        assert user_pool == shared.acp.user_pool
        first = await loops.get("acp:one")
        second = await loops.get("acp:two")
        assert first is not second
        assert await loops.get("acp:one") is first

    built: list[str | None] = []
    shared = SimpleNamespace(
        backend=backend,
        session_manager=object(),
        acp=SimpleNamespace(user_pool=2, max_loops=8),
    )

    def fake_build_loop(_shared, conversation=None):
        built.append(conversation)
        return SimpleNamespace(workspace=None)

    monkeypatch.setattr(acp_commands, "redirect_loguru_to_file", lambda *a, **k: "acp.log")
    monkeypatch.setattr(acp_commands, "install_crash_handlers", lambda: None)
    monkeypatch.setattr(acp_commands, "claim_stdout", fake_claim_stdout)
    monkeypatch.setattr(acp_commands, "_open_stdin", fake_open_stdin)
    monkeypatch.setattr(acp_commands, "serve", fake_serve)
    monkeypatch.setattr(acp_commands, "build_shared", lambda config=None: shared)
    monkeypatch.setattr(acp_commands, "build_loop", fake_build_loop)


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
    from raven.cli.acp_commands import _serve
    from raven.memory_engine.backend import MemoryServiceUnavailableError

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


# -- the shared / per-session split ---------------------------------------------


def _shared_for(tmp_path):
    from types import SimpleNamespace

    from raven.cli.acp_commands import AcpShared
    from raven.config.raven import RavenConfig
    from raven.config.schema import Config

    config = Config(agents={"defaults": {"workspace": str(tmp_path), "model": "m", "provider": "custom"}})
    return AcpShared(
        config=config,
        ec_config=RavenConfig(),
        acp=config.acp,
        provider=SimpleNamespace(get_default_model=lambda: "m"),
        session_manager=object(),
        strategies=None,
        plugin_registry=None,
        backend=None,
        plugin_tools=None,
        store_inflight=set(),
        pending_recovery={},
    )


def _captured_loop_kwargs(tmp_path, monkeypatch, conversation):
    """The kwargs ``build_loop`` hands AgentLoop, without building one."""
    from raven.cli import acp_commands

    captured: dict = {}

    class _Recorder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def configure_personalization(self, _enable):
            return None

    import raven.agent.loop as loop_module

    monkeypatch.setattr(loop_module, "AgentLoop", _Recorder)
    acp_commands.build_loop(_shared_for(tmp_path), conversation)
    return captured


def test_a_session_writes_in_its_own_subtree(tmp_path, monkeypatch):
    kwargs = _captured_loop_kwargs(tmp_path, monkeypatch, "acp:20260827_000000_aaa")
    assert kwargs["file_workspace"] == tmp_path / "acp_workspaces" / "20260827_000000_aaa"
    assert kwargs["file_workspace"].is_dir()


def test_the_file_roots_stay_out_of_the_session_store(tmp_path, monkeypatch):
    """SessionManager owns ``workspace/sessions`` and lists it with
    ``glob("*/*.jsonl")``, so a report written there as .jsonl would come back
    as a session on a channel named after the directory."""
    from raven.session.manager import SessionManager

    kwargs = _captured_loop_kwargs(tmp_path, monkeypatch, "acp:20260827_000000_aaa")
    store = SessionManager(tmp_path).sessions_dir
    root = kwargs["file_workspace"]
    assert store not in root.parents
    assert root != store

    (root / "report.jsonl").write_text("{}\n", encoding="utf-8")
    assert SessionManager(tmp_path).list_sessions() == []


def test_the_prompt_facing_workspace_stays_shared(tmp_path, monkeypatch):
    """The guard on AGENTS.md 0.2: the system prompt's memory segments and the
    skill catalogue are read from ``workspace``, so moving it per session would
    empty both and change what the model reads. Only the file tools move."""
    kwargs = _captured_loop_kwargs(tmp_path, monkeypatch, "acp:20260827_000000_aaa")
    assert kwargs["workspace"] == tmp_path
    assert kwargs["workspace"] != kwargs["file_workspace"]


def test_two_sessions_do_not_share_a_file_root(tmp_path, monkeypatch):
    first = _captured_loop_kwargs(tmp_path, monkeypatch, "acp:one")
    second = _captured_loop_kwargs(tmp_path, monkeypatch, "acp:two")
    assert first["file_workspace"] != second["file_workspace"]
    assert first["workspace"] == second["workspace"]


def test_every_session_engine_shares_the_one_write_gate(tmp_path, monkeypatch):
    shared = _shared_for(tmp_path)
    from raven.cli import acp_commands

    captured: list[dict] = []

    class _Recorder:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def configure_personalization(self, _enable):
            return None

    import raven.agent.loop as loop_module

    monkeypatch.setattr(loop_module, "AgentLoop", _Recorder)
    acp_commands.build_loop(shared, "acp:one")
    acp_commands.build_loop(shared, "acp:two")
    assert captured[0]["store_inflight"] is captured[1]["store_inflight"] is shared.store_inflight
    assert captured[0]["session_manager"] is captured[1]["session_manager"] is shared.session_manager
    assert captured[0]["backend"] is captured[1]["backend"]


def test_a_rebuilt_engine_inherits_the_interrupted_turn_record(tmp_path, monkeypatch):
    """The engine cap evicts between an interrupted turn and the turn that reads it.

    ``_pending_recovery`` is written by the turn that was interrupted and read by
    that session's NEXT turn, so on this surface its two ends can sit on
    different engine instances. Held per instance it is rebuilt empty, and the
    recovery warning is dropped while the files and the shadow commit still
    exist -- which is worse than never having stashed it, because the turn is
    told nothing and the partial edits look finished.
    """
    shared = _shared_for(tmp_path)
    from raven.cli import acp_commands

    captured: list[dict] = []

    class _Recorder:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def configure_personalization(self, _enable):
            return None

    import raven.agent.loop as loop_module

    monkeypatch.setattr(loop_module, "AgentLoop", _Recorder)
    acp_commands.build_loop(shared, "acp:one")
    # The same session again is the rebuild an eviction forces.
    acp_commands.build_loop(shared, "acp:one")
    assert captured[0]["pending_recovery"] is captured[1]["pending_recovery"] is shared.pending_recovery

    # And the map carries a record across that rebuild rather than merely being
    # the same object: a stash taken through the first engine's map is what the
    # second engine reads.
    captured[0]["pending_recovery"]["acp:one"] = {"checkpoint_id": "abc1234", "files": ["partial.py"]}
    assert captured[1]["pending_recovery"]["acp:one"]["files"] == ["partial.py"]


def test_no_session_leaves_the_engine_on_the_shared_root(tmp_path, monkeypatch):
    """``build_loop`` with no conversation is the pre-change shape, kept so a
    caller that has one engine is not forced to invent a session id."""
    kwargs = _captured_loop_kwargs(tmp_path, monkeypatch, None)
    assert kwargs["file_workspace"] is None


def test_a_minted_session_id_is_used_as_the_directory_name(tmp_path):
    from raven.cli.acp_commands import _session_file_root

    root = _session_file_root(tmp_path, "acp:20260827_000000_aaa")
    assert root == tmp_path / "acp_workspaces" / "20260827_000000_aaa"


@pytest.mark.parametrize(
    "session_id",
    [
        "acp:../../etc",
        "acp:..",
        "acp:.",
        "acp:a/b",
        "acp:.hidden",
        "acp:",
        "acp:with space",
        "acp:" + "x" * 200,
    ],
)
def test_a_wire_supplied_id_can_never_walk_out_of_the_workspace(tmp_path, session_id):
    """session/load and session/resume take the id from the client, so this join
    is one of the places that must not trust it."""
    from raven.cli.acp_commands import _session_file_root

    root = _session_file_root(tmp_path, session_id)
    assert root is not None
    resolved = root.resolve()
    parent = (tmp_path / "acp_workspaces").resolve()
    assert resolved.parent == parent
    assert parent in resolved.parents


def test_a_rejected_id_still_maps_to_one_stable_directory(tmp_path):
    from raven.cli.acp_commands import _session_file_root

    first = _session_file_root(tmp_path, "acp:../../etc")
    second = _session_file_root(tmp_path, "acp:../../etc")
    other = _session_file_root(tmp_path, "acp:../../var")
    assert first == second
    assert first != other
