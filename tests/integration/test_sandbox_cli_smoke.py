"""Integration tests: CLI commands → real SandboxDebugServer → mocked boxlite.

All four commands (list, ls, exec, shell) exercise the full JSON protocol over
a real Unix domain socket. Only boxlite is mocked — no KVM or real VM required.

Each test starts a real SandboxDebugServer, patches boxlite.Boxlite at the
server side, and invokes the CLI via run_in_executor so the test event loop
can still service the server while the CLI's asyncio.run() runs in a thread.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from raven.cli.sandbox_commands import sandbox_app
from raven.sandbox.debug_server import SandboxDebugServer

runner = CliRunner(mix_stderr=False)


# ── fixtures & helpers ─────────────────────────────────────────────────────────


@pytest.fixture
def sock_dir():
    d = tempfile.mkdtemp(prefix="ec_cli_int_", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
async def server(sock_dir):
    path = sock_dir / "debug.sock"
    srv = SandboxDebugServer(path, {"b1"})
    await srv.start()
    yield path, srv
    await srv.stop()


def _box(id="b1", name=None, status="running", image="ubuntu:22.04", cpus=2, memory_mib=2048):
    info = MagicMock()
    info.id = id
    info.name = name
    info.state.status = status
    info.image = image
    info.cpus = cpus
    info.memory_mib = memory_mib
    info.created_at = "2025-01-15T10:30:00+00:00"
    return info


def _execution(stdout_chunks=(), stderr_chunks=(), exit_code=0):
    async def _stdout():
        for chunk in stdout_chunks:
            yield chunk

    async def _stderr():
        for chunk in stderr_chunks:
            yield chunk

    ex = MagicMock()
    ex.stdout = MagicMock(return_value=_stdout())
    ex.stderr = MagicMock(return_value=_stderr())
    ex.wait = AsyncMock(return_value=MagicMock(exit_code=exit_code))
    ex.kill = AsyncMock()
    ex.stdin = MagicMock(return_value=MagicMock(send_input=AsyncMock()))
    ex.resize_tty = AsyncMock()
    return ex


async def _invoke(args, socket_path):
    """Run CLI in executor so the test event loop can service the server."""

    def _run():
        with patch("raven.cli.sandbox_commands._get_socket_path", return_value=socket_path):
            return runner.invoke(sandbox_app, args)

    return await asyncio.get_event_loop().run_in_executor(None, _run)


async def _invoke_shell(args, socket_path):
    """Run shell command in executor with TTY functions patched.

    Patches raven.cli.sandbox_commands.sys (not the global sys) so CliRunner's
    stdin replacement cannot override our mock.  TTY/signal calls are stubbed to
    avoid errors when running outside a real terminal or from a non-main thread.

    Uses a real OS pipe for the mock stdin fd so loop.add_reader() succeeds —
    fd 0 (stdin) gives EINVAL from kqueue in non-TTY test environments.
    """
    import os as _os
    import sys as _real_sys

    r_fd, w_fd = _os.pipe()

    mock_sys = MagicMock(wraps=_real_sys)
    mock_sys.stdin = MagicMock()
    mock_sys.stdin.fileno.return_value = r_fd

    def _run():
        try:
            with (
                patch("raven.cli.sandbox_commands._get_socket_path", return_value=socket_path),
                patch("raven.cli.sandbox_commands.sys", mock_sys),
                patch("tty.setraw"),
                patch("termios.tcgetattr", return_value=[]),
                patch("termios.tcsetattr"),
                patch("signal.signal"),
            ):
                return runner.invoke(sandbox_app, args)
        finally:
            _os.close(r_fd)
            _os.close(w_fd)

    return await asyncio.get_event_loop().run_in_executor(None, _run)


def _mock_runtime(boxes=(), box=None, execution=None):
    rt = MagicMock()
    rt.list_info = AsyncMock(return_value=list(boxes))
    rt.get = AsyncMock(return_value=box)
    if box is not None and execution is not None:
        box.exec = AsyncMock(return_value=execution)
    return rt


# ── list / ls ──────────────────────────────────────────────────────────────────


class TestListIntegration:
    async def test_running_vm_appears_in_table(self, server):
        path, _ = server
        rt = _mock_runtime(boxes=[_box(id="abc123")])
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke(["list"], path)
        assert result.exit_code == 0
        assert "abc123" in result.output

    async def test_owned_marker_distinguished(self, server):
        path, _ = server
        boxes = [_box(id="b1", name="owned"), _box(id="b2", name="other")]
        rt = _mock_runtime(boxes=boxes)
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke(["list"], path)
        assert result.exit_code == 0
        assert "*" in result.output  # owned marker for b1
        assert "-" in result.output  # unowned marker for b2

    async def test_empty_list_message(self, server):
        path, _ = server
        rt = _mock_runtime(boxes=[])
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke(["list"], path)
        assert result.exit_code == 0
        assert "no vms" in result.output.lower()

    async def test_ls_alias_same_behavior(self, server):
        path, _ = server
        rt = _mock_runtime(boxes=[_box(id="abc123")])
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            r_list = await _invoke(["list"], path)
            cls.return_value = _mock_runtime(boxes=[_box(id="abc123")])
            r_ls = await _invoke(["ls"], path)
        assert r_list.exit_code == r_ls.exit_code == 0
        assert "abc123" in r_list.output
        assert "abc123" in r_ls.output

    async def test_server_error_shown_and_exits_1(self, server):
        path, _ = server
        rt = MagicMock()
        rt.list_info = AsyncMock(side_effect=RuntimeError("db locked"))
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke(["list"], path)
        assert result.exit_code == 1
        assert "db locked" in result.output

    async def test_vm_fields_in_output(self, server):
        path, _ = server
        rt = _mock_runtime(boxes=[_box(id="zz99", image="alpine:latest", cpus=4, memory_mib=1024)])
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke(["list"], path)
        assert "zz99" in result.output
        assert "alpine" in result.output
        assert "4" in result.output
        assert "1024" in result.output


# ── exec ───────────────────────────────────────────────────────────────────────


class TestExecIntegration:
    async def test_stdout_appears_in_output(self, server):
        path, _ = server
        mock_box = MagicMock()
        ex = _execution(stdout_chunks=["hello from vm\n"])
        rt = _mock_runtime(boxes=[_box()], box=mock_box, execution=ex)
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke(["exec", "echo", "hello from vm"], path)
        assert result.exit_code == 0
        assert "hello from vm" in result.output

    async def test_exit_code_propagated(self, server):
        path, _ = server
        mock_box = MagicMock()
        ex = _execution(exit_code=42)
        rt = _mock_runtime(boxes=[_box()], box=mock_box, execution=ex)
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke(["exec", "false"], path)
        assert result.exit_code == 42

    async def test_vm_ref_forwarded_to_server(self, server):
        path, _ = server
        # Server owns b2, not b1 — provide b2 as the owned/running VM
        _, srv = server
        srv._owned_ids.add("b2")
        mock_box = MagicMock()
        ex = _execution(exit_code=0)
        boxes = [_box(id="b1", status="stopped"), _box(id="b2")]
        rt = _mock_runtime(boxes=boxes, box=mock_box, execution=ex)
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke(["exec", "--vm", "b2", "ls"], path)
        assert result.exit_code == 0

    async def test_no_running_vm_shows_error(self, server):
        path, _ = server
        rt = _mock_runtime(boxes=[_box(id="b1", status="stopped")])
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke(["exec", "ls"], path)
        assert result.exit_code == 1
        assert "no running vms" in result.output.lower()

    async def test_unknown_vm_ref_shows_error(self, server):
        path, _ = server
        rt = _mock_runtime(boxes=[_box(id="b1")])
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke(["exec", "--vm", "nope", "ls"], path)
        assert result.exit_code == 1
        assert "no vm found" in result.output.lower()


# ── shell ──────────────────────────────────────────────────────────────────────


class TestShellIntegration:
    async def test_error_before_ready_shown(self, server):
        path, _ = server
        rt = _mock_runtime(boxes=[_box(id="b1", status="stopped")])
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke(["shell"], path)
        assert result.exit_code == 1
        assert "no running vms" in result.output.lower()

    async def test_vm_ref_and_shell_flags_forwarded(self, server):
        """--vm and --shell reach the server even when the connection then errors."""
        path, _ = server
        rt = _mock_runtime(boxes=[_box(id="b1", status="stopped")])
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke(["shell", "--vm", "b1", "--shell", "/bin/bash"], path)
        assert result.exit_code == 1
        assert "not running" in result.output.lower()

    async def test_ready_exit_lifecycle(self, server):
        """Server sends ready → stdout → exit; CLI exits with correct code."""
        path, _ = server
        mock_box = MagicMock()
        ex = _execution(stdout_chunks=["$ "], exit_code=0)
        rt = _mock_runtime(boxes=[_box()], box=mock_box, execution=ex)
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke_shell(["shell"], path)
        assert result.exit_code == 0

    async def test_shell_nonzero_exit_propagated(self, server):
        path, _ = server
        mock_box = MagicMock()
        ex = _execution(exit_code=2)
        rt = _mock_runtime(boxes=[_box()], box=mock_box, execution=ex)
        with patch("boxlite.Boxlite") as cls:
            cls.return_value = rt
            result = await _invoke_shell(["shell"], path)
        assert result.exit_code == 2
