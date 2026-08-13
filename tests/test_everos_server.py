"""Tests for raven.plugin.memory.everos._server."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from raven.plugin.memory.everos._server import _everos_executable, ensure_everos_server


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
    async def test_auto_start_on_connection_error(self, tmp_path) -> None:
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
            ) as mock_start,
            patch(
                "raven.plugin.memory.everos._server.get_logs_dir",
                return_value=tmp_path,
            ),
        ):
            await ensure_everos_server("http://localhost:18791", timeout=10.0)

        mock_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_raises(self, tmp_path) -> None:
        with (
            patch(
                "raven.plugin.memory.everos._server._probe_health",
                return_value=False,
            ),
            patch(
                "raven.plugin.memory.everos._server._start_server_if_unlocked",
            ),
            patch(
                "raven.plugin.memory.everos._server.get_logs_dir",
                return_value=tmp_path,
            ),
            pytest.raises(RuntimeError, match="EverOS server failed to start"),
        ):
            await ensure_everos_server("http://localhost:18791", timeout=1.0)

    def test_port_extraction(self) -> None:
        from raven.plugin.memory.everos._server import _extract_port

        assert _extract_port("http://localhost:18791") == "18791"
        assert _extract_port("http://127.0.0.1:9999") == "9999"
        assert _extract_port("http://localhost") == "80"
