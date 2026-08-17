"""Tests for raven.plugin.memory.everos._server."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from raven.plugin.memory.everos._server import (
    EverosNotConfiguredError,
    StopOutcome,
    _everos_executable,
    ensure_everos_server,
)


def _make_executable(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


class TestEverosExecutable:
    """The interpreter's own directory wins over PATH.

    ``uv tool install`` exposes only the requested package's entry points, so a
    released install has ``raven`` on PATH and ``everos`` only inside the tool
    venv. Preferring the sibling also avoids picking up an everos from an
    unrelated environment whose version does not match raven's pin.
    """

    def test_prefers_interpreter_sibling(self, tmp_path, monkeypatch) -> None:
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        _make_executable(venv_bin / "everos")
        monkeypatch.setattr("sys.executable", str(venv_bin / "python3"))
        monkeypatch.setenv("PATH", "")

        assert _everos_executable() == str(venv_bin / "everos")

    def test_falls_back_to_path(self, tmp_path, monkeypatch) -> None:
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        path_dir = tmp_path / "elsewhere"
        path_dir.mkdir()
        _make_executable(path_dir / "everos")
        monkeypatch.setattr("sys.executable", str(venv_bin / "python3"))
        monkeypatch.setenv("PATH", str(path_dir))

        assert _everos_executable() == str(path_dir / "everos")

    def test_sibling_beats_path_when_both_exist(self, tmp_path, monkeypatch) -> None:
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        _make_executable(venv_bin / "everos")
        path_dir = tmp_path / "elsewhere"
        path_dir.mkdir()
        _make_executable(path_dir / "everos")
        monkeypatch.setattr("sys.executable", str(venv_bin / "python3"))
        monkeypatch.setenv("PATH", str(path_dir))

        assert _everos_executable() == str(venv_bin / "everos")

    def test_sibling_without_exec_bit_is_skipped(self, tmp_path, monkeypatch) -> None:
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "everos").write_text("not executable\n")
        (venv_bin / "everos").chmod(0o644)
        path_dir = tmp_path / "elsewhere"
        path_dir.mkdir()
        _make_executable(path_dir / "everos")
        monkeypatch.setattr("sys.executable", str(venv_bin / "python3"))
        monkeypatch.setenv("PATH", str(path_dir))

        assert _everos_executable() == str(path_dir / "everos")

    def test_missing_everywhere_names_the_interpreter_dir(self, tmp_path, monkeypatch) -> None:
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        monkeypatch.setattr("sys.executable", str(venv_bin / "python3"))
        monkeypatch.setenv("PATH", "")

        with pytest.raises(RuntimeError, match=str(venv_bin)):
            _everos_executable()


@pytest.fixture
def everos_toml(tmp_path, monkeypatch):
    """Redirect the EverOS config read/write to a throwaway root raven owns."""
    import raven.config.update_everos as ue

    root = tmp_path / ".everos"
    monkeypatch.setattr(ue, "everos_root", lambda: root)
    monkeypatch.setattr(ue, "everos_owned", lambda: True)
    return root / "everos.toml"


def _live_child():
    """A Popen stand-in that is still running (``poll()`` returns None)."""
    proc = MagicMock()
    proc.poll.return_value = None
    return proc


def _write_llm_section(cfg, *, model="mem-llm", api_key="k"):
    cfg.parent.mkdir(parents=True, exist_ok=True)
    body = "[llm]\n"
    if model is not None:
        body += f'model = "{model}"\n'
    if api_key is not None:
        body += f'api_key = "{api_key}"\n'
    cfg.write_text(body, encoding="utf-8")


class TestSpawnPreflight:
    """A server with no LLM credentials cannot survive startup, so do not spawn.

    EverOS builds its LLM client eagerly during startup and fails outright when
    credentials are missing. Spawning regardless burned a full poll timeout on a
    process that had already exited and buried the reason in the server log.
    """

    @pytest.mark.asyncio
    async def test_missing_api_key_does_not_spawn(self, everos_toml, monkeypatch) -> None:
        _write_llm_section(everos_toml, api_key="")
        spawned = []
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server._start_server_if_unlocked",
            lambda *a, **kw: spawned.append(1),
        )
        with patch("raven.plugin.memory.everos._server._probe_health", return_value=False):
            with pytest.raises(EverosNotConfiguredError, match="memory LLM is not configured"):
                await ensure_everos_server("http://localhost:18791", timeout=1.0)

        assert spawned == [], "spawned a server that could never finish starting"

    @pytest.mark.asyncio
    async def test_missing_section_entirely_does_not_spawn(self, everos_toml, monkeypatch) -> None:
        everos_toml.parent.mkdir(parents=True, exist_ok=True)
        everos_toml.write_text('[memory]\ntimezone = "UTC"\n', encoding="utf-8")
        spawned = []
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server._start_server_if_unlocked",
            lambda *a, **kw: spawned.append(1),
        )
        with patch("raven.plugin.memory.everos._server._probe_health", return_value=False):
            with pytest.raises(EverosNotConfiguredError):
                await ensure_everos_server("http://localhost:18791", timeout=1.0)

        assert spawned == []

    @pytest.mark.asyncio
    async def test_a_running_server_needs_no_credential_check(self, everos_toml) -> None:
        """A /health 200 proves the LLM client was built, so do not second-guess it."""
        _write_llm_section(everos_toml, api_key="")
        waits: list[int] = []
        with patch("raven.plugin.memory.everos._server._probe_health", return_value=True):
            await ensure_everos_server("http://localhost:18791", on_wait=lambda: waits.append(1))

        assert waits == [], "narrated a wait that never happened"

    @pytest.mark.asyncio
    async def test_configured_llm_reaches_the_spawn(self, everos_toml, tmp_path, monkeypatch) -> None:
        _write_llm_section(everos_toml)
        spawned = []
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server._start_server_if_unlocked",
            lambda *a, **kw: spawned.append(1),
        )
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server.get_logs_dir",
            lambda: tmp_path,
        )
        with patch("raven.plugin.memory.everos._server._probe_health", side_effect=[False, True]):
            await ensure_everos_server("http://localhost:18791", timeout=5.0)

        assert spawned == [1]


class TestTheRootDescribesItsOwnAddress:
    """The address goes into ``<root>/everos.toml``, not onto the command line.

    A ``--port`` override left the file describing an address nobody listened on,
    which is how the wizard came to probe one port while the backend talked to
    another.
    """

    @pytest.fixture(autouse=True)
    def _no_real_spawn(self, tmp_path, monkeypatch):
        monkeypatch.setattr("raven.plugin.memory.everos._server.get_logs_dir", lambda: tmp_path)
        monkeypatch.setattr("raven.plugin.memory.everos._server.get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server._everos_executable",
            lambda: "/bin/true",
        )
        self.spawned: list[list[str]] = []

        class _Proc:
            pid = 4242

            def poll(self):
                return None

        def _popen(argv, **_kw):
            self.spawned.append(argv)
            return _Proc()

        monkeypatch.setattr("subprocess.Popen", _popen)

    def test_the_declared_address_is_written_before_the_child_starts(self, everos_toml) -> None:
        import tomllib

        from raven.plugin.memory.everos._server import _start_server_if_unlocked

        _write_llm_section(everos_toml)
        _start_server_if_unlocked("http://localhost:18791")

        with everos_toml.open("rb") as fh:
            api = tomllib.load(fh)["api"]
        assert api == {"host": "localhost", "port": 18791}

    def test_the_child_gets_root_and_no_port(self, everos_toml) -> None:
        from raven.plugin.memory.everos._server import _start_server_if_unlocked

        _write_llm_section(everos_toml)
        _start_server_if_unlocked("http://localhost:18791")

        argv = self.spawned[0]
        assert "--port" not in argv, "a command-line port would override the toml again"
        assert "--root" in argv
        assert argv[argv.index("--root") + 1] == str(everos_toml.parent)

    def test_the_started_server_is_recorded(self, everos_toml, tmp_path) -> None:
        import json

        from raven.plugin.memory.everos._server import _start_server_if_unlocked

        _write_llm_section(everos_toml)
        _start_server_if_unlocked("http://localhost:18791")

        record = json.loads((tmp_path / "everos-server.pid").read_text())
        assert record["pid"] == 4242
        assert record["root"] == str(everos_toml.parent)


class TestStoppingWhatWeStarted:
    """A pidfile is stale information, so verify before signalling."""

    @pytest.fixture
    def _data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("raven.plugin.memory.everos._server.get_data_dir", lambda: tmp_path)
        return tmp_path

    def test_a_pid_reused_by_another_program_is_not_signalled(self, _data_dir, monkeypatch) -> None:
        import json
        import os as _os

        from raven.plugin.memory.everos._server import stop_recorded_server

        (_data_dir / "everos-server.pid").write_text(
            json.dumps({"pid": 4242, "base_url": "http://localhost:18791", "root": str(_data_dir)})
        )
        monkeypatch.setattr("raven.plugin.memory.everos._server._is_everos_server", lambda _p: False)
        signalled: list[int] = []
        monkeypatch.setattr(_os, "kill", lambda *a: signalled.append(1))

        assert stop_recorded_server(_data_dir) is StopOutcome.NOT_OURS
        assert signalled == [], "signalled a pid that is no longer an everos server"

    def test_a_record_for_another_root_is_ignored(self, _data_dir, monkeypatch) -> None:
        import json
        import os as _os

        from raven.plugin.memory.everos._server import stop_recorded_server

        (_data_dir / "everos-server.pid").write_text(
            json.dumps({"pid": 4242, "base_url": "http://localhost:18791", "root": "/somewhere/else"})
        )
        monkeypatch.setattr("raven.plugin.memory.everos._server._is_everos_server", lambda _p: True)
        signalled: list[int] = []
        monkeypatch.setattr(_os, "kill", lambda *a: signalled.append(1))

        assert stop_recorded_server(_data_dir) is StopOutcome.NOT_OURS
        assert signalled == []

    def test_a_server_that_will_not_exit_reports_draining_not_ownership(self, _data_dir, monkeypatch) -> None:
        """A shutdown that is still draining memory work is not a foreign process.

        Collapsed into one False, the wizard told users whose OME jobs were
        mid-flight that raven had not started the server -- an explanation that is
        simply untrue, and one that sends them looking for the wrong thing.
        """
        import json
        import os as _os

        from raven.plugin.memory.everos._server import stop_recorded_server

        (_data_dir / "everos-server.pid").write_text(
            json.dumps({"pid": 4242, "base_url": "http://localhost:18791", "root": str(_data_dir)})
        )
        monkeypatch.setattr("raven.plugin.memory.everos._server._is_everos_server", lambda _p: True)
        monkeypatch.setattr(_os, "kill", lambda *_a: None)
        monkeypatch.setattr("raven.plugin.memory.everos._server.time.sleep", lambda _s: None)

        assert stop_recorded_server(_data_dir, timeout=1.0) is StopOutcome.STILL_DRAINING
        assert (_data_dir / "everos-server.pid").exists(), "forgot a server that is still up"

    def test_an_undeliverable_signal_is_its_own_answer(self, _data_dir, monkeypatch) -> None:
        import json
        import os as _os

        from raven.plugin.memory.everos._server import stop_recorded_server

        (_data_dir / "everos-server.pid").write_text(
            json.dumps({"pid": 4242, "base_url": "http://localhost:18791", "root": str(_data_dir)})
        )
        monkeypatch.setattr("raven.plugin.memory.everos._server._is_everos_server", lambda _p: True)

        def _denied(*_a):
            raise PermissionError("nope")

        monkeypatch.setattr(_os, "kill", _denied)

        assert stop_recorded_server(_data_dir) is StopOutcome.SIGNAL_FAILED

    def test_a_verified_server_gets_sigterm_not_sigkill(self, _data_dir, monkeypatch) -> None:
        import json
        import os as _os
        import signal as _signal

        from raven.plugin.memory.everos._server import stop_recorded_server

        (_data_dir / "everos-server.pid").write_text(
            json.dumps({"pid": 4242, "base_url": "http://localhost:18791", "root": str(_data_dir)})
        )
        alive = [True, False]
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server._is_everos_server",
            lambda _p: alive.pop(0) if alive else False,
        )
        sent: list[int] = []
        monkeypatch.setattr(_os, "kill", lambda _pid, sig: sent.append(sig))

        assert stop_recorded_server(_data_dir) is StopOutcome.STOPPED
        assert sent == [_signal.SIGTERM]
        assert not (_data_dir / "everos-server.pid").exists()


class TestThePrimitivesAgainstTheRealOS:
    """The four OS-touching helpers, unmocked.

    Everything above stubs these out to stay deterministic, which leaves the
    layer where a wrong assumption about flock or ``ps`` would not be caught by
    anything. These run them for real.
    """

    def test_an_untouched_root_reads_as_free(self, tmp_path) -> None:
        from raven.plugin.memory.everos._server import ome_lock_held

        assert ome_lock_held(tmp_path / "never-started") is False

    def test_a_held_ome_lock_is_detected(self, tmp_path) -> None:
        """The signal the whole "served elsewhere" state rests on.

        Also pins the documented flock caveat: the lock lives on the open file
        description, so a holder in *this* process collides with itself. That is
        why only the wizard and doctor -- which hold no engine -- may ask.
        """
        from raven.plugin.memory.everos._server import ome_lock_held
        from raven.utils.portable_lock import file_lock

        lock = tmp_path / "root" / ".index" / "sqlite" / "ome.db.lock"
        lock.parent.mkdir(parents=True)
        lock.touch()

        with file_lock(lock, blocking=False):
            assert ome_lock_held(tmp_path / "root") is True

        assert ome_lock_held(tmp_path / "root") is False, "the lock outlived its holder"

    def test_ps_does_not_mistake_this_process_for_a_server(self) -> None:
        """The pid-reuse guard, run against a real ``ps``.

        A pidfile names a number, not a process; without this check a recycled
        pid would take a SIGTERM meant for an everos server.
        """
        from raven.plugin.memory.everos._server import _is_everos_server

        assert _is_everos_server(os.getpid()) is False

    def test_ps_recognises_a_process_whose_command_line_says_everos(self) -> None:
        import subprocess
        import sys

        from raven.plugin.memory.everos._server import _is_everos_server

        # A stand-in whose command line carries the marker `ps` looks for; the
        # point is that the parsing works against real `ps` output, not that this
        # is an everos build.
        #
        # Deliberately not `sh -c "sleep 5 # marker"`: a shell handed a single
        # simple command execs it directly, replacing its own command line and
        # dropping the marker -- which made this pass alone and fail in a full
        # run. A python interpreter never rewrites its argv, so the marker stays.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)  # everos server start --root /x"])
        try:
            assert _is_everos_server(proc.pid) is True
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_a_dead_pid_is_not_a_server(self) -> None:
        import subprocess

        from raven.plugin.memory.everos._server import _is_everos_server

        proc = subprocess.Popen(["sh", "-c", "exit 0"])
        proc.wait(timeout=5)

        assert _is_everos_server(proc.pid) is False

    def test_health_probe_reads_a_real_response(self, tmp_path) -> None:
        """``_probe_health`` against a real socket: 200 is up, 500 is not, and a
        closed port is not an exception the caller has to handle."""
        import http.server
        import threading

        from raven.plugin.memory.everos._server import _probe_health

        class _Handler(http.server.BaseHTTPRequestHandler):
            status = 200

            def do_GET(self):  # noqa: N802 - stdlib callback name
                self.send_response(self.status)
                self.end_headers()

            def log_message(self, *_a):
                return

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert _probe_health(f"http://127.0.0.1:{port}") is True
            _Handler.status = 503
            assert _probe_health(f"http://127.0.0.1:{port}") is False
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        # Nothing listening any more: a refused connection is False, not a raise.
        assert _probe_health(f"http://127.0.0.1:{port}") is False


def test_the_wait_budget_stays_small() -> None:
    """The timeout is a budget for how long a user waits, not a failure detector.

    Once the poll loop watches the child's exit code, a boot that cannot succeed
    is caught in about a second whatever this value is -- so the only thing left
    to size is the wait, and overrunning it is cheap: the child keeps booting and
    the next session finds it. Pinned because the number reads like a safety
    margin and invites being raised back.
    """
    import inspect

    default = inspect.signature(ensure_everos_server).parameters["timeout"].default
    assert default == 10.0


class TestDeadChildDetection:
    """ "Still booting" and "already dead" are different states.

    The Popen handle used to be discarded, so a child that exited in under a
    second still cost the caller the full poll timeout -- 30s per session, with
    the reason buried in the server log.
    """

    @pytest.fixture
    def _logs(self, tmp_path, monkeypatch):
        monkeypatch.setattr("raven.plugin.memory.everos._server.get_logs_dir", lambda: tmp_path)
        return tmp_path / "everos-server.log"

    @pytest.mark.asyncio
    async def test_exited_child_fails_immediately(self, everos_toml, _logs, monkeypatch) -> None:
        _write_llm_section(everos_toml)
        probes = []

        def _probe(*_a, **_kw):
            probes.append(1)
            return False

        dead = MagicMock()
        dead.poll.return_value = 1
        dead.returncode = 1
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server._start_server_if_unlocked",
            lambda *a, **kw: dead,
        )
        with patch("raven.plugin.memory.everos._server._probe_health", side_effect=_probe):
            with pytest.raises(RuntimeError, match="exited with code 1"):
                await ensure_everos_server("http://localhost:18791", timeout=30.0)

        # Asserting on the probe count rather than wall-clock keeps this
        # deterministic: one pre-loop probe plus one inside the loop, versus the
        # 61 a full 30s budget would have cost.
        assert len(probes) == 2

    @pytest.mark.asyncio
    async def test_failure_carries_the_reason_from_the_log(self, everos_toml, _logs, monkeypatch) -> None:
        _write_llm_section(everos_toml)
        _logs.parent.mkdir(parents=True, exist_ok=True)
        _logs.write_text(
            "some uvicorn noise\n"
            "Traceback (most recent call last):\n"
            '  File "engine.py", line 554, in _acquire_lock\n'
            "everos.infra.ome.exceptions.EngineLockHeldError: another OfflineEngine "
            "instance already holds /tmp/ome.db.lock\n"
            "Application startup failed. Exiting.\n",
            encoding="utf-8",
        )
        dead = MagicMock()
        dead.poll.return_value = 3
        dead.returncode = 3
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server._start_server_if_unlocked",
            lambda *a, **kw: dead,
        )
        with patch("raven.plugin.memory.everos._server._probe_health", return_value=False):
            with pytest.raises(RuntimeError, match="EngineLockHeldError"):
                await ensure_everos_server("http://localhost:18791", timeout=5.0)

    @pytest.mark.asyncio
    async def test_live_child_still_gets_the_full_budget(self, everos_toml, _logs, monkeypatch) -> None:
        """A slow first boot is what the timeout exists for; do not cut it short."""
        _write_llm_section(everos_toml)
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server._start_server_if_unlocked",
            lambda *a, **kw: _live_child(),
        )
        probes = []

        def _probe(*_a, **_kw):
            probes.append(1)
            return False

        with patch("raven.plugin.memory.everos._server._probe_health", side_effect=_probe):
            with pytest.raises(RuntimeError, match="is still starting"):
                await ensure_everos_server("http://localhost:18791", timeout=1.0)

        # One pre-loop probe plus one per 0.5s poll interval across a 1s budget.
        assert len(probes) == 3, "timeout budget was not spent polling"

    @pytest.mark.asyncio
    async def test_another_process_spawning_is_not_treated_as_dead(self, everos_toml, _logs, monkeypatch) -> None:
        """No handle means someone else holds the startup lock, not a dead child."""
        _write_llm_section(everos_toml)
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server._start_server_if_unlocked",
            lambda *a, **kw: None,
        )
        with patch("raven.plugin.memory.everos._server._probe_health", side_effect=[False, True]):
            await ensure_everos_server("http://localhost:18791", timeout=5.0)

    @pytest.mark.asyncio
    async def test_missing_log_degrades_to_no_detail(self, everos_toml, _logs, monkeypatch) -> None:
        _write_llm_section(everos_toml)
        dead = MagicMock()
        dead.poll.return_value = 2
        dead.returncode = 2
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server._start_server_if_unlocked",
            lambda *a, **kw: dead,
        )
        with patch("raven.plugin.memory.everos._server._probe_health", return_value=False):
            with pytest.raises(RuntimeError, match="exited with code 2"):
                await ensure_everos_server("http://localhost:18791", timeout=5.0)


class TestEnsureEverosServer:
    @pytest.mark.asyncio
    async def test_server_already_running(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch(
            "raven.plugin.memory.everos._server._probe_health",
            return_value=True,
        ):
            await ensure_everos_server("http://localhost:18791")

    @pytest.mark.asyncio
    async def test_auto_start_on_connection_error(self, tmp_path, everos_toml) -> None:
        _write_llm_section(everos_toml)
        call_count = 0

        def probe_side_effect(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            return call_count >= 3

        with (
            patch(
                "raven.plugin.memory.everos._server._probe_health",
                side_effect=probe_side_effect,
            ),
            patch(
                "raven.plugin.memory.everos._server._start_server_if_unlocked",
                return_value=_live_child(),
            ) as mock_start,
            patch(
                "raven.plugin.memory.everos._server.get_logs_dir",
                return_value=tmp_path,
            ),
        ):
            await ensure_everos_server("http://localhost:18791", timeout=10.0)

        mock_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_raises(self, tmp_path, everos_toml) -> None:
        """No child of ours to watch (another process holds the startup lock),
        and the address never turns healthy, so the budget is spent and the
        message says the process is still up."""
        _write_llm_section(everos_toml)
        with (
            patch(
                "raven.plugin.memory.everos._server._probe_health",
                return_value=False,
            ),
            patch(
                "raven.plugin.memory.everos._server._start_server_if_unlocked",
                return_value=None,
            ),
            patch(
                "raven.plugin.memory.everos._server.get_logs_dir",
                return_value=tmp_path,
            ),
            pytest.raises(RuntimeError, match="is still starting"),
        ):
            await ensure_everos_server("http://localhost:18791", timeout=1.0)

    def test_port_extraction(self) -> None:
        from raven.plugin.memory.everos._server import _extract_port

        assert _extract_port("http://localhost:18791") == "18791"
        assert _extract_port("http://127.0.0.1:9999") == "9999"
        assert _extract_port("http://localhost") == "80"


class TestProbeClassification:
    """A probe answers *why* it failed, not just that it did.

    Collapsing every failure into ``False`` hid the one distinction the caller
    has to act on: a refused connection is instant and means nobody is
    listening, while a timeout costs the full budget and means something *is*
    listening but not answering. Retrying the first is free; retrying the second
    charges the user again every turn.
    """

    def test_ok_on_200(self) -> None:
        from raven.plugin.memory.everos._server import ProbeResult, probe_health

        resp = MagicMock(status_code=200)
        with patch("httpx.get", return_value=resp):
            assert probe_health("http://localhost:18791") is ProbeResult.OK

    def test_refused_is_distinct_from_timeout(self) -> None:
        import httpx

        from raven.plugin.memory.everos._server import ProbeResult, probe_health

        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            assert probe_health("http://localhost:18791") is ProbeResult.REFUSED

        with patch("httpx.get", side_effect=httpx.ConnectTimeout("slow")):
            assert probe_health("http://localhost:18791") is ProbeResult.TIMEOUT

        with patch("httpx.get", side_effect=httpx.ReadTimeout("hung")):
            assert probe_health("http://localhost:18791") is ProbeResult.TIMEOUT

    def test_non_200_is_error_not_refused(self) -> None:
        from raven.plugin.memory.everos._server import ProbeResult, probe_health

        resp = MagicMock(status_code=503)
        with patch("httpx.get", return_value=resp):
            assert probe_health("http://localhost:18791") is ProbeResult.ERROR

    def test_unexpected_exception_is_error(self) -> None:
        from raven.plugin.memory.everos._server import ProbeResult, probe_health

        with patch("httpx.get", side_effect=ValueError("garbage")):
            assert probe_health("http://localhost:18791") is ProbeResult.ERROR

    def test_bool_wrapper_stays_truthful(self) -> None:
        """``_probe_health`` keeps its bool contract for the callers that only
        need liveness. Returning the enum from it directly would be a silent
        bug: every enum member is truthy, so ``if _probe_health(...)`` would
        pass on a refused connection."""
        import httpx

        from raven.plugin.memory.everos._server import _probe_health

        resp = MagicMock(status_code=200)
        with patch("httpx.get", return_value=resp):
            assert _probe_health("http://localhost:18791") is True
        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            assert _probe_health("http://localhost:18791") is False
        with patch("httpx.get", side_effect=httpx.ReadTimeout("hung")):
            assert _probe_health("http://localhost:18791") is False

    def test_probe_budget_is_one_second(self) -> None:
        """Loopback: if it does not answer in a second it is not answering.

        The probe runs out of band now, but it still has to finish quickly --
        a probe that outlives the turn it was kicked from is a leak, not a
        check.
        """
        from raven.plugin.memory.everos._server import _PROBE_TIMEOUT_S

        assert _PROBE_TIMEOUT_S == 1.0


class TestLockHolderLookup:
    """Who holds this root's data, and where is it actually listening?

    The pidfile answers "did raven start it", which is a record raven keeps
    about itself -- lose the file and a server raven did start reads as
    foreign. The lock answers "who is serving this data" from the OS, which is
    the question that actually blocks a start, and it stays true across a lost
    pidfile, a moved config dir, and a rename.

    Finding the holder's listening port turns the one unrecoverable state --
    the lock is taken and the declared address is silent -- into a recoverable
    one: connect to where it really is instead of killing it.
    """

    def test_returns_none_when_lock_file_absent(self, tmp_path) -> None:
        from raven.plugin.memory.everos._server import lock_holder

        assert lock_holder(tmp_path) is None

    def test_lsof_pid_is_verified_against_the_cmdline(self, tmp_path) -> None:
        """A pid alone is not enough: the number may have been recycled onto an
        unrelated process, and signalling that is the accident this guards."""
        from raven.plugin.memory.everos import _server

        lock = tmp_path / ".index" / "sqlite" / "ome.db.lock"
        lock.parent.mkdir(parents=True)
        lock.touch()

        with (
            patch.object(_server, "_lock_holder_pid", return_value=4242),
            patch.object(_server, "_cmdline_of", return_value="/usr/bin/vim notes.txt"),
        ):
            assert _server.lock_holder(tmp_path) is None

    def test_reports_the_port_the_holder_actually_listens_on(self, tmp_path) -> None:
        from raven.plugin.memory.everos import _server

        lock = tmp_path / ".index" / "sqlite" / "ome.db.lock"
        lock.parent.mkdir(parents=True)
        lock.touch()
        cmdline = f"/venv/bin/python /venv/bin/everos server start --root {tmp_path}"

        with (
            patch.object(_server, "_lock_holder_pid", return_value=4242),
            patch.object(_server, "_cmdline_of", return_value=cmdline),
            patch.object(_server, "_listening_port", return_value=39999),
        ):
            holder = _server.lock_holder(tmp_path)

        assert holder is not None
        assert holder.pid == 4242
        assert holder.port == 39999

    def test_a_holder_with_no_listening_port_is_still_reported(self, tmp_path) -> None:
        """``everos demo`` and an embedded engine hold the lock without serving
        HTTP. Reporting them with ``port=None`` is what lets the wizard say
        "something has your data but there is nowhere to connect" instead of
        silently deciding nobody is there."""
        from raven.plugin.memory.everos import _server

        lock = tmp_path / ".index" / "sqlite" / "ome.db.lock"
        lock.parent.mkdir(parents=True)
        lock.touch()
        cmdline = f"/venv/bin/python /venv/bin/everos server start --root {tmp_path}"

        with (
            patch.object(_server, "_lock_holder_pid", return_value=4242),
            patch.object(_server, "_cmdline_of", return_value=cmdline),
            patch.object(_server, "_listening_port", return_value=None),
        ):
            holder = _server.lock_holder(tmp_path)

        assert holder is not None
        assert holder.port is None

    def test_falls_back_to_the_pidfile_when_the_os_cannot_answer(self, tmp_path) -> None:
        from raven.plugin.memory.everos import _server

        lock = tmp_path / ".index" / "sqlite" / "ome.db.lock"
        lock.parent.mkdir(parents=True)
        lock.touch()
        cmdline = f"/venv/bin/python /venv/bin/everos server start --root {tmp_path}"

        with (
            patch.object(_server, "_lsof_lock_pid", return_value=None),
            patch.object(_server, "_proc_locks_pid", return_value=None),
            patch.object(_server, "_read_pidfile", return_value={"pid": 77, "root": str(tmp_path)}),
            patch.object(_server, "_cmdline_of", return_value=cmdline),
            patch.object(_server, "_listening_port", return_value=18791),
        ):
            holder = _server.lock_holder(tmp_path)

        assert holder is not None
        assert holder.pid == 77


class TestTheWrittenAddressIsVerified:
    """Writing ``[api]`` and then spawning assumes the write landed.

    Dropping ``--port`` made the toml the single authority on where a server
    listens; that only holds if the file really says what raven thinks it
    says. An unverified write turns a failed edit into a server on the wrong
    port -- exactly the drift the change was meant to end.
    """

    def test_readback_mismatch_refuses_to_spawn(self, tmp_path, monkeypatch) -> None:
        from raven.plugin.memory.everos import _server

        root = tmp_path / "everos"
        root.mkdir()
        (root / "everos.toml").write_text('[api]\nhost = "127.0.0.1"\nport = 8000\n')

        monkeypatch.setattr(_server, "_everos_executable", lambda: "/bin/true")
        monkeypatch.setattr("raven.config.update_everos.everos_root", lambda: root)
        # A write that silently does not take: the readback is the only thing
        # standing between this and a server bound to 8000 while raven probes 18791.
        monkeypatch.setattr("raven.config.update_everos.set_everos_api", lambda **kw: None)

        with pytest.raises(RuntimeError, match="did not take effect"):
            _server._start_server_if_unlocked("http://localhost:18791")


class TestParsingLsofListenOutput:
    """Real ``lsof`` rows, because the shape is what the parser got wrong.

    The address is neither the last field -- ``(LISTEN)`` is -- nor at a fixed
    index, since COMMAND is padded to the widest value in the output. Taking
    the last field silently found no port at all, so a server that was plainly
    listening looked like a lock holder serving nothing.
    """

    def test_ipv4_row(self) -> None:
        from raven.plugin.memory.everos._server import _parse_listen_port

        out = (
            "COMMAND     PID  USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME\n"
            "python3.1 94107 admin   32u  IPv4 0x88b36e3af566b544      0t0  TCP 127.0.0.1:31995 (LISTEN)\n"
        )
        assert _parse_listen_port(out) == 31995

    def test_ipv6_row(self) -> None:
        from raven.plugin.memory.everos._server import _parse_listen_port

        out = (
            "COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
            "everos   4242 admin   11u  IPv6  0x1e2      0t0  TCP [::1]:18791 (LISTEN)\n"
        )
        assert _parse_listen_port(out) == 18791

    def test_wildcard_row(self) -> None:
        from raven.plugin.memory.everos._server import _parse_listen_port

        out = (
            "COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
            "everos   4242 admin   11u  IPv4  0x1e2      0t0  TCP *:8000 (LISTEN)\n"
        )
        assert _parse_listen_port(out) == 8000

    def test_header_only_is_no_port(self) -> None:
        from raven.plugin.memory.everos._server import _parse_listen_port

        assert _parse_listen_port("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n") is None

    def test_empty_output_is_no_port(self) -> None:
        from raven.plugin.memory.everos._server import _parse_listen_port

        assert _parse_listen_port("") is None


class TestParsingProcLocks:
    """Rows captured from a real Linux /proc/locks (kernel 6.8, Ubuntu).

    This branch cannot run on the development machine, so the format is pinned
    from a live capture rather than from the documentation. The waiter row is
    the reason: a process blocked on the same lock is listed with a "->" prefix
    that shifts every field right by one, and a parser that read the pid
    positionally without noticing would hand back the waiter. The caller of
    ``lock_holder`` goes on to signal what it is told.
    """

    @staticmethod
    def _parse(rows: str, inode: int, tmp_path, monkeypatch) -> int | None:
        from raven.plugin.memory.everos import _server

        lock = tmp_path / "ome.db.lock"
        lock.touch()
        monkeypatch.setattr(Path, "read_text", lambda self, **kw: rows)
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(_server.Path, "stat", lambda self, **kw: SimpleNamespace(st_ino=inode))
        return _server._proc_locks_pid(lock)

    def test_a_holder_row(self, tmp_path, monkeypatch) -> None:
        rows = "3: FLOCK  ADVISORY  WRITE 1550263 fc:00:4980767 0 EOF\n"
        assert self._parse(rows, 4980767, tmp_path, monkeypatch) == 1550263

    def test_a_waiter_never_wins_whatever_the_order(self, tmp_path, monkeypatch) -> None:
        holder = "2: FLOCK  ADVISORY  WRITE 1550263 fc:00:4980768 0 EOF"
        waiter = "2: -> FLOCK  ADVISORY  WRITE 1550264 fc:00:4980768 0 EOF"

        for rows in (f"{holder}\n{waiter}\n", f"{waiter}\n{holder}\n"):
            assert self._parse(rows, 4980768, tmp_path, monkeypatch) == 1550263

    def test_an_unrelated_inode_is_not_matched(self, tmp_path, monkeypatch) -> None:
        rows = "3: FLOCK  ADVISORY  WRITE 1550263 fc:00:4980767 0 EOF\n"
        assert self._parse(rows, 999999, tmp_path, monkeypatch) is None


class TestStoppingWhatTheLockNamed:
    """The pidfile finds the target for a stop; the lock finds it for a lookup.

    Letting those disagree defeats the reason the lock lookup exists. A server
    raven started and then lost the pidfile for is exactly the case the lock was
    added to recover, and a caller that identifies the holder that way and then
    stops "the recorded server" gets NOT_OURS for a process it just named.
    """

    def test_a_pid_from_the_lock_can_be_stopped(self, monkeypatch) -> None:
        from raven.plugin.memory.everos import _server

        signalled: list[int] = []
        monkeypatch.setattr(_server.os, "kill", lambda pid, sig: signalled.append(pid))
        # First poll says still alive, second says gone.
        alive = iter([True, False])
        monkeypatch.setattr(_server, "_is_everos_server", lambda _p: next(alive, False))
        monkeypatch.setattr(_server.time, "sleep", lambda _s: None)
        monkeypatch.setattr(_server, "_pidfile_path", lambda: Path("/nonexistent/everos-server.pid"))

        assert _server.stop_pid(4242) is _server.StopOutcome.STOPPED
        assert signalled == [4242]

    def test_an_undeliverable_signal_is_reported(self, monkeypatch) -> None:
        from raven.plugin.memory.everos import _server

        def _boom(_pid, _sig):
            raise PermissionError("not yours")

        monkeypatch.setattr(_server.os, "kill", _boom)
        assert _server.stop_pid(4242) is _server.StopOutcome.SIGNAL_FAILED

    def test_a_process_that_will_not_go_is_reported_as_draining(self, monkeypatch) -> None:
        from raven.plugin.memory.everos import _server

        monkeypatch.setattr(_server.os, "kill", lambda _p, _s: None)
        monkeypatch.setattr(_server, "_is_everos_server", lambda _p: True)
        monkeypatch.setattr(_server.time, "sleep", lambda _s: None)

        assert _server.stop_pid(4242, timeout=1.0) is _server.StopOutcome.STILL_DRAINING


class TestAnUnwritableRootFailsAsAStartFailure:
    """A root raven cannot write to must read as "the server did not start".

    The address is written into the root before the child is spawned, so a
    directory that refuses the write raises OSError from deep inside the start
    path. Every caller guards with ``except RuntimeError`` -- the wizard, the
    backend -- because that is what "could not start" has always been. An
    OSError walked past all of them and ended the wizard on a traceback, in a
    situation the user can act on: fix the permissions and run it again.
    """

    def test_it_surfaces_as_runtime_error(self, tmp_path, monkeypatch) -> None:
        from raven.plugin.memory.everos import _server

        root = tmp_path / "everos"
        root.mkdir()
        (root / "everos.toml").write_text('[api]\nhost = "127.0.0.1"\nport = 8000\n')
        root.chmod(0o555)
        monkeypatch.setattr(_server, "_everos_executable", lambda: "/bin/true")
        monkeypatch.setattr("raven.config.update_everos.everos_root", lambda: root)
        monkeypatch.setattr("raven.config.update_everos.everos_owned", lambda: True)
        try:
            with pytest.raises(RuntimeError) as caught:
                _server._start_server_if_unlocked("http://localhost:18791")
        finally:
            root.chmod(0o755)

        assert "everos.toml" in str(caught.value) or "write" in str(caught.value).lower()
