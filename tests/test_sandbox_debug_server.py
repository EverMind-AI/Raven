"""Unit tests for SandboxDebugServer — all run without boxlite or KVM."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from raven.sandbox.debug_server import (
    SandboxDebugServer,
    SandboxDebugServerError,
    _send,
)


@pytest.fixture
def sock_dir():
    """Yield a short-path temp dir suitable for Unix domain sockets.

    macOS limits Unix socket paths to 104 bytes. pytest's tmp_path often
    exceeds this, so we use a short path under /tmp instead.
    """
    d = tempfile.mkdtemp(prefix="ec_dbg_", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _client(path: Path, request: dict) -> dict:
    """Connect, send one request, receive one response, close."""
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write((json.dumps(request) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return json.loads(line.decode())


# ---------------------------------------------------------------------------
# resolve_socket_path
# ---------------------------------------------------------------------------

class TestResolveSocketPath:
    def test_relative_resolved_against_data_dir(self, tmp_path: Path) -> None:
        result = SandboxDebugServer.resolve_socket_path("sandbox/debug.sock", tmp_path)
        assert result == tmp_path / "sandbox" / "debug.sock"

    def test_absolute_used_as_is(self, tmp_path: Path) -> None:
        abs_path = tmp_path / "custom" / "my.sock"
        result = SandboxDebugServer.resolve_socket_path(str(abs_path), tmp_path / "data")
        assert result == abs_path

    def test_parent_dirs_created(self, tmp_path: Path) -> None:
        SandboxDebugServer.resolve_socket_path("a/b/c/debug.sock", tmp_path)
        assert (tmp_path / "a" / "b" / "c").is_dir()


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

class TestServerLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_socket_file(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, set())
        await server.start()
        try:
            assert path.exists()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_socket_permissions_are_0600(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, set())
        await server.start()
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            assert mode == 0o600
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_stop_removes_socket_file(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, set())
        await server.start()
        await server.stop()
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_stale_socket_removed_on_start(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        path.touch()  # simulate stale file
        server = SandboxDebugServer(path, set())
        await server.start()
        try:
            assert path.exists()  # new socket created
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, set())
        await server.start()
        await server.stop()
        await server.stop()  # should not raise


# ---------------------------------------------------------------------------
# Framing error handling
# ---------------------------------------------------------------------------

class TestFraming:
    @pytest.mark.asyncio
    async def test_invalid_json_returns_error(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, set())
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(b"not json\n")
            await writer.drain()
            line = await reader.readline()
            writer.close()
            await writer.wait_closed()
            msg = json.loads(line)
            assert msg["type"] == "error"
            assert "Invalid JSON" in msg["message"]
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_unknown_cmd_returns_error(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, set())
        await server.start()
        try:
            resp = await _client(path, {"cmd": "ping"})
            assert resp["type"] == "error"
            assert "ping" in resp["message"]
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_missing_cmd_returns_error(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, set())
        await server.start()
        try:
            resp = await _client(path, {"foo": "bar"})
            assert resp["type"] == "error"
            assert "None" in resp["message"]
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_oversized_line_returns_error(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, set(), max_message_bytes=32)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(b"x" * 64 + b"\n")
            await writer.drain()
            line = await reader.readline()
            writer.close()
            await writer.wait_closed()
            msg = json.loads(line)
            assert msg["type"] == "error"
            assert "too large" in msg["message"].lower()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_concurrent_connections_handled(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, set())
        await server.start()
        try:
            results = await asyncio.gather(
                _client(path, {"cmd": "ping"}),
                _client(path, {"cmd": "ping"}),
            )
            assert all(r["type"] == "error" for r in results)
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# list handler
# ---------------------------------------------------------------------------

def _make_box_info(
    id: str = "box1",
    name: str | None = None,
    status: str = "running",
    image: str = "ubuntu:22.04",
    cpus: int = 2,
    memory_mib: int = 2048,
) -> MagicMock:
    info = MagicMock()
    info.id = id
    info.name = name
    info.state.status = status
    info.image = image
    info.cpus = cpus
    info.memory_mib = memory_mib
    info.created_at = None
    return info


class TestListHandler:
    @pytest.mark.asyncio
    async def test_list_owned_annotation(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        owned = {"owned-box"}
        server = SandboxDebugServer(path, owned)
        await server.start()

        box_owned = _make_box_info(id="owned-box")
        box_other = _make_box_info(id="other-box")

        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[box_owned, box_other])

        with patch("boxlite.Boxlite") as mock_cls:
            mock_cls.return_value = mock_runtime
            resp = await _client(path, {"cmd": "list"})

        await server.stop()

        assert resp["type"] == "vm_list"
        vms = {v["id"]: v for v in resp["vms"]}
        assert vms["owned-box"]["owned"] is True
        assert vms["other-box"]["owned"] is False

    @pytest.mark.asyncio
    async def test_list_empty_runtime(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, set())
        await server.start()

        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[])

        with patch("boxlite.Boxlite") as mock_cls:
            mock_cls.return_value = mock_runtime
            resp = await _client(path, {"cmd": "list"})

        await server.stop()

        assert resp["type"] == "vm_list"
        assert resp["vms"] == []

    @pytest.mark.asyncio
    async def test_list_boxlite_not_installed(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, set())
        await server.start()

        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "boxlite":
                raise ImportError("No module named 'boxlite'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            resp = await _client(path, {"cmd": "list"})

        await server.stop()

        assert resp["type"] == "error"
        assert "boxlite" in resp["message"].lower()

    @pytest.mark.asyncio
    async def test_list_runtime_error_returns_error(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, set())
        await server.start()

        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(side_effect=RuntimeError("db locked"))

        with patch("boxlite.Boxlite") as mock_cls:
            mock_cls.return_value = mock_runtime
            resp = await _client(path, {"cmd": "list"})

        await server.stop()

        assert resp["type"] == "error"
        assert "db locked" in resp["message"]

    @pytest.mark.asyncio
    async def test_list_vm_fields_populated(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, {"b1"})
        await server.start()

        box = _make_box_info(id="b1", name="my-vm", status="running", image="alpine:latest", cpus=4, memory_mib=1024)

        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[box])

        with patch("boxlite.Boxlite") as mock_cls:
            mock_cls.return_value = mock_runtime
            resp = await _client(path, {"cmd": "list"})

        await server.stop()

        assert resp["type"] == "vm_list"
        vm = resp["vms"][0]
        assert vm["id"] == "b1"
        assert vm["name"] == "my-vm"
        assert vm["status"] == "running"
        assert vm["image"] == "alpine:latest"
        assert vm["cpus"] == 4
        assert vm["memory_mib"] == 1024



# ---------------------------------------------------------------------------
# VM resolution (shared by exec and shell)
# ---------------------------------------------------------------------------

class TestVmResolution:
    """Tests for _resolve_vm via the exec command."""

    def _setup(self, sock_dir: Path, owned: set[str]) -> tuple:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, owned)
        return path, server

    def _box(self, id: str, name: str | None = None, status: str = "running") -> MagicMock:
        b = MagicMock()
        b.id = id
        b.name = name
        b.state.status = status
        return b

    async def _exec_req(self, path: Path, vm_ref, boxes: list) -> dict:
        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=boxes)
        with patch("boxlite.Boxlite") as mock_cls:
            mock_cls.return_value = mock_runtime
            return await _client(path, {"cmd": "exec", "vm_ref": vm_ref, "program": "ls"})

    @pytest.mark.asyncio
    async def test_auto_select_no_running_vms(self, sock_dir: Path) -> None:
        path, server = self._setup(sock_dir, {"b1"})
        await server.start()
        resp = await self._exec_req(path, None, [self._box("b1", status="stopped")])
        await server.stop()
        assert resp["type"] == "error"
        assert "No running VMs" in resp["message"]

    @pytest.mark.asyncio
    async def test_auto_select_multiple_running_vms(self, sock_dir: Path) -> None:
        path, server = self._setup(sock_dir, {"b1", "b2"})
        await server.start()
        resp = await self._exec_req(path, None, [self._box("b1"), self._box("b2")])
        await server.stop()
        assert resp["type"] == "error"
        assert "Multiple running VMs" in resp["message"]

    @pytest.mark.asyncio
    async def test_vm_ref_not_found(self, sock_dir: Path) -> None:
        path, server = self._setup(sock_dir, {"b1"})
        await server.start()
        resp = await self._exec_req(path, "nope", [self._box("b1")])
        await server.stop()
        assert resp["type"] == "error"
        assert "No VM found" in resp["message"]

    @pytest.mark.asyncio
    async def test_vm_not_owned(self, sock_dir: Path) -> None:
        path, server = self._setup(sock_dir, set())
        await server.start()
        resp = await self._exec_req(path, "b1", [self._box("b1")])
        await server.stop()
        assert resp["type"] == "error"
        assert "not owned" in resp["message"]

    @pytest.mark.asyncio
    async def test_vm_not_running(self, sock_dir: Path) -> None:
        path, server = self._setup(sock_dir, {"b1"})
        await server.start()
        resp = await self._exec_req(path, "b1", [self._box("b1", status="stopped")])
        await server.stop()
        assert resp["type"] == "error"
        assert "not running" in resp["message"]

    @pytest.mark.asyncio
    async def test_ambiguous_name_match(self, sock_dir: Path) -> None:
        path, server = self._setup(sock_dir, {"b1", "b2"})
        await server.start()
        boxes = [self._box("b1", name="dup"), self._box("b2", name="dup")]
        resp = await self._exec_req(path, "dup", boxes)
        await server.stop()
        assert resp["type"] == "error"
        assert "Ambiguous" in resp["message"]

    @pytest.mark.asyncio
    async def test_program_empty_returns_error(self, sock_dir: Path) -> None:
        path, server = self._setup(sock_dir, {"b1"})
        await server.start()

        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[self._box("b1")])
        with patch("boxlite.Boxlite") as mock_cls:
            mock_cls.return_value = mock_runtime
            resp = await _client(path, {"cmd": "exec", "vm_ref": None, "program": ""})
        await server.stop()
        assert resp["type"] == "error"
        assert "program" in resp["message"].lower()

    @pytest.mark.asyncio
    async def test_attach_box_boxlite_not_installed(self, sock_dir: Path) -> None:
        """M3: exec/shell must give the same 'boxlite is not installed' error
        as list, instead of a noisier 'Failed to list VMs: ...' wrapper."""
        path, server = self._setup(sock_dir, {"b1"})
        await server.start()

        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "boxlite":
                raise ImportError("No module named 'boxlite'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            resp = await _client(path, {"cmd": "exec", "vm_ref": None, "program": "ls"})

        await server.stop()
        assert resp["type"] == "error"
        # Must match the list-handler message verbatim — no "Failed to list VMs" wrapper.
        assert resp["message"] == "boxlite is not installed."


# ---------------------------------------------------------------------------
# exec handler streaming
# ---------------------------------------------------------------------------

class TestExecHandler:
    @pytest.mark.asyncio
    async def test_exec_streams_stdout_and_sends_exit(self, sock_dir: Path) -> None:
        import base64

        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, {"b1"})
        await server.start()

        box_info = MagicMock()
        box_info.id = "b1"
        box_info.name = None
        box_info.state.status = "running"

        mock_execution = MagicMock()
        mock_execution.stdout = MagicMock(return_value=_async_iter(["hello\n"]))
        mock_execution.stderr = MagicMock(return_value=_async_iter([]))
        mock_execution.wait = AsyncMock(return_value=MagicMock(exit_code=0))
        mock_execution.kill = AsyncMock()

        mock_box = MagicMock()
        mock_box.exec = AsyncMock(return_value=mock_execution)

        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[box_info])
        mock_runtime.get = AsyncMock(return_value=mock_box)

        with patch("boxlite.Boxlite") as mock_cls:
            mock_cls.return_value = mock_runtime
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write((json.dumps({"cmd": "exec", "vm_ref": None, "program": "echo", "args": ["hello"]}) + "\n").encode())
            await writer.drain()

            messages = []
            for _ in range(3):  # stdout + exit (at most)
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if not line:
                    break
                messages.append(json.loads(line))

            writer.close()
            await writer.wait_closed()

        await server.stop()

        types = [m["type"] for m in messages]
        assert "stdout" in types
        assert "exit" in types
        stdout_msgs = [m for m in messages if m["type"] == "stdout"]
        decoded = base64.b64decode(stdout_msgs[0]["data"]).decode()
        assert "hello" in decoded
        exit_msg = next(m for m in messages if m["type"] == "exit")
        assert exit_msg["code"] == 0


# ---------------------------------------------------------------------------
# shell handler
# ---------------------------------------------------------------------------

class TestShellHandler:
    @pytest.mark.asyncio
    async def test_shell_empty_path_returns_error(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, {"b1"})
        await server.start()

        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[])
        with patch("boxlite.Boxlite") as mock_cls:
            mock_cls.return_value = mock_runtime
            resp = await _client(path, {"cmd": "shell", "vm_ref": None, "shell": ""})

        await server.stop()
        assert resp["type"] == "error"
        assert "shell" in resp["message"].lower()

    @pytest.mark.asyncio
    async def test_shell_sends_ready_then_exit(self, sock_dir: Path) -> None:
        import base64

        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, {"b1"})
        await server.start()

        box_info = MagicMock()
        box_info.id = "b1"
        box_info.name = None
        box_info.state.status = "running"

        mock_stdin = MagicMock()
        mock_stdin.send_input = AsyncMock()

        mock_execution = MagicMock()
        mock_execution.stdout = MagicMock(return_value=_async_iter(["$ "]))
        mock_execution.stdin = MagicMock(return_value=mock_stdin)
        mock_execution.wait = AsyncMock(return_value=MagicMock(exit_code=0))
        mock_execution.kill = AsyncMock()
        mock_execution.resize_tty = AsyncMock()

        mock_box = MagicMock()
        mock_box.exec = AsyncMock(return_value=mock_execution)

        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[box_info])
        mock_runtime.get = AsyncMock(return_value=mock_box)

        with patch("boxlite.Boxlite") as mock_cls:
            mock_cls.return_value = mock_runtime

            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write((json.dumps({
                "cmd": "shell", "vm_ref": None, "shell": "/bin/sh"
            }) + "\n").encode())
            await writer.drain()

            messages = []
            for _ in range(3):
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if not line:
                    break
                messages.append(json.loads(line))

            writer.close()
            await writer.wait_closed()

        await server.stop()

        types = [m["type"] for m in messages]
        assert "ready" in types
        assert "exit" in types or "stdout" in types

    @pytest.mark.asyncio
    async def test_shell_resize_forwarded(self, sock_dir: Path) -> None:
        import base64

        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, {"b1"})
        await server.start()

        box_info = MagicMock()
        box_info.id = "b1"
        box_info.name = None
        box_info.state.status = "running"

        mock_stdin_obj = MagicMock()
        mock_stdin_obj.send_input = AsyncMock()

        resize_received = asyncio.Event()

        async def _slow_wait():
            await resize_received.wait()
            return MagicMock(exit_code=0)

        mock_execution = MagicMock()
        mock_execution.stdout = MagicMock(return_value=_async_iter([]))
        mock_execution.stdin = MagicMock(return_value=mock_stdin_obj)
        mock_execution.wait = _slow_wait
        mock_execution.kill = AsyncMock()

        async def _resize_tty(rows, cols):
            resize_received.set()

        mock_execution.resize_tty = AsyncMock(side_effect=_resize_tty)

        mock_box = MagicMock()
        mock_box.exec = AsyncMock(return_value=mock_execution)

        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[box_info])
        mock_runtime.get = AsyncMock(return_value=mock_box)

        with patch("boxlite.Boxlite") as mock_cls:
            mock_cls.return_value = mock_runtime

            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write((json.dumps({
                "cmd": "shell", "vm_ref": None, "shell": "/bin/sh"
            }) + "\n").encode())
            await writer.drain()

            # Read ready
            await asyncio.wait_for(reader.readline(), timeout=2.0)

            # Send resize — server will unblock _slow_wait once resize_tty is called
            writer.write((json.dumps({"cmd": "resize", "rows": 40, "cols": 120}) + "\n").encode())
            await writer.drain()

            # Read exit message
            await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.close()
            await writer.wait_closed()

        await server.stop()
        mock_execution.resize_tty.assert_called_with(rows=40, cols=120)

    @pytest.mark.asyncio
    async def test_shell_resize_ignores_zero_values(self, sock_dir: Path) -> None:
        """resize with rows=0 or cols=0 must be silently ignored."""
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, {"b1"})
        await server.start()

        box_info = MagicMock()
        box_info.id = "b1"
        box_info.name = None
        box_info.state.status = "running"

        mock_stdin_obj = MagicMock()
        mock_stdin_obj.send_input = AsyncMock()

        mock_execution = MagicMock()
        mock_execution.stdout = MagicMock(return_value=_async_iter([]))
        mock_execution.stdin = MagicMock(return_value=mock_stdin_obj)
        mock_execution.wait = AsyncMock(return_value=MagicMock(exit_code=0))
        mock_execution.kill = AsyncMock()
        mock_execution.resize_tty = AsyncMock()

        mock_box = MagicMock()
        mock_box.exec = AsyncMock(return_value=mock_execution)

        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[box_info])
        mock_runtime.get = AsyncMock(return_value=mock_box)

        with patch("boxlite.Boxlite") as mock_cls:
            mock_cls.return_value = mock_runtime

            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write((json.dumps({
                "cmd": "shell", "vm_ref": None, "shell": "/bin/sh"
            }) + "\n").encode())
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)  # ready

            writer.write((json.dumps({"cmd": "resize", "rows": 0, "cols": 80}) + "\n").encode())
            await writer.drain()
            await asyncio.sleep(0.1)
            writer.close()
            await writer.wait_closed()

        await server.stop()
        mock_execution.resize_tty.assert_not_called()


# ---------------------------------------------------------------------------
# Connection lifecycle: P1 regression tests
# ---------------------------------------------------------------------------

def _async_iter(items):
    """Return an async iterator over a list of items."""
    async def _gen():
        for item in items:
            yield item
    return _gen()


def _slow_async_iter(chunks, delay: float = 0.05):
    """Yield chunks with a short delay between them — used to simulate VM stdout
    that arrives in bursts after a process has exited."""
    async def _gen():
        for c in chunks:
            await asyncio.sleep(delay)
            yield c
    return _gen()


class TestStartLifecycle:
    """Covers P1.1 server-side: start() must not clobber a live socket."""

    @pytest.mark.asyncio
    async def test_start_refuses_when_existing_socket_is_alive(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        srv1 = SandboxDebugServer(path, set())
        await srv1.start()
        try:
            srv2 = SandboxDebugServer(path, set())
            with pytest.raises(SandboxDebugServerError) as exc_info:
                await srv2.start()
            assert "already in use" in str(exc_info.value)
            # srv1 must still be operational after the rejected start.
            resp = await _client(path, {"cmd": "bogus"})
            assert resp["type"] == "error"
        finally:
            await srv1.stop()

    @pytest.mark.asyncio
    async def test_start_unlinks_stale_socket(self, sock_dir: Path) -> None:
        # A regular file with no listener — must be treated as stale.
        path = sock_dir / "debug.sock"
        path.touch()
        srv = SandboxDebugServer(path, set())
        await srv.start()
        try:
            assert path.exists()
            resp = await _client(path, {"cmd": "bogus"})
            assert resp["type"] == "error"
        finally:
            await srv.stop()


class TestSingleClientGuard:
    """Covers P1.1 per-connection: only one client may be attached at a time."""

    @pytest.mark.asyncio
    async def test_second_client_during_shell_is_rejected(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        owned = {"b1"}
        server = SandboxDebugServer(path, owned)
        await server.start()

        box_info = MagicMock()
        box_info.id = "b1"
        box_info.name = None
        box_info.state.status = "running"

        # A shell whose wait() blocks forever — keeps the first client attached.
        wait_started = asyncio.Event()
        wait_release = asyncio.Event()

        async def _blocking_wait():
            wait_started.set()
            await wait_release.wait()
            return MagicMock(exit_code=0)

        mock_execution = MagicMock()
        mock_execution.stdout = MagicMock(return_value=_async_iter([]))
        mock_execution.stdin = MagicMock(return_value=MagicMock(send_input=AsyncMock()))
        mock_execution.wait = _blocking_wait
        mock_execution.kill = AsyncMock()
        mock_execution.resize_tty = AsyncMock()

        mock_box = MagicMock()
        mock_box.exec = AsyncMock(return_value=mock_execution)
        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[box_info])
        mock_runtime.get = AsyncMock(return_value=mock_box)

        try:
            with patch("boxlite.Boxlite") as cls:
                cls.return_value = mock_runtime
                # Client A — opens shell, reads ready, then sits idle.
                rA, wA = await asyncio.open_unix_connection(str(path))
                wA.write((json.dumps({"cmd": "shell", "vm_ref": None, "shell": "/bin/sh"}) + "\n").encode())
                await wA.drain()
                first = json.loads(await asyncio.wait_for(rA.readline(), timeout=2.0))
                assert first["type"] == "ready"
                await asyncio.wait_for(wait_started.wait(), timeout=2.0)

                # Client B — must be refused immediately with the single-client error.
                respB = await _client(path, {"cmd": "list"})
                assert respB["type"] == "error"
                assert "active client" in respB["message"].lower()

                # Now release client A so the server can shut down cleanly.
                wait_release.set()
                wA.close()
                try:
                    await wA.wait_closed()
                except Exception:
                    pass
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_new_client_accepted_after_previous_disconnects(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, set())
        await server.start()
        try:
            # Two sequential single-client connections both succeed.
            r1 = await _client(path, {"cmd": "bogus"})
            assert r1["type"] == "error"
            r2 = await _client(path, {"cmd": "bogus"})
            assert r2["type"] == "error"
            assert "active client" not in r2["message"].lower()
        finally:
            await server.stop()


class TestExecDisconnectKillsExecution:
    """Covers P1.3: exec must not block server when client disconnects mid-stream."""

    @pytest.mark.asyncio
    async def test_long_running_exec_disconnect_triggers_kill(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, {"b1"})
        await server.start()

        box_info = MagicMock()
        box_info.id = "b1"
        box_info.name = None
        box_info.state.status = "running"

        wait_release = asyncio.Event()

        async def _blocking_wait():
            await wait_release.wait()
            return MagicMock(exit_code=0)

        kill_called = asyncio.Event()

        async def _kill():
            kill_called.set()
            wait_release.set()  # let wait() unblock so the task can be cancelled cleanly

        mock_execution = MagicMock()
        mock_execution.stdout = MagicMock(return_value=_async_iter(["partial output\n"]))
        mock_execution.stderr = MagicMock(return_value=_async_iter([]))
        mock_execution.wait = _blocking_wait
        mock_execution.kill = AsyncMock(side_effect=_kill)

        mock_box = MagicMock()
        mock_box.exec = AsyncMock(return_value=mock_execution)
        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[box_info])
        mock_runtime.get = AsyncMock(return_value=mock_box)

        try:
            with patch("boxlite.Boxlite") as cls:
                cls.return_value = mock_runtime
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write((json.dumps({
                    "cmd": "exec", "vm_ref": None,
                    "program": "sleep", "args": ["999"],
                }) + "\n").encode())
                await writer.drain()

                # Wait until we've seen the first stdout chunk so the handler is
                # past initial setup and in the disconnect-watch state.
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                assert json.loads(line)["type"] == "stdout"

                # Now close the client — should trigger execution.kill().
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

                await asyncio.wait_for(kill_called.wait(), timeout=2.0)
        finally:
            await server.stop()


class TestExecHandlesFutureWait:
    """Regression: real boxlite Execution.wait() returns an asyncio.Future, not
    a coroutine. asyncio.create_task() only accepts coroutines, so a naive
    `create_task(execution.wait())` raises TypeError on real VMs even though
    AsyncMock-based unit tests pass. This test mimics the real shape."""

    @pytest.mark.asyncio
    async def test_exec_wait_returning_future_is_handled(self, sock_dir: Path) -> None:
        import base64

        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, {"b1"})
        await server.start()

        box_info = MagicMock()
        box_info.id = "b1"
        box_info.name = None
        box_info.state.status = "running"

        loop = asyncio.get_event_loop()
        wait_future: asyncio.Future = loop.create_future()
        wait_future.set_result(MagicMock(exit_code=0))

        # Use a plain MagicMock (not AsyncMock) so wait() returns the Future
        # itself synchronously — same shape as the Rust-backed boxlite binding.
        mock_execution = MagicMock()
        mock_execution.stdout = MagicMock(return_value=_async_iter(["hi\n"]))
        mock_execution.stderr = MagicMock(return_value=_async_iter([]))
        mock_execution.wait = MagicMock(return_value=wait_future)
        mock_execution.kill = AsyncMock()

        mock_box = MagicMock()
        mock_box.exec = AsyncMock(return_value=mock_execution)
        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[box_info])
        mock_runtime.get = AsyncMock(return_value=mock_box)

        try:
            with patch("boxlite.Boxlite") as cls:
                cls.return_value = mock_runtime
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write((json.dumps({
                    "cmd": "exec", "vm_ref": None, "program": "echo", "args": ["hi"],
                }) + "\n").encode())
                await writer.drain()

                messages = []
                for _ in range(3):
                    line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                    if not line:
                        break
                    messages.append(json.loads(line))

                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        finally:
            await server.stop()

        types = [m["type"] for m in messages]
        assert "stdout" in types
        assert "exit" in types, f"exit message missing — got {types}"
        decoded = base64.b64decode(messages[types.index("stdout")]["data"]).decode()
        assert "hi" in decoded


class TestShellDisconnectKillsExecution:
    """Covers P1.2: idle shell client disconnect must kill the VM-side shell."""

    @pytest.mark.asyncio
    async def test_idle_shell_client_disconnect_triggers_kill(self, sock_dir: Path) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, {"b1"})
        await server.start()

        box_info = MagicMock()
        box_info.id = "b1"
        box_info.name = None
        box_info.state.status = "running"

        wait_release = asyncio.Event()

        async def _blocking_wait():
            await wait_release.wait()
            return MagicMock(exit_code=0)

        kill_called = asyncio.Event()

        async def _kill():
            kill_called.set()
            wait_release.set()

        mock_execution = MagicMock()
        mock_execution.stdout = MagicMock(return_value=_async_iter([]))
        mock_execution.stdin = MagicMock(return_value=MagicMock(send_input=AsyncMock()))
        mock_execution.wait = _blocking_wait
        mock_execution.kill = AsyncMock(side_effect=_kill)
        mock_execution.resize_tty = AsyncMock()

        mock_box = MagicMock()
        mock_box.exec = AsyncMock(return_value=mock_execution)
        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[box_info])
        mock_runtime.get = AsyncMock(return_value=mock_box)

        try:
            with patch("boxlite.Boxlite") as cls:
                cls.return_value = mock_runtime
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write((json.dumps({
                    "cmd": "shell", "vm_ref": None, "shell": "/bin/sh",
                }) + "\n").encode())
                await writer.drain()
                first = json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
                assert first["type"] == "ready"

                # Close the client without ever sending any stdin — simulates the user
                # opening `raven sandbox shell` and walking away by closing the term.
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

                await asyncio.wait_for(kill_called.wait(), timeout=2.0)
        finally:
            await server.stop()


class TestShellDrainsStdoutBeforeExit:
    """Covers P1.4: shell exit must not race ahead of trailing stdout chunks."""

    @pytest.mark.asyncio
    async def test_trailing_stdout_arrives_before_exit_message(self, sock_dir: Path) -> None:
        import base64

        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, {"b1"})
        await server.start()

        box_info = MagicMock()
        box_info.id = "b1"
        box_info.name = None
        box_info.state.status = "running"

        # stdout yields several chunks each with a small delay; wait() returns
        # immediately. Without the drain fix, exit would race ahead of the
        # later chunks.
        chunks = ["line1\n", "line2\n", "line3\n"]
        mock_execution = MagicMock()
        mock_execution.stdout = MagicMock(return_value=_slow_async_iter(chunks, delay=0.05))
        mock_execution.stdin = MagicMock(return_value=MagicMock(send_input=AsyncMock()))
        mock_execution.wait = AsyncMock(return_value=MagicMock(exit_code=0))
        mock_execution.kill = AsyncMock()
        mock_execution.resize_tty = AsyncMock()

        mock_box = MagicMock()
        mock_box.exec = AsyncMock(return_value=mock_execution)
        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[box_info])
        mock_runtime.get = AsyncMock(return_value=mock_box)

        try:
            with patch("boxlite.Boxlite") as cls:
                cls.return_value = mock_runtime
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write((json.dumps({
                    "cmd": "shell", "vm_ref": None, "shell": "/bin/sh",
                }) + "\n").encode())
                await writer.drain()

                messages = []
                # ready, 3 stdout, exit — stop after exit
                while True:
                    line = await asyncio.wait_for(reader.readline(), timeout=3.0)
                    if not line:
                        break
                    msg = json.loads(line)
                    messages.append(msg)
                    if msg["type"] == "exit":
                        break

                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        finally:
            await server.stop()

        types = [m["type"] for m in messages]
        # All three stdout chunks must appear strictly before the exit message.
        exit_idx = types.index("exit")
        stdout_indices = [i for i, t in enumerate(types) if t == "stdout"]
        assert len(stdout_indices) == 3, (
            f"expected 3 stdout chunks before exit, got types={types}"
        )
        assert all(i < exit_idx for i in stdout_indices), (
            f"stdout chunk arrived after exit — drain regressed. types={types}"
        )
        decoded = b"".join(base64.b64decode(messages[i]["data"]) for i in stdout_indices)
        assert decoded == b"line1\nline2\nline3\n"


class TestShellResizeTaskCleanup:
    """Covers the resize-task tracking fix: an in-flight resize_tty that's
    still pending at handler shutdown must be cancelled and awaited rather
    than left as an orphan (which would trip a 'Task exception was never
    retrieved' warning if it later raised, and keep ``execution`` alive past
    the handler).
    """

    @pytest.mark.asyncio
    async def test_pending_resize_is_cancelled_on_shutdown(
        self, sock_dir: Path,
    ) -> None:
        path = sock_dir / "debug.sock"
        server = SandboxDebugServer(path, {"b1"})
        await server.start()

        box_info = MagicMock()
        box_info.id = "b1"
        box_info.name = None
        box_info.state.status = "running"

        # resize_tty hangs forever. Track whether the coroutine sees
        # CancelledError — that's the signal the cleanup fix is doing its job.
        resize_started = asyncio.Event()
        resize_cancelled = asyncio.Event()

        async def _hanging_resize(rows=None, cols=None):
            resize_started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                resize_cancelled.set()
                raise

        # wait() must block until the test has staged the resize and closed
        # the client; otherwise it would set done_event first and trigger
        # cleanup before the resize_task is ever created. Closing the client
        # makes _stdin_task observe EOF and set done_event itself.
        wait_release = asyncio.Event()

        async def _blocking_wait():
            await wait_release.wait()
            return MagicMock(exit_code=0)

        mock_execution = MagicMock()
        mock_execution.stdout = MagicMock(return_value=_async_iter([]))
        mock_execution.stdin = MagicMock(return_value=MagicMock(send_input=AsyncMock()))
        mock_execution.wait = _blocking_wait
        mock_execution.kill = AsyncMock(side_effect=lambda: wait_release.set())
        mock_execution.resize_tty = _hanging_resize

        mock_box = MagicMock()
        mock_box.exec = AsyncMock(return_value=mock_execution)
        mock_runtime = MagicMock()
        mock_runtime.list_info = AsyncMock(return_value=[box_info])
        mock_runtime.get = AsyncMock(return_value=mock_box)

        # Capture asyncio's "Task exception was never retrieved" reports —
        # if the resize task escapes cleanup, it lands here.
        loop = asyncio.get_running_loop()
        unhandled: list[dict] = []
        original_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, ctx: unhandled.append(ctx))

        try:
            with patch("boxlite.Boxlite") as cls:
                cls.return_value = mock_runtime

                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write((json.dumps({
                    "cmd": "shell", "vm_ref": None, "shell": "/bin/sh",
                }) + "\n").encode())
                await writer.drain()
                first = json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
                assert first["type"] == "ready"

                # Send a resize so a hanging resize task lands in resize_tasks.
                writer.write((json.dumps({"cmd": "resize", "rows": 40, "cols": 120}) + "\n").encode())
                await writer.drain()
                await asyncio.wait_for(resize_started.wait(), timeout=2.0)

                # Close the client — handler tears down. The pending resize
                # task must be cancelled (resize_cancelled gets set), and the
                # whole cleanup must finish promptly (no deadlock waiting on
                # the hung resize).
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

                await asyncio.wait_for(resize_cancelled.wait(), timeout=2.0)
        finally:
            # Server.stop() should return quickly even with a previously
            # hanging resize — it would block forever if the cleanup hadn't
            # cancelled the task.
            await asyncio.wait_for(server.stop(), timeout=2.0)
            loop.set_exception_handler(original_handler)

        # No "Task exception was never retrieved" or similar leakage.
        assert unhandled == [], f"unhandled task exceptions during cleanup: {unhandled}"
