"""Integration tests for sandbox CLI commands — require KVM / Hypervisor.framework.

Skipped automatically when /dev/kvm is unavailable (Linux) or boxlite is not
installed. Tests verify `raven sandbox list/ls/exec/shell` end-to-end:

    CLI process  ──unix socket──>  SandboxDebugServer  ──>  real boxlite VM

Each test starts its own SandboxDebugServer over a freshly created VM, runs the
CLI command (in an executor thread for non-shell, or via pty.fork for shell),
and asserts on real output produced inside the VM.
"""

from __future__ import annotations

import asyncio
import os
import pty
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from raven.cli.sandbox_commands import sandbox_app

requires_kvm = pytest.mark.skipif(
    sys.platform == "linux" and not Path("/dev/kvm").exists(),
    reason="/dev/kvm not available",
)

pytestmark = requires_kvm

# Image used by these tests — pulled once per session before any test runs.
_IMAGE = "ubuntu:22.04"

runner = CliRunner(mix_stderr=False)


# ── session setup ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="session", autouse=True)
def pre_pull_image():
    """Pull the OCI image once per session before any test starts.

    Separating the pull from test execution gives a clear signal when a failure
    is a network/setup problem rather than a bug in the code under test. Skips
    the whole module if boxlite is not installed or /dev/kvm is absent.
    """
    if sys.platform == "linux" and not Path("/dev/kvm").exists():
        return  # pytestmark will skip all tests anyway

    try:
        import boxlite
    except ImportError:
        pytest.skip("boxlite not installed")

    from raven.sandbox._runtime import get_boxlite_runtime

    async def _pull() -> None:
        # Pull through raven's runtime so the image cache matches the home
        # dir the real_server fixture and SandboxDebugServer use.
        async with boxlite.SimpleBox(
            image=_IMAGE,
            cpus=1,
            memory_mib=256,
            runtime=get_boxlite_runtime(),
        ):
            pass  # __aenter__ pulls the image; __aexit__ removes the box

    try:
        asyncio.run(_pull())
    except Exception as exc:
        pytest.skip(f"OCI image pull failed for {_IMAGE!r} — likely a network issue, not a code bug.\n  Error: {exc}")


# ── per-test fixtures: short socket dir + real VM + real debug server ───────


@pytest.fixture
def sock_dir():
    """Short-lived directory for the Unix socket.

    pytest's ``tmp_path`` lives under ``/private/var/folders/...`` on macOS,
    which routinely exceeds the AF_UNIX 104-char path limit. ``/tmp`` is short.
    """
    d = tempfile.mkdtemp(prefix="ec_cli_realvm_", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
async def real_server(sock_dir):
    """Boot a real boxlite VM and start a SandboxDebugServer that owns it.

    Yields ``(socket_path, box_id)``. Tears down the server and the VM at the
    end of the test, even on failure.

    The fixture goes through ``get_boxlite_runtime()`` so the VM is created in
    the same home_dir the SandboxDebugServer reads from. Using
    ``boxlite.Boxlite.default()`` here would land the VM in ``~/.boxlite``
    while the server looks under ``<raven_data_dir>/sandbox/boxlite`` —
    they would not see each other.
    """
    import boxlite

    from raven.sandbox._runtime import get_boxlite_runtime
    from raven.sandbox.debug_server import SandboxDebugServer

    runtime = get_boxlite_runtime()
    box = await runtime.create(boxlite.BoxOptions(image=_IMAGE, cpus=1, memory_mib=512))
    # `create` leaves the VM in `configured` state; we need it `running` before
    # the CLI's `list` will show it as running and exec/shell can attach.
    await box.start()

    sock_path = sock_dir / "debug.sock"
    server = SandboxDebugServer(sock_path, {box.id})
    await server.start()

    try:
        yield sock_path, box.id
    finally:
        try:
            await server.stop()
        except Exception:
            pass
        try:
            await box.stop()
        except Exception:
            pass
        # boxlite 0.8.2 Box has no .remove() — must go through the runtime.
        # Without this, every CI run would leak the VM (silently, because the
        # AttributeError was swallowed by `except Exception: pass`).
        try:
            await runtime.remove(box.id)
        except Exception:
            pass


# ── helpers ───────────────────────────────────────────────────────────────────


async def _invoke(args, socket_path):
    """Run a CLI command in an executor thread so the test loop can keep
    servicing the SandboxDebugServer concurrently.
    """

    def _run():
        with patch(
            "raven.cli.sandbox_commands._get_socket_path",
            return_value=socket_path,
        ):
            return runner.invoke(sandbox_app, args)

    return await asyncio.get_running_loop().run_in_executor(None, _run)


# ── list / ls ─────────────────────────────────────────────────────────────────


class TestListLsRealVM:
    async def test_list_shows_running_vm(self, real_server):
        path, box_id = real_server
        result = await _invoke(["list"], path)
        assert result.exit_code == 0, result.stderr
        assert box_id[:8] in result.output
        assert "running" in result.output

    async def test_list_includes_image(self, real_server):
        path, _ = real_server
        result = await _invoke(["list"], path)
        assert result.exit_code == 0
        # Image name shows up in the table (possibly truncated by Rich rendering).
        assert "ubuntu" in result.output

    async def test_ls_alias_returns_same_vm(self, real_server):
        path, box_id = real_server
        list_result = await _invoke(["list"], path)
        ls_result = await _invoke(["ls"], path)
        assert list_result.exit_code == 0
        assert ls_result.exit_code == 0
        assert box_id[:8] in ls_result.output

    async def test_list_marks_owned_vm(self, real_server):
        # Server owns the VM (we put its id in owned_ids), so the table should
        # render the owned-marker `*` in the first column.
        path, _ = real_server
        result = await _invoke(["list"], path)
        assert "*" in result.output


# ── exec ──────────────────────────────────────────────────────────────────────


class TestExecRealVM:
    async def test_exec_echo(self, real_server):
        path, _ = real_server
        result = await _invoke(["exec", "echo", "hello-from-vm"], path)
        assert result.exit_code == 0, result.stderr
        assert "hello-from-vm" in result.output

    async def test_exec_nonzero_exit_propagated(self, real_server):
        path, _ = real_server
        # Use sh -c so we control the exit code exactly.
        result = await _invoke(["exec", "sh", "-c", "exit 7"], path)
        assert result.exit_code == 7

    async def test_exec_arithmetic_via_shell(self, real_server):
        path, _ = real_server
        result = await _invoke(["exec", "sh", "-c", "echo $((6*7))"], path)
        assert result.exit_code == 0
        assert "42" in result.output

    async def test_exec_with_explicit_vm_ref(self, real_server):
        path, box_id = real_server
        result = await _invoke(
            ["exec", "--vm", box_id, "echo", "via-ref"],
            path,
        )
        assert result.exit_code == 0
        assert "via-ref" in result.output


# ── shell ─────────────────────────────────────────────────────────────────────


# Helper to launch the real CLI under a PTY. We use subprocess with the slave
# end as stdin/stdout/stderr instead of pty.fork() — pytest-asyncio runs the
# test loop on the main thread but our `_invoke` helper offloads to a thread
# pool, which makes the process multi-threaded and forkpty() unsafe (deadlock).
def _spawn_shell_under_pty(socket_path: Path) -> tuple[subprocess.Popen, int]:
    """Spawn `raven sandbox shell` in a subprocess whose stdio is a PTY.

    The subprocess inherits the current Python interpreter and runs the CLI
    with ``--socket-path``-equivalent monkey patching done via a wrapper
    script: easier than passing the path as an argument since the CLI does
    not expose one. We use an env var instead.
    """
    master_fd, slave_fd = pty.openpty()

    # Tiny inline runner that patches the socket-path resolver and invokes the
    # shell command. Equivalent to what _invoke does but in a fresh process so
    # forkpty thread-safety issues do not apply.
    runner_src = (
        "import os, sys\n"
        "from unittest.mock import patch\n"
        "from pathlib import Path\n"
        "import raven.cli.sandbox_commands as sc\n"
        "sock = Path(os.environ['EC_TEST_SOCKET'])\n"
        "with patch.object(sc, '_get_socket_path', return_value=sock):\n"
        "    try:\n"
        "        sc.sandbox_shell(vm=None, shell_path='/bin/sh')\n"
        "    except SystemExit:\n"
        "        pass\n"
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", runner_src],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env={**os.environ, "EC_TEST_SOCKET": str(socket_path)},
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave_fd)  # parent only needs the master end
    return proc, master_fd


class TestShellRealVM:
    """`shell` puts the local terminal into raw mode and forwards stdin/stdout
    over a Unix socket to a PTY-backed shell inside the VM. We can't drive that
    through CliRunner — so we spawn the CLI as a subprocess attached to a real
    PTY and drive it the way a human terminal would.
    """

    async def test_shell_command_echo_and_output(self, real_server):
        sock_path, _ = real_server
        marker = b"SHELL-MARKER-9376"

        proc, master_fd = _spawn_shell_under_pty(sock_path)

        loop = asyncio.get_running_loop()
        output = bytearray()

        def _on_master():
            try:
                d = os.read(master_fd, 4096)
                if d:
                    output.extend(d)
            except OSError:
                pass

        loop.add_reader(master_fd, _on_master)
        try:
            # Wait for the in-VM shell prompt (sh prints `# ` for root, `$ ` for non-root).
            deadline = time.time() + 30
            while b"# " not in bytes(output) and b"$ " not in bytes(output) and time.time() < deadline:
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.3)  # let the prompt fully arrive
            assert b"# " in bytes(output) or b"$ " in bytes(output), (
                f"shell prompt never arrived; got: {bytes(output)!r}"
            )

            # Type a command. With a working stdin path, the marker should
            # appear at least twice in the output stream:
            #   1. the cooked-mode kernel echo of our typed line, and
            #   2. the result of `echo MARKER` printed by the shell.
            os.write(master_fd, b"echo " + marker + b"\n")
            deadline = time.time() + 15
            while bytes(output).count(marker) < 2 and time.time() < deadline:
                await asyncio.sleep(0.1)

            os.write(master_fd, b"exit\n")
            await asyncio.sleep(1.0)
        finally:
            loop.remove_reader(master_fd)
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            try:
                os.close(master_fd)
            except OSError:
                pass

        full = bytes(output)
        # Echo of the typed command proves stdin reached the in-VM PTY,
        # and the program output proves the shell processed it.
        assert full.count(marker) >= 2, (
            f"expected marker {marker!r} to appear at least twice "
            f"(typed-echo + program-output); got count={full.count(marker)} "
            f"in output={full!r}"
        )

    async def test_shell_unknown_vm_ref_errors(self, real_server):
        # No PTY needed for the error-path: the CLI exits before raw-mode setup.
        path, _ = real_server
        result = await _invoke(["shell", "--vm", "no-such-vm-id"], path)
        assert result.exit_code == 1
        assert "no vm found" in result.output.lower()
