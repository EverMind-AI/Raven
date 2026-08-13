"""Tests for raven.plugin.memory.everos._server."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from raven.plugin.memory.everos._server import (
    EverosNotConfiguredError,
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
    """Redirect the EverOS config read to a throwaway toml."""
    import raven.config.update_everos as ue

    cfg = tmp_path / ".everos" / "everos.toml"
    monkeypatch.setattr(ue, "_EVEROS_CONFIG", cfg)
    return cfg


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
        with patch("raven.plugin.memory.everos._server._probe_health", return_value=True):
            await ensure_everos_server("http://localhost:18791")

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
            with pytest.raises(RuntimeError, match="did not become healthy"):
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
            pytest.raises(RuntimeError, match="did not become healthy"),
        ):
            await ensure_everos_server("http://localhost:18791", timeout=1.0)

    def test_port_extraction(self) -> None:
        from raven.plugin.memory.everos._server import _extract_port

        assert _extract_port("http://localhost:18791") == "18791"
        assert _extract_port("http://127.0.0.1:9999") == "9999"
        assert _extract_port("http://localhost") == "80"
