"""Unit tests for sandbox CLI subcommands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from raven.cli.sandbox_commands import sandbox_app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vm(
    id: str = "box1",
    name: str | None = None,
    owned: bool = True,
    status: str = "running",
    image: str = "ubuntu:22.04",
    cpus: int = 2,
    memory_mib: int = 2048,
    created_at: str | None = None,
) -> dict:
    return {
        "id": id,
        "name": name,
        "owned": owned,
        "status": status,
        "image": image,
        "cpus": cpus,
        "memory_mib": memory_mib,
        "created_at": created_at,
    }


def _mock_transport(recv_response: dict):
    """Return patched _connect, _send, _recv, _close for a single round-trip."""
    mock_reader = AsyncMock()
    mock_writer = MagicMock()
    mock_connect = AsyncMock(return_value=(mock_reader, mock_writer))
    mock_send = AsyncMock()
    mock_recv = AsyncMock(return_value=recv_response)
    mock_close = MagicMock()
    return mock_connect, mock_send, mock_recv, mock_close


# ---------------------------------------------------------------------------
# list / ls
# ---------------------------------------------------------------------------


class TestListCommand:
    def test_missing_socket_exits_1(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.sock"
        with patch("raven.cli.sandbox_commands._get_socket_path", return_value=missing):
            result = runner.invoke(sandbox_app, ["list"])
        assert result.exit_code == 1
        output = result.output.lower()
        assert "debug socket" in output or "not found" in output

    def test_ls_alias_exits_same_as_list(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.sock"
        with patch("raven.cli.sandbox_commands._get_socket_path", return_value=missing):
            r_list = runner.invoke(sandbox_app, ["list"])
            r_ls = runner.invoke(sandbox_app, ["ls"])
        assert r_list.exit_code == r_ls.exit_code

    def test_empty_vm_list_shows_no_vms(self, tmp_path: Path) -> None:
        path = tmp_path / "debug.sock"
        path.touch()
        mock_connect, mock_send, mock_recv, mock_close = _mock_transport({"type": "vm_list", "vms": []})
        with (
            patch("raven.cli.sandbox_commands._get_socket_path", return_value=path),
            patch("raven.cli.sandbox_commands._connect", mock_connect),
            patch("raven.cli.sandbox_commands._send", mock_send),
            patch("raven.cli.sandbox_commands._recv", mock_recv),
            patch("raven.cli.sandbox_commands._close", mock_close),
        ):
            result = runner.invoke(sandbox_app, ["list"])
        assert result.exit_code == 0
        assert "no vms" in result.output.lower()

    def test_server_error_exits_1(self, tmp_path: Path) -> None:
        path = tmp_path / "debug.sock"
        path.touch()
        mock_connect, mock_send, mock_recv, mock_close = _mock_transport(
            {"type": "error", "message": "runtime unavailable"}
        )
        with (
            patch("raven.cli.sandbox_commands._get_socket_path", return_value=path),
            patch("raven.cli.sandbox_commands._connect", mock_connect),
            patch("raven.cli.sandbox_commands._send", mock_send),
            patch("raven.cli.sandbox_commands._recv", mock_recv),
            patch("raven.cli.sandbox_commands._close", mock_close),
        ):
            result = runner.invoke(sandbox_app, ["list"])
        assert result.exit_code == 1
        assert "runtime unavailable" in result.output

    def test_vm_list_displayed_as_table(self, tmp_path: Path) -> None:
        path = tmp_path / "debug.sock"
        path.touch()
        vms = [
            _make_vm(id="abc123", name="my-vm", owned=True, status="running"),
            _make_vm(id="def456", name=None, owned=False, status="stopped"),
        ]
        mock_connect, mock_send, mock_recv, mock_close = _mock_transport({"type": "vm_list", "vms": vms})
        with (
            patch("raven.cli.sandbox_commands._get_socket_path", return_value=path),
            patch("raven.cli.sandbox_commands._connect", mock_connect),
            patch("raven.cli.sandbox_commands._send", mock_send),
            patch("raven.cli.sandbox_commands._recv", mock_recv),
            patch("raven.cli.sandbox_commands._close", mock_close),
        ):
            result = runner.invoke(sandbox_app, ["list"])
        assert result.exit_code == 0
        assert "abc123" in result.output
        assert "def456" in result.output

    def test_send_called_with_list_cmd(self, tmp_path: Path) -> None:
        """Verify the client sends {"cmd": "list"} to the server."""
        path = tmp_path / "debug.sock"
        path.touch()
        mock_connect, mock_send, mock_recv, mock_close = _mock_transport({"type": "vm_list", "vms": []})
        with (
            patch("raven.cli.sandbox_commands._get_socket_path", return_value=path),
            patch("raven.cli.sandbox_commands._connect", mock_connect),
            patch("raven.cli.sandbox_commands._send", mock_send),
            patch("raven.cli.sandbox_commands._recv", mock_recv),
            patch("raven.cli.sandbox_commands._close", mock_close),
        ):
            runner.invoke(sandbox_app, ["list"])
        mock_send.assert_called_once()
        sent_obj = mock_send.call_args[0][1]
        assert sent_obj == {"cmd": "list"}


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------


class TestExecCommand:
    def test_no_command_exits_1(self, tmp_path: Path) -> None:
        path = tmp_path / "debug.sock"
        path.touch()
        with patch("raven.cli.sandbox_commands._get_socket_path", return_value=path):
            result = runner.invoke(sandbox_app, ["exec"])
        assert result.exit_code == 1
        assert "required" in result.output.lower() or "command" in result.output.lower()

    def test_missing_socket_exits_1(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.sock"
        with patch("raven.cli.sandbox_commands._get_socket_path", return_value=missing):
            result = runner.invoke(sandbox_app, ["exec", "ls"])
        assert result.exit_code == 1

    def test_server_error_exits_1(self, tmp_path: Path) -> None:
        path = tmp_path / "debug.sock"
        path.touch()
        mock_connect, mock_send, mock_recv, mock_close = _mock_transport({"type": "error", "message": "VM not found"})
        with (
            patch("raven.cli.sandbox_commands._get_socket_path", return_value=path),
            patch("raven.cli.sandbox_commands._connect", mock_connect),
            patch("raven.cli.sandbox_commands._send", mock_send),
            patch("raven.cli.sandbox_commands._recv", mock_recv),
            patch("raven.cli.sandbox_commands._close", mock_close),
        ):
            result = runner.invoke(sandbox_app, ["exec", "ls"])
        assert result.exit_code == 1
        assert "VM not found" in result.output

    def test_sends_correct_request(self, tmp_path: Path) -> None:
        path = tmp_path / "debug.sock"
        path.touch()

        # Simulate: stdout chunk then exit
        recv_responses = [
            {"type": "exit", "code": 0},
        ]
        recv_mock = AsyncMock(side_effect=recv_responses)
        mock_reader = AsyncMock()
        mock_writer = MagicMock()
        mock_connect = AsyncMock(return_value=(mock_reader, mock_writer))

        with (
            patch("raven.cli.sandbox_commands._get_socket_path", return_value=path),
            patch("raven.cli.sandbox_commands._connect", mock_connect),
            patch("raven.cli.sandbox_commands._send", AsyncMock()) as mock_send,
            patch("raven.cli.sandbox_commands._recv", recv_mock),
            patch("raven.cli.sandbox_commands._close"),
        ):
            runner.invoke(sandbox_app, ["exec", "--vm", "my-vm", "ls", "-la"])

        sent = mock_send.call_args[0][1]
        assert sent["cmd"] == "exec"
        assert sent["vm_ref"] == "my-vm"
        assert sent["program"] == "ls"
        assert sent.get("args") == ["-la"]

    def test_exit_code_propagated(self, tmp_path: Path) -> None:
        path = tmp_path / "debug.sock"
        path.touch()
        mock_connect, mock_send, mock_recv, mock_close = _mock_transport({"type": "exit", "code": 42})
        with (
            patch("raven.cli.sandbox_commands._get_socket_path", return_value=path),
            patch("raven.cli.sandbox_commands._connect", mock_connect),
            patch("raven.cli.sandbox_commands._send", mock_send),
            patch("raven.cli.sandbox_commands._recv", mock_recv),
            patch("raven.cli.sandbox_commands._close", mock_close),
        ):
            result = runner.invoke(sandbox_app, ["exec", "false"])
        assert result.exit_code == 42


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------


class TestShellCommand:
    def test_missing_socket_exits_1(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.sock"
        with patch("raven.cli.sandbox_commands._get_socket_path", return_value=missing):
            result = runner.invoke(sandbox_app, ["shell"])
        assert result.exit_code == 1

    def test_server_error_before_ready_exits_1(self, tmp_path: Path) -> None:
        """If the server returns an error instead of ready, terminal must NOT enter raw mode."""
        path = tmp_path / "debug.sock"
        path.touch()
        mock_connect, mock_send, mock_recv, mock_close = _mock_transport({"type": "error", "message": "VM not found"})
        with (
            patch("raven.cli.sandbox_commands._get_socket_path", return_value=path),
            patch("raven.cli.sandbox_commands._connect", mock_connect),
            patch("raven.cli.sandbox_commands._send", mock_send),
            patch("raven.cli.sandbox_commands._recv", mock_recv),
            patch("raven.cli.sandbox_commands._close", mock_close),
        ):
            result = runner.invoke(sandbox_app, ["shell"])
        assert result.exit_code == 1
        assert "VM not found" in result.output

    def test_sends_correct_shell_command(self, tmp_path: Path) -> None:
        path = tmp_path / "debug.sock"
        path.touch()

        recv_responses = [
            {"type": "error", "message": "stopped"},  # error before ready — avoids raw mode
        ]
        recv_mock = AsyncMock(side_effect=recv_responses)
        mock_reader = AsyncMock()
        mock_writer = MagicMock()
        mock_connect = AsyncMock(return_value=(mock_reader, mock_writer))

        with (
            patch("raven.cli.sandbox_commands._get_socket_path", return_value=path),
            patch("raven.cli.sandbox_commands._connect", mock_connect),
            patch("raven.cli.sandbox_commands._send", AsyncMock()) as mock_send,
            patch("raven.cli.sandbox_commands._recv", recv_mock),
            patch("raven.cli.sandbox_commands._close"),
        ):
            runner.invoke(sandbox_app, ["shell", "--vm", "my-vm", "--shell", "/bin/bash"])

        sent = mock_send.call_args_list[0][0][1]
        assert sent["cmd"] == "shell"
        assert sent["vm_ref"] == "my-vm"
        assert sent["shell"] == "/bin/bash"

    def test_default_shell_is_bin_sh(self, tmp_path: Path) -> None:
        path = tmp_path / "debug.sock"
        path.touch()

        recv_mock = AsyncMock(return_value={"type": "error", "message": "no vms"})
        mock_connect = AsyncMock(return_value=(AsyncMock(), MagicMock()))

        with (
            patch("raven.cli.sandbox_commands._get_socket_path", return_value=path),
            patch("raven.cli.sandbox_commands._connect", mock_connect),
            patch("raven.cli.sandbox_commands._send", AsyncMock()) as mock_send,
            patch("raven.cli.sandbox_commands._recv", recv_mock),
            patch("raven.cli.sandbox_commands._close"),
        ):
            runner.invoke(sandbox_app, ["shell"])

        sent = mock_send.call_args_list[0][0][1]
        assert sent["shell"] == "/bin/sh"


# ---------------------------------------------------------------------------
# _get_socket_path wiring (regression test for the cfg.tools.sandbox.debug.socket
# attribute path — every other test in this file patches _get_socket_path away,
# so without this test the wiring inside it is never exercised).
# ---------------------------------------------------------------------------


class TestGetSocketPath:
    def test_reads_configured_socket_path(self, tmp_path: Path) -> None:
        from raven.cli.sandbox_commands import _get_socket_path
        from raven.config.schema import Config
        from raven.sandbox.config import SandboxDebugConfig

        cfg = Config()
        cfg.tools.sandbox.debug = SandboxDebugConfig(enabled=True, socket="custom/path.sock")
        with (
            patch("raven.config.loader.load_config", return_value=cfg),
            patch("raven.config.paths.get_data_dir", return_value=tmp_path),
        ):
            result = _get_socket_path()
        assert result == tmp_path / "custom" / "path.sock"

    def test_falls_back_to_default_when_config_missing(self, tmp_path: Path) -> None:
        from raven.cli.sandbox_commands import _get_socket_path

        with (
            patch(
                "raven.config.loader.load_config",
                side_effect=FileNotFoundError("no config file"),
            ),
            patch("raven.config.paths.get_data_dir", return_value=tmp_path),
        ):
            result = _get_socket_path()
        assert result == tmp_path / "sandbox" / "debug.sock"

    def test_falls_back_to_default_when_config_load_fails(self, tmp_path: Path, caplog) -> None:
        """A malformed/unreadable config must not crash the CLI — the broad
        except in _get_socket_path should log a warning and fall back to the
        default socket path."""
        import logging

        from raven.cli.sandbox_commands import _get_socket_path

        with (
            patch(
                "raven.config.loader.load_config",
                side_effect=ValueError("malformed yaml at line 7"),
            ),
            patch("raven.config.paths.get_data_dir", return_value=tmp_path),
            caplog.at_level(logging.WARNING, logger="raven.cli.sandbox_commands"),
        ):
            result = _get_socket_path()

        assert result == tmp_path / "sandbox" / "debug.sock"
        assert any("malformed yaml" in rec.message for rec in caplog.records), (
            "the underlying config error must be logged so the user can correlate"
        )


# ---------------------------------------------------------------------------
# Recv robustness (M2): malformed / empty server responses must not surface
# as Python tracebacks in the user's terminal.
# ---------------------------------------------------------------------------


class TestRecvRobustness:
    def test_empty_response_exits_with_clean_error(self, tmp_path: Path) -> None:
        path = tmp_path / "debug.sock"
        path.touch()

        # readline() returning b"" simulates the server closing the connection
        # before sending a response.
        mock_reader = MagicMock()
        mock_reader.readline = AsyncMock(return_value=b"")
        mock_writer = MagicMock()

        with (
            patch("raven.cli.sandbox_commands._get_socket_path", return_value=path),
            patch(
                "raven.cli.sandbox_commands._connect",
                AsyncMock(return_value=(mock_reader, mock_writer)),
            ),
            patch("raven.cli.sandbox_commands._send", AsyncMock()),
            patch("raven.cli.sandbox_commands._close"),
        ):
            result = runner.invoke(sandbox_app, ["list"])

        assert result.exit_code == 1
        assert "server closed connection" in result.output.lower()

    def test_malformed_json_exits_with_clean_error(self, tmp_path: Path) -> None:
        path = tmp_path / "debug.sock"
        path.touch()

        mock_reader = MagicMock()
        mock_reader.readline = AsyncMock(return_value=b"not json at all\n")
        mock_writer = MagicMock()

        with (
            patch("raven.cli.sandbox_commands._get_socket_path", return_value=path),
            patch(
                "raven.cli.sandbox_commands._connect",
                AsyncMock(return_value=(mock_reader, mock_writer)),
            ),
            patch("raven.cli.sandbox_commands._send", AsyncMock()),
            patch("raven.cli.sandbox_commands._close"),
        ):
            result = runner.invoke(sandbox_app, ["list"])

        assert result.exit_code == 1
        assert "malformed response" in result.output.lower()
