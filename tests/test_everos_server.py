"""Tests for raven.plugin.memory.everos._server."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from raven.plugin.memory.everos._server import _everos_executable, ensure_everos_server


class TestEnsureEverosServer:
    @pytest.mark.asyncio
    async def test_server_already_running(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200

        with (
            patch("raven.plugin.memory.everos._server._probe_health", return_value=True),
            patch("raven.plugin.memory.everos._server._speaks_our_api", return_value=True),
        ):
            await ensure_everos_server("http://localhost:18791")

    @pytest.mark.asyncio
    async def test_a_live_server_that_cannot_serve_our_api_is_refused(self) -> None:
        """Health is not a handshake.

        An older EverOS answers /health perfectly and 404s every call the client
        makes afterwards. Adopting it is what made that failure silent -- a
        store failure is swallowed per turn, so the pairing ran for days looking
        healthy while nothing was written and nothing recalled.
        """
        with (
            patch("raven.plugin.memory.everos._server._probe_health", return_value=True),
            patch("raven.plugin.memory.everos._server._speaks_our_api", return_value=False),
            patch("raven.plugin.memory.everos._server._start_server_if_unlocked") as start,
        ):
            with pytest.raises(RuntimeError, match="too old for this raven"):
                await ensure_everos_server("http://localhost:18791")
        # Refused, not worked around: starting a second one on the same port
        # would only fail to bind.
        start.assert_not_called()


class TestEverosExecutable:
    def test_prefers_the_interpreter_s_own_environment(self, tmp_path, monkeypatch) -> None:
        """EverOS is a pinned raven dependency, so the server we launch must be
        that copy -- not whichever one a PATH lookup happens to find first."""
        binder = tmp_path / "bin"
        binder.mkdir()
        mine = binder / "everos"
        mine.write_text("#!/bin/sh\n")
        monkeypatch.setattr("sys.executable", str(binder / "python"))
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server.shutil.which",
            lambda _n: "/somewhere/else/everos",
        )
        assert _everos_executable() == str(mine)

    def test_falls_back_to_path(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("sys.executable", str(tmp_path / "nothing" / "python"))
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server.shutil.which",
            lambda _n: "/usr/local/bin/everos",
        )
        assert _everos_executable() == "/usr/local/bin/everos"

    def test_none_when_neither_exists(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("sys.executable", str(tmp_path / "nothing" / "python"))
        monkeypatch.setattr("raven.plugin.memory.everos._server.shutil.which", lambda _n: None)
        assert _everos_executable() is None

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
