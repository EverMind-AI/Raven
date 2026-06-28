"""Integration tests for BoxliteExecutor — require KVM / Hypervisor.framework.

Skipped automatically when /dev/kvm is unavailable (Linux) or on non-Apple-Silicon
macOS. Tests verify real VM execution: echo, timeout, cwd translation, volume mounts,
and MCP stdio roundtrip.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

requires_kvm = pytest.mark.skipif(
    sys.platform == "linux" and not Path("/dev/kvm").exists(),
    reason="/dev/kvm not available",
)

pytestmark = requires_kvm

# Images used by this test module — must be pulled before any test runs.
_IMAGES = [
    "ubuntu:22.04",  # base image for all executor tests
    "node:20-slim",  # required for MCP stdio roundtrip test
]


@pytest.fixture(scope="session", autouse=True)
def pre_pull_images():
    """Pull all OCI images once per session before any test starts.

    Separating the pull from test execution gives a clear signal when a failure
    is a network/setup problem rather than a bug in the code under test.
    Skips the whole module if boxlite is not installed or /dev/kvm is absent.
    """
    if sys.platform == "linux" and not Path("/dev/kvm").exists():
        return  # pytestmark will skip all tests anyway

    try:
        import boxlite
    except ImportError:
        return  # boxlite not installed; tests will fail at VM creation

    async def _pull(image: str) -> None:
        """Start a minimal throw-away box to force image pull, then stop it."""
        async with boxlite.SimpleBox(image=image, cpus=1, memory_mib=256):
            pass  # __aenter__ pulls the image; __aexit__ removes the box

    for image in _IMAGES:
        try:
            asyncio.run(_pull(image))
        except Exception as exc:
            pytest.skip(
                f"OCI image pull failed for {image!r} — likely a network issue, "
                f"not a code bug.\n"
                f"  Fix: check connectivity or pre-pull manually: "
                f"boxlite pull {image}\n"
                f"  Error: {exc}"
            )


@pytest.fixture
async def executor(tmp_path):
    from raven.sandbox.boxlite_executor import BoxliteExecutor
    async with BoxliteExecutor(
        image="ubuntu:22.04",
        workspace=tmp_path,
        cpus=1,
        memory_mib=512,
    ) as e:
        yield e


class TestBoxliteExecutorIntegration:
    async def test_exec_echo(self, executor):
        result = await executor.exec("echo hello")
        assert result.stdout.strip() == "hello"
        assert result.exit_code == 0

    async def test_exec_timeout(self, executor):
        result = await executor.exec("sleep 10", timeout=1)
        assert result.exit_code == -1
        assert "timed out" in result.stderr.lower()

    async def test_exec_cwd(self, executor, tmp_path):
        subdir = tmp_path / "myproject"
        subdir.mkdir()
        result = await executor.exec("pwd", cwd=str(subdir))
        assert "/workspace/myproject" in result.stdout

    async def test_volume_mount_file_visible_in_vm(self, executor, tmp_path):
        host_file = tmp_path / "hello.txt"
        host_file.write_text("from host\n")
        result = await executor.exec("cat /workspace/hello.txt")
        assert "from host" in result.stdout

    async def test_lifecycle_context_manager(self, tmp_path):
        from raven.sandbox.boxlite_executor import BoxliteExecutor
        async with BoxliteExecutor(
            image="ubuntu:22.04",
            workspace=tmp_path,
            cpus=1,
            memory_mib=512,
        ) as e:
            result = await e.exec("echo lifecycle")
        assert result.stdout.strip() == "lifecycle"


@pytest.fixture
async def node_executor(tmp_path):
    """Executor using a Node.js image — required for npx-based MCP server tests."""
    from raven.sandbox.boxlite_executor import BoxliteExecutor
    async with BoxliteExecutor(
        image="node:20-slim",
        workspace=tmp_path,
        cpus=1,
        memory_mib=1024,  # npm needs more memory than basic ubuntu tests
        create_timeout=600,  # first-run image pull can be slow
    ) as e:
        yield e


class TestBoxliteStdioMCPRoundtrip:
    async def test_npx_mcp_server_everything(self, node_executor):
        """MCP stdio server starts inside VM, ClientSession initialises, list_tools works."""
        pytest.importorskip("mcp")
        from mcp import ClientSession

        # Pre-install the package so start_process launches the binary immediately
        # (without npm download) — avoids stdin/stdout timing issues during npx download.
        result = await node_executor.exec(
            "npm install -g @modelcontextprotocol/server-everything",
            timeout=120,
        )
        assert result.exit_code == 0, f"npm install failed:\n{result.stdout}\n{result.stderr}"

        read, write = await node_executor.start_process("mcp-server-everything", [])
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=30)
            tools = await asyncio.wait_for(session.list_tools(), timeout=15)
        assert len(tools.tools) > 0
