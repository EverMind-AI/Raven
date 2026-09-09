"""Unit tests for the sandbox package and sandbox-related ExecTool / AgentLoop behaviour.

All tests run without boxlite installed and without KVM/Hypervisor access.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from raven.sandbox import (
    DirectExecutor,
    ExecResult,
    SandboxConfig,
    SandboxExecutor,
    SandboxInitError,
    build_executor,
)
from raven.sandbox.boxlite_executor import BoxliteExecutor

# ---------------------------------------------------------------------------
# Helpers: mock executors
# ---------------------------------------------------------------------------


class MockExecutor(SandboxExecutor):
    """Sandboxed mock executor (is_sandboxed=True) that records calls."""

    def __init__(self, responses: list[ExecResult] | None = None):
        self.calls: list[dict] = []
        self._responses = responses or [ExecResult(stdout="ok", stderr="", exit_code=0)]
        self._idx = 0

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        self.calls.append({"command": command, "cwd": cwd, "timeout": timeout, "env": env})
        result = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        return result


class DirectMockExecutor(MockExecutor):
    """MockExecutor that reports is_sandboxed=False (host-execution fallback tests)."""

    @property
    def is_sandboxed(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# ExecResult
# ---------------------------------------------------------------------------


class TestExecResultAsText:
    def test_stdout_only(self):
        r = ExecResult(stdout="hello\n", stderr="", exit_code=0)
        text = r.as_text()
        assert "hello" in text
        assert "Exit code: 0" in text

    def test_stderr_included_when_non_empty(self):
        r = ExecResult(stdout="out\n", stderr="warn\n", exit_code=1)
        text = r.as_text()
        assert "STDERR:" in text
        assert "warn" in text
        assert "Exit code: 1" in text

    def test_stderr_whitespace_only_not_shown(self):
        r = ExecResult(stdout="out\n", stderr="   \n", exit_code=0)
        text = r.as_text()
        assert "STDERR:" not in text

    def test_empty_output(self):
        r = ExecResult(stdout="", stderr="", exit_code=0)
        assert r.as_text() == "\nExit code: 0"  # exit-code line is always present

    def test_timed_out_renders_recovery_guidance(self):
        r = ExecResult(stdout="partial", stderr="Timed out after 5s", exit_code=-1, timed_out=True)
        text = r.as_text()
        assert "partial" in text
        assert "time limit" in text
        assert "background:true" in text

    def test_truncation(self):
        long_out = "x" * 20_000
        r = ExecResult(stdout=long_out, stderr="", exit_code=0)
        text = r.as_text(max_chars=100)
        assert "truncated" in text
        assert len(text) < 300  # well under original

    def test_truncation_spills_full_output_to_file(self, tmp_path, monkeypatch):
        """Truncation must not lose evidence: the full output is saved and the
        marker names the file, so the model can grep it instead of re-running."""
        monkeypatch.setenv("HOME", str(tmp_path))
        long_out = "line\n" * 5_000
        r = ExecResult(stdout=long_out, stderr="", exit_code=0)
        text = r.as_text(max_chars=100, spill_prefix="exec")
        assert "full output saved to" in text
        spilled = list((tmp_path / ".raven" / "tool-output").glob("exec-*.log"))
        assert len(spilled) == 1
        assert long_out in spilled[0].read_text()


# ---------------------------------------------------------------------------
# SandboxConfig validators
# ---------------------------------------------------------------------------


class TestSandboxConfigValidators:
    def test_defaults(self):
        c = SandboxConfig()
        assert c.backend == "none"
        assert c.allow_net is True
        assert c.extra_volumes == []

    def test_allow_net_empty_list_raises(self):
        with pytest.raises(ValueError, match="ambiguous"):
            SandboxConfig(allow_net=[])

    def test_allow_net_non_empty_list_ok(self):
        c = SandboxConfig(allow_net=["pypi.org"])
        assert c.allow_net == ["pypi.org"]

    def test_extra_volumes_bad_mode_raises(self):
        with pytest.raises(ValueError, match="ro.*rw"):
            SandboxConfig(extra_volumes=[["/host", "/vm", "xx"]])

    def test_extra_volumes_relative_host_raises(self):
        with pytest.raises(ValueError, match="absolute"):
            SandboxConfig(extra_volumes=[["relative/path", "/vm", "rw"]])

    def test_extra_volumes_relative_vm_raises(self):
        with pytest.raises(ValueError, match="absolute"):
            SandboxConfig(extra_volumes=[["/host", "relative/vm", "rw"]])

    def test_extra_volumes_wrong_length_raises(self):
        with pytest.raises(ValueError):
            SandboxConfig(extra_volumes=[["/host", "/vm"]])  # missing mode

    def test_extra_volumes_valid(self):
        c = SandboxConfig(extra_volumes=[["/data", "/data", "ro"]])
        assert c.extra_volumes == [["/data", "/data", "ro"]]

    def test_extra_config_key_rejected(self):
        with pytest.raises(ValidationError):
            SandboxConfig(unknown_key="x")  # extra="forbid"

    def test_aliases_accept_both_camel_and_snake(self):
        """populate_by_name=True + alias_generator=to_camel must let users
        pass either ``max_message_bytes`` or ``maxMessageBytes`` (and same for
        the rest of the snake_case fields). This locks down the loader contract
        so a future field rename can't silently drop snake_case support."""
        from raven.sandbox.config import SandboxDebugConfig

        snake = SandboxDebugConfig.model_validate(
            {
                "enabled": True,
                "socket": "x.sock",
                "max_message_bytes": 2048,
            }
        )
        camel = SandboxDebugConfig.model_validate(
            {
                "enabled": True,
                "socket": "x.sock",
                "maxMessageBytes": 2048,
            }
        )
        assert snake.max_message_bytes == camel.max_message_bytes == 2048

        snake_outer = SandboxConfig.model_validate(
            {
                "backend": "auto",
                "memory_mib": 4096,
                "allow_net": False,
            }
        )
        camel_outer = SandboxConfig.model_validate(
            {
                "backend": "auto",
                "memoryMib": 4096,
                "allowNet": False,
            }
        )
        assert snake_outer.memory_mib == camel_outer.memory_mib == 4096
        assert snake_outer.allow_net is camel_outer.allow_net is False


# ---------------------------------------------------------------------------
# build_executor
# ---------------------------------------------------------------------------


class TestBuildExecutor:
    def test_none_config_returns_direct(self, tmp_path):
        e = build_executor(None, tmp_path)
        assert isinstance(e, DirectExecutor)

    def test_backend_none_returns_direct(self, tmp_path):
        e = build_executor(SandboxConfig(backend="none"), tmp_path)
        assert isinstance(e, DirectExecutor)

    def test_backend_auto_without_boxlite_raises(self, tmp_path):
        with patch.dict("sys.modules", {"boxlite": None}):
            with pytest.raises(SandboxInitError, match="No sandbox backend available"):
                build_executor(SandboxConfig(backend="auto"), tmp_path)

    def test_backend_boxlite_without_boxlite_raises(self, tmp_path):
        with patch.dict("sys.modules", {"boxlite": None}):
            with pytest.raises(SandboxInitError, match="No sandbox backend available"):
                build_executor(SandboxConfig(backend="boxlite"), tmp_path)

    def test_unknown_backend_raises(self, tmp_path):
        cfg = SandboxConfig.model_construct(backend="unknown")  # bypass validator
        with pytest.raises(SandboxInitError, match="Unknown sandbox backend"):
            build_executor(cfg, tmp_path)


# ---------------------------------------------------------------------------
# DirectExecutor
# ---------------------------------------------------------------------------


class TestDirectExecutor:
    async def test_exec_echo(self):
        e = DirectExecutor()
        result = await e.exec("echo hello")
        assert result.stdout.strip() == "hello"
        assert result.exit_code == 0

    async def test_exec_timeout(self):
        e = DirectExecutor()
        result = await e.exec("sleep 10", timeout=1)
        assert result.exit_code == -1
        assert result.timed_out
        assert "Timed" in result.stderr

    async def test_exec_timeout_preserves_partial_output(self):
        # A killed long build must not report zero bytes: everything captured
        # before the kill has to survive (P0 of the tools improvement plan).
        e = DirectExecutor()
        result = await e.exec("echo started; echo oops >&2; sleep 10", timeout=1)
        assert result.exit_code == -1
        assert result.timed_out
        assert "started" in result.stdout
        assert "oops" in result.stderr
        assert "Timed out after 1s" in result.stderr

    async def test_exec_honors_timeouts_above_600(self):
        # ExecTool owns the per-command ceiling; the executor must not clamp a
        # configured ceiling back down (maxTimeout: 3600 was silently capped).
        import raven.sandbox.direct_executor as de

        assert not hasattr(de, "_MAX_TIMEOUT")
        result = await DirectExecutor().exec("echo ok", timeout=900)
        assert result.exit_code == 0

    @pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="no bash on host")
    async def test_exec_runs_under_bash(self):
        # Models write bashisms; /bin/sh is dash on Debian-family hosts and
        # rejects them ([[, source, arrays), so exec prefers bash.
        result = await DirectExecutor().exec("[[ 1 == 1 ]] && echo bashism-ok")
        assert result.exit_code == 0
        assert "bashism-ok" in result.stdout

    async def test_exec_backgrounded_child_does_not_block(self):
        # `cmd &` holding the pipe open must not stall the call until the
        # child's own exit (communicate() used to wait for pipe EOF).
        import time

        e = DirectExecutor()
        start = time.monotonic()
        result = await e.exec("sleep 8 & echo spawned", timeout=6)
        assert time.monotonic() - start < 5
        assert result.exit_code == 0
        assert "spawned" in result.stdout

    async def test_exec_env(self):
        e = DirectExecutor()
        result = await e.exec("echo $MY_VAR", env={"MY_VAR": "sandwich"})
        assert "sandwich" in result.stdout

    async def test_inherit_env_passes_host_vars(self, monkeypatch):
        monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/task/lib")
        stripped = await DirectExecutor().exec("echo [$LD_LIBRARY_PATH]")
        inherited = await DirectExecutor(inherit_env=True).exec("echo [$LD_LIBRARY_PATH]")
        assert "[]" in stripped.stdout
        assert "[/opt/task/lib]" in inherited.stdout

    async def test_host_env_not_inherited(self, monkeypatch):
        """A sensitive host env var must not leak to executed commands."""
        monkeypatch.setenv("RAVEN_TEST_SECRET", "leak-me")
        e = DirectExecutor()
        result = await e.exec("echo secret=[$RAVEN_TEST_SECRET]")
        assert "leak-me" not in result.stdout
        assert "secret=[]" in result.stdout

    async def test_path_still_present(self):
        """PATH must survive the allowlist or every command breaks."""
        e = DirectExecutor()
        result = await e.exec("echo $PATH")
        assert result.stdout.strip()
        assert result.exit_code == 0

    async def test_is_sandboxed_false(self):
        assert DirectExecutor().is_sandboxed is False

    async def test_supports_process_spawning_false(self):
        assert DirectExecutor().supports_process_spawning is False

    async def test_lifecycle_noop(self):
        e = DirectExecutor()
        await e.start()
        await e.stop()  # both no-ops; no error


# ---------------------------------------------------------------------------
# ExecTool with mock executors
# ---------------------------------------------------------------------------


class TestExecToolWithMockExecutor:
    async def test_sandboxed_skips_deny_list(self, tmp_path):
        """Deny-list guard is skipped for sandboxed executors."""
        from raven.agent.tools.shell import ExecTool

        executor = MockExecutor()
        tool = ExecTool(executor=executor, working_dir=str(tmp_path))
        # rm -rf would normally be blocked
        result = await tool.execute("rm -rf /")
        assert "blocked" not in result
        assert len(executor.calls) == 1

    async def test_sandboxed_workspace_restriction_enforced(self, tmp_path):
        """Sandbox: workspace restriction still applied when restrict_to_workspace=True."""
        from raven.agent.tools.shell import ExecTool

        executor = MockExecutor()
        tool = ExecTool(
            executor=executor,
            working_dir=str(tmp_path),
            restrict_to_workspace=True,
        )
        result = await tool.execute("cat ../../../etc/passwd", working_dir=str(tmp_path))
        assert "blocked" in result
        assert len(executor.calls) == 0

    async def test_non_sandboxed_deny_list_runs(self, tmp_path):
        """Non-sandboxed executor: deny-list guard is applied."""
        from raven.agent.tools.shell import ExecTool

        executor = DirectMockExecutor()
        tool = ExecTool(executor=executor, working_dir=str(tmp_path))
        result = await tool.execute("rm -rf /important")
        assert "blocked" in result
        assert len(executor.calls) == 0

    # Host GUI automation (osascript / `open -a|-b`) is NOT a product default —
    # it is opt-in via extra_deny_patterns (the proactivity-eval harness sets it
    # because it runs the agent un-sandboxed on the operator's machine).
    _GUI_DENY = [r"\bosascript\b", r"\bopen\s+-[ab]\b"]

    async def test_extra_deny_patterns_block_host_gui_automation(self, tmp_path):
        """With extra_deny_patterns set, osascript / `open -a|-b` are blocked
        (non-sandboxed path), while opening a file and benign commands run."""
        from raven.agent.tools.shell import ExecTool

        async def run(cmd):
            return await ExecTool(
                executor=DirectMockExecutor(),
                working_dir=str(tmp_path),
                extra_deny_patterns=self._GUI_DENY,
            ).execute(cmd)

        for cmd in (
            "osascript -e 'tell application \"Music\" to play'",
            "open -a Music",
            "open -b com.apple.Music",
        ):
            assert "blocked" in await run(cmd), f"should block: {cmd}"

        for cmd in ("open notes.txt", "echo hi", "ls -la"):
            assert "blocked" not in await run(cmd), f"should allow: {cmd}"

        # Known accepted collateral: the security-broad ``\bosascript\b`` also
        # trips when 'osascript' is a mere argument. Pinned so a future narrowing
        # to command-position is a deliberate change, not an accident.
        assert "blocked" in await run("grep osascript /var/log/system.log")

    async def test_gui_automation_not_blocked_by_product_default(self, tmp_path):
        """Product default (no extra_deny_patterns): osascript is NOT blocked —
        the GUI-automation block is eval-scoped, not shipped behaviour."""
        from raven.agent.tools.shell import ExecTool

        executor = DirectMockExecutor()
        result = await ExecTool(executor=executor, working_dir=str(tmp_path)).execute("osascript -e x")
        assert "blocked" not in result
        assert len(executor.calls) == 1

    async def test_path_append_sandboxed_injects_export(self, tmp_path):
        """path_append with sandboxed executor: wraps command with export PATH."""
        from raven.agent.tools.shell import ExecTool

        executor = MockExecutor()
        tool = ExecTool(executor=executor, working_dir=str(tmp_path), path_append="/custom/bin")
        await tool.execute("mycommand")
        call = executor.calls[0]
        assert "export PATH=" in call["command"]
        assert "/custom/bin" in call["command"]
        assert call["env"] is None

    async def test_path_append_non_sandboxed_uses_env(self, tmp_path):
        """path_append with non-sandboxed executor: env dict has extended PATH."""
        from raven.agent.tools.shell import ExecTool

        executor = DirectMockExecutor()
        tool = ExecTool(executor=executor, working_dir=str(tmp_path), path_append="/custom/bin")
        await tool.execute("mycommand")
        call = executor.calls[0]
        assert call["env"] is not None
        assert "/custom/bin" in call["env"]["PATH"]
        # Only PATH is passed — not a copy of the full host environment (which
        # would leak host secrets past DirectExecutor's baseline allowlist).
        assert set(call["env"]) == {"PATH"}
        # command unchanged
        assert "export PATH" not in call["command"]

    async def test_timeout_zero_passed_through(self, tmp_path):
        """timeout=0 is not replaced by the default timeout."""
        from raven.agent.tools.shell import ExecTool

        executor = MockExecutor()
        tool = ExecTool(executor=executor, working_dir=str(tmp_path), timeout=60)
        await tool.execute("cmd", timeout=0)
        assert executor.calls[0]["timeout"] == 0

    async def test_default_executor_is_direct(self):
        """ExecTool() with no executor arg uses DirectExecutor."""
        from raven.agent.tools.shell import ExecTool

        tool = ExecTool()
        assert isinstance(tool._executor, DirectExecutor)
        assert tool._executor.is_sandboxed is False


# ---------------------------------------------------------------------------
# BoxliteExecutor._translate_cwd
# ---------------------------------------------------------------------------


class TestBoxliteTranslateCwd:
    def _make_executor(self, workspace: Path) -> BoxliteExecutor:
        return BoxliteExecutor(
            image="ubuntu:22.04",
            workspace=workspace,
        )

    def test_none_returns_workspace_mount(self, tmp_path):
        e = self._make_executor(tmp_path)
        assert e._translate_cwd(None) == "/workspace"

    def test_workspace_root_returns_workspace_mount(self, tmp_path):
        e = self._make_executor(tmp_path)
        assert e._translate_cwd(str(tmp_path)) == "/workspace"

    def test_subdir_translates_correctly(self, tmp_path):
        sub = tmp_path / "sub" / "dir"
        e = self._make_executor(tmp_path)
        assert e._translate_cwd(str(sub)) == "/workspace/sub/dir"

    def test_outside_path_falls_back_to_workspace(self, tmp_path):
        e = self._make_executor(tmp_path)
        result = e._translate_cwd("/completely/outside")
        assert result == "/workspace"


# ---------------------------------------------------------------------------
# BoxliteExecutor._collect
# ---------------------------------------------------------------------------


class TestBoxliteCollect:
    async def _collect(self, lines: list[str]) -> str:
        async def _gen():
            for line in lines:
                yield line

        return await BoxliteExecutor._collect(_gen())

    async def test_empty_stream(self):
        assert await self._collect([]) == ""

    async def test_lines_with_newlines_not_doubled(self):
        result = await self._collect(["hello\n", "world\n"])
        assert result == "hello\nworld\n"

    async def test_lines_without_newlines_get_one_appended(self):
        result = await self._collect(["hello", "world"])
        assert result == "hello\nworld\n"

    async def test_mixed_lines(self):
        result = await self._collect(["hello\n", "world"])
        assert result == "hello\nworld\n"


# ---------------------------------------------------------------------------
# BoxliteExecutor bridge logic (mock Execution)
# ---------------------------------------------------------------------------


def _make_mock_execution(stdout_lines=None, stderr_lines=None):
    """Build a mock boxlite.Execution object."""

    async def _stdout_iter():
        for line in stdout_lines or []:
            yield line

    async def _stderr_iter():
        for line in stderr_lines or []:
            yield line

    exec_result = MagicMock()
    exec_result.exit_code = 0

    execution = MagicMock()
    execution.stdout.return_value = _stdout_iter()
    execution.stderr.return_value = _stderr_iter()
    execution.wait = AsyncMock(return_value=exec_result)
    execution.kill = AsyncMock()
    execution.stdin.return_value = MagicMock(send_input=AsyncMock())
    return execution


class TestBoxliteExecTimeout:
    async def test_timeout_kills_and_returns_minus_one(self, tmp_path):
        """exec() times out: execution.kill() is called, exit_code=-1 returned."""
        executor = BoxliteExecutor(image="ubuntu:22.04", workspace=tmp_path, default_timeout=1)

        mock_box = MagicMock()
        execution = _make_mock_execution()

        async def _slow_exec(*a, **kw):
            await asyncio.sleep(10)
            return execution

        mock_box.exec = _slow_exec
        executor._box = mock_box

        result = await executor.exec("sleep 10", timeout=1)
        assert result.exit_code == -1
        assert "timed out" in result.stderr.lower()

    async def test_exec_timeout_execution_kill_called(self, tmp_path):
        """When execution handle is obtained before timeout, kill() must be called."""
        executor = BoxliteExecutor(image="ubuntu:22.04", workspace=tmp_path, default_timeout=1)

        execution = _make_mock_execution()
        execution.stdout.return_value = _infinite_stream()
        execution.stderr.return_value = _infinite_stream()

        async def _slow_wait():
            await asyncio.sleep(10)
            return MagicMock(exit_code=0)

        execution.wait = AsyncMock(side_effect=_slow_wait)

        mock_box = MagicMock()
        mock_box.exec = AsyncMock(return_value=execution)
        executor._box = mock_box

        result = await executor.exec("cmd", timeout=1)
        assert result.exit_code == -1
        execution.kill.assert_awaited_once()


async def _infinite_stream():
    while True:
        await asyncio.sleep(10)
        yield "line"


class TestBoxliteVerifyTimeout:
    async def test_verify_timeout_raises_init_error(self, tmp_path):
        """_verify() timeout → SandboxInitError with 'timed out' message."""
        execution = _make_mock_execution()

        async def _slow_wait():
            await asyncio.sleep(10)
            return MagicMock(exit_code=0)

        execution.wait = AsyncMock(side_effect=_slow_wait)
        execution.stdout.return_value = _infinite_stream()
        execution.stderr.return_value = _infinite_stream()

        mock_box = MagicMock()
        mock_box.exec = AsyncMock(return_value=execution)

        executor = BoxliteExecutor(image="ubuntu:22.04", workspace=tmp_path, verify_timeout=1)
        with pytest.raises(SandboxInitError, match="timed out"):
            await executor._verify(mock_box)
        execution.kill.assert_awaited_once()


class TestBoxliteStop:
    async def test_stop_kills_executions_and_cancels_tasks(self, tmp_path):
        """stop() kills each stored execution and cancels each stored task."""
        executor = BoxliteExecutor(image="ubuntu:22.04", workspace=tmp_path)

        exec1 = MagicMock()
        exec1.kill = AsyncMock()
        exec2 = MagicMock()
        exec2.kill = AsyncMock()
        executor._process_executions = [exec1, exec2]

        done_event = asyncio.Event()

        async def _long_task():
            await done_event.wait()

        task = asyncio.create_task(_long_task())
        executor._process_tasks = [task]

        await executor.stop()

        exec1.kill.assert_awaited_once()
        exec2.kill.assert_awaited_once()
        assert executor._process_executions == []
        assert executor._process_tasks == []
        assert task.cancelled()


class TestBoxliteCleanupOrdering:
    """P2.1: a VM ID must remain in owned_ids until cleanup actually finishes."""

    async def test_owned_ids_kept_until_after_box_stop(self, tmp_path, monkeypatch):
        owned: set[str] = set()
        executor = BoxliteExecutor(
            image="ubuntu:22.04",
            workspace=tmp_path,
            owned_ids=owned,
        )

        mock_box = MagicMock()
        mock_box.id = "vm-cleanup-1"
        owned_during_stop: list[bool] = []

        async def _stop():
            # While box.stop() is running, ownership must still be claimed —
            # otherwise a concurrent `sandbox ls` would mark the VM as orphan.
            owned_during_stop.append(mock_box.id in owned)

        mock_box.stop = AsyncMock(side_effect=_stop)
        executor._box = mock_box
        owned.add(mock_box.id)

        # Stub out the runtime.remove() call so we don't import boxlite.
        from raven.sandbox import _runtime as rt_mod

        fake_runtime = MagicMock()
        fake_runtime.remove = AsyncMock()
        monkeypatch.setattr(rt_mod, "get_boxlite_runtime", lambda: fake_runtime)

        await executor._cleanup_box()

        assert owned_during_stop == [True], "owned_ids.discard() must run AFTER box.stop(), not before."
        assert "vm-cleanup-1" not in owned, "ownership must be released after cleanup"
        assert executor._box is None


class TestBoxliteStartFailureCleanup:
    """R1: a partial-start failure must run cleanup so we don't leak VMs.

    `loop.py` enters the executor via AsyncExitStack; when start() raises,
    __aexit__ is *not* called by Python's context-manager protocol, so the
    executor itself must clean up before re-raising.
    """

    async def test_box_start_failure_runs_cleanup_box(self, tmp_path, monkeypatch):
        owned: set[str] = set()
        executor = BoxliteExecutor(
            image="ubuntu:22.04",
            workspace=tmp_path,
            owned_ids=owned,
        )

        mock_box = MagicMock()
        mock_box.id = "vm-partial-start"
        # box.start() blows up after create() succeeded — the partial-start case.
        mock_box.start = AsyncMock(side_effect=RuntimeError("vm refused to boot"))
        cleanup_called: list[str] = []

        async def _stop():
            cleanup_called.append("stop")

        mock_box.stop = AsyncMock(side_effect=_stop)

        fake_runtime = MagicMock()
        fake_runtime.create = AsyncMock(return_value=mock_box)
        fake_runtime.remove = AsyncMock()

        from raven.sandbox import _runtime as rt_mod

        monkeypatch.setattr(rt_mod, "get_boxlite_runtime", lambda: fake_runtime)

        # Patch boxlite.BoxOptions so we can construct it without the real package.
        import sys

        fake_boxlite = MagicMock()
        fake_boxlite.BoxOptions = MagicMock(return_value=MagicMock())
        monkeypatch.setitem(sys.modules, "boxlite", fake_boxlite)

        with pytest.raises(RuntimeError, match="vm refused to boot"):
            await executor.start()

        assert cleanup_called == ["stop"], "box.stop() must be called when start() fails mid-way"
        assert "vm-partial-start" not in owned, "ownership must be released on failed start"
        assert executor._box is None

    async def test_runtime_create_failure_runs_cleanup_without_box(self, tmp_path, monkeypatch):
        """If runtime.create() itself fails, no box was ever created — cleanup
        must still complete cleanly without leaking a VM ID into owned_ids and
        without raising over the missing _box."""
        owned: set[str] = set()
        executor = BoxliteExecutor(
            image="ubuntu:22.04",
            workspace=tmp_path,
            owned_ids=owned,
        )

        fake_runtime = MagicMock()
        fake_runtime.create = AsyncMock(side_effect=RuntimeError("create failed"))

        from raven.sandbox import _runtime as rt_mod

        monkeypatch.setattr(rt_mod, "get_boxlite_runtime", lambda: fake_runtime)

        import sys

        fake_boxlite = MagicMock()
        fake_boxlite.BoxOptions = MagicMock(return_value=MagicMock())
        monkeypatch.setitem(sys.modules, "boxlite", fake_boxlite)

        with pytest.raises(RuntimeError, match="create failed"):
            await executor.start()

        assert owned == set(), "no VM was created → owned_ids must remain empty"
        assert executor._box is None

    async def test_verify_failure_runs_cleanup_box(self, tmp_path, monkeypatch):
        """_verify() failure during start() must run the registered cleanup so
        a box that booted but failed the echo-ok probe doesn't leak. Pairs
        with TestBoxliteVerifyTimeout, which only exercises _verify() directly
        — this test covers the start()-level integration.
        """
        owned: set[str] = set()
        executor = BoxliteExecutor(
            image="ubuntu:22.04",
            workspace=tmp_path,
            owned_ids=owned,
            verify_timeout=1,
        )

        mock_box = MagicMock()
        mock_box.id = "vm-verify-fails"
        mock_box.start = AsyncMock()  # boot succeeds
        cleanup_called: list[str] = []

        async def _stop():
            cleanup_called.append("stop")

        mock_box.stop = AsyncMock(side_effect=_stop)

        # _verify runs `echo ok`; make wait() hang so verify_timeout fires.
        async def _slow_wait():
            await asyncio.sleep(10)
            return MagicMock(exit_code=0)

        execution = _make_mock_execution()
        execution.wait = AsyncMock(side_effect=_slow_wait)
        execution.stdout.return_value = _infinite_stream()
        execution.stderr.return_value = _infinite_stream()
        mock_box.exec = AsyncMock(return_value=execution)

        fake_runtime = MagicMock()
        fake_runtime.create = AsyncMock(return_value=mock_box)
        fake_runtime.remove = AsyncMock()

        from raven.sandbox import _runtime as rt_mod

        monkeypatch.setattr(rt_mod, "get_boxlite_runtime", lambda: fake_runtime)

        import sys

        fake_boxlite = MagicMock()
        fake_boxlite.BoxOptions = MagicMock(return_value=MagicMock())
        monkeypatch.setitem(sys.modules, "boxlite", fake_boxlite)

        with pytest.raises(SandboxInitError, match="timed out"):
            await executor.start()

        assert cleanup_called == ["stop"], (
            "_verify failure must trigger _cleanup_after_failed_start → _cleanup_box → box.stop()"
        )
        assert "vm-verify-fails" not in owned, "ownership must be released on _verify failure"
        assert executor._box is None


class TestBoxliteStartProcessBridges:
    """Bridge behaviour tests via mock Execution."""

    async def test_stdout_bridge_forwards_valid_json(self, tmp_path):
        """Valid JSON lines from VM stdout reach the anyio read stream as SessionMessage."""
        mcp_types = pytest.importorskip("mcp.types", reason="mcp not installed")
        from mcp.shared.message import SessionMessage

        executor = BoxliteExecutor(image="ubuntu:22.04", workspace=tmp_path)
        mock_box = MagicMock()

        # Real boxlite yields chunks with trailing \n (lines)
        json_line = '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'

        async def _stdout():
            yield json_line

        async def _stderr():
            return
            yield  # make it an async generator

        execution = MagicMock()
        execution.stdout.return_value = _stdout()
        execution.stderr.return_value = _stderr()
        execution.stdin.return_value = MagicMock(send_input=AsyncMock())
        mock_box.exec = AsyncMock(return_value=execution)
        executor._box = mock_box

        read_recv, write_send = await executor.start_process("mcp-server", [])

        # Give bridge tasks a moment to run
        await asyncio.sleep(0.05)

        msg = await asyncio.wait_for(read_recv.receive(), timeout=1.0)
        # MCP SDK 1.x: read stream carries SessionMessage wrapping JSONRPCMessage
        assert isinstance(msg, SessionMessage)
        assert isinstance(msg.message, mcp_types.JSONRPCMessage)

    async def test_stdout_bridge_skips_non_json_lines(self, tmp_path):
        """Non-JSON stdout lines (e.g. npm progress) are logged and skipped, not forwarded.

        Forwarding them as Exception objects would break ClientSession before the MCP
        server has a chance to start (e.g. during npx package download).
        """
        pytest.importorskip("mcp", reason="mcp not installed")
        from mcp.types import JSONRPCMessage

        executor = BoxliteExecutor(image="ubuntu:22.04", workspace=tmp_path)
        mock_box = MagicMock()

        json_line = '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'

        async def _stdout():
            yield "npm warn: some download progress\n"  # non-JSON — must be skipped
            yield json_line  # valid JSON — must arrive

        async def _stderr():
            return
            yield

        execution = MagicMock()
        execution.stdout.return_value = _stdout()
        execution.stderr.return_value = _stderr()
        execution.stdin.return_value = MagicMock(send_input=AsyncMock())
        mock_box.exec = AsyncMock(return_value=execution)
        executor._box = mock_box

        read_recv, _ = await executor.start_process("mcp-server", [])
        await asyncio.sleep(0.05)

        from mcp.shared.message import SessionMessage as SM

        # The first (and only) message must be a SessionMessage wrapping the valid JSON line
        msg = await asyncio.wait_for(read_recv.receive(), timeout=1.0)
        assert isinstance(msg, SM), f"Expected SessionMessage, got {type(msg)}: {msg}"
        assert isinstance(msg.message, JSONRPCMessage)

    async def test_stdin_bridge_sends_json(self, tmp_path):
        """JSONRPCMessage sent to write stream is forwarded to ExecStdin.send_input()."""
        JSONRPCMessage = pytest.importorskip("mcp.types", reason="mcp not installed").JSONRPCMessage

        executor = BoxliteExecutor(image="ubuntu:22.04", workspace=tmp_path)
        mock_box = MagicMock()

        stdin_mock = MagicMock()
        stdin_mock.send_input = AsyncMock()

        async def _stdout():
            return
            yield

        async def _stderr():
            return
            yield

        execution = MagicMock()
        execution.stdout.return_value = _stdout()
        execution.stderr.return_value = _stderr()
        execution.stdin.return_value = stdin_mock
        mock_box.exec = AsyncMock(return_value=execution)
        executor._box = mock_box

        _, write_send = await executor.start_process("mcp-server", [])

        # MCP SDK 1.x: write stream carries SessionMessage
        from mcp.shared.message import SessionMessage

        rpc_msg = JSONRPCMessage.model_validate({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        await write_send.send(SessionMessage(message=rpc_msg))
        await asyncio.sleep(0.05)

        stdin_mock.send_input.assert_awaited()
        raw = stdin_mock.send_input.call_args[0][0]
        assert raw.endswith(b"\n")
        import json

        payload = json.loads(raw.decode())
        assert payload["method"] == "ping"


# ---------------------------------------------------------------------------
# AgentLoop executor lifecycle (mock executor + mock bus/provider)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_provider():
    p = MagicMock()
    p.get_default_model.return_value = "mock-model"
    return p


class FailingExecutor(SandboxExecutor):
    """Executor whose start() always raises SandboxInitError."""

    async def exec(self, *a, **kw) -> ExecResult:
        raise NotImplementedError

    async def start(self) -> None:
        raise SandboxInitError("test: sandbox failed to start")


class TestAgentLoopExecutorLifecycle:
    async def test_start_executor_idempotent(self, tmp_path, mock_provider):
        """Calling _start_executor() twice only initialises once."""
        from raven.agent.loop import AgentLoop

        loop = AgentLoop(provider=mock_provider, workspace=tmp_path)
        # Inject a no-op executor
        loop._executor = MockExecutor()
        await loop._start_executor()
        stack_first = loop._executor_stack
        await loop._start_executor()
        assert loop._executor_stack is stack_first  # same object

    async def test_start_executor_failing_executor_leaves_stack_none(self, tmp_path, mock_provider):
        """SandboxInitError from start() propagates; _executor_stack stays None."""
        from raven.agent.loop import AgentLoop

        loop = AgentLoop(provider=mock_provider, workspace=tmp_path)
        loop._executor = FailingExecutor()
        with pytest.raises(SandboxInitError):
            await loop._start_executor()
        assert loop._executor_stack is None
        assert loop._executor_started is False

    async def test_close_mcp_resets_flags(self, tmp_path, mock_provider):
        """close_mcp() resets _mcp_connected and _mcp_connecting."""
        from raven.agent.loop import AgentLoop

        loop = AgentLoop(provider=mock_provider, workspace=tmp_path)
        loop._mcp_connected = True
        loop._mcp_connecting = True
        await loop.close_mcp()
        assert loop._mcp_connected is False
        assert loop._mcp_connecting is False

    @staticmethod
    def _cli_req():
        from raven.spine import ChatType, Origin, Source, TurnRequest

        return TurnRequest(
            origin=Origin.USER,
            source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
            text="hello",
            conversation="cli:c",
        )

    async def test_run_turn_closes_executor_on_unexpected_error(self, tmp_path, mock_provider):
        """run_turn() closes the executor when _connect_mcp raises a non-SandboxInitError."""
        from raven.agent.loop import AgentLoop

        stopped = []

        class TrackingExecutor(SandboxExecutor):
            async def exec(self, *a, **kw) -> ExecResult:
                raise NotImplementedError

            async def stop(self) -> None:
                stopped.append(True)

        loop = AgentLoop(provider=mock_provider, workspace=tmp_path, mcp_servers={"svc": object()})
        loop._executor = TrackingExecutor()

        async def _failing_connect_mcp():
            raise RuntimeError("unexpected network error")

        loop._connect_mcp = _failing_connect_mcp

        async def _emit(_ev):
            pass

        with pytest.raises(RuntimeError, match="unexpected network error"):
            await loop.run_turn(self._cli_req(), _emit, lambda: [])
        assert stopped == [True], "executor.stop() must be called on unexpected exception"

    async def test_run_turn_closes_executor_on_sandbox_init_error(self, tmp_path, mock_provider):
        """run_turn() closes the executor when SandboxInitError is raised. Unlike
        the old string-returning path (which returned a "[Sandbox error]" string), the spine path
        re-raises — the scheduler turns it into a TurnFailed event, the intended
        spine error surface."""
        from raven.agent.loop import AgentLoop

        stopped = []

        class StartedThenFailsMCP(SandboxExecutor):
            """Starts fine, but triggers SandboxInitError via _connect_mcp path."""

            async def exec(self, *a, **kw) -> ExecResult:
                raise NotImplementedError

            async def stop(self) -> None:
                stopped.append(True)

        loop = AgentLoop(
            provider=mock_provider,
            workspace=tmp_path,
            mcp_servers={"svc": object()},  # non-empty so _connect_mcp is attempted
        )
        loop._executor = StartedThenFailsMCP()

        # Patch _connect_mcp to raise SandboxInitError after executor starts
        async def _failing_connect_mcp():
            raise SandboxInitError("test: MCP sandbox guard fired")

        loop._connect_mcp = _failing_connect_mcp

        async def _emit(_ev):
            pass

        with pytest.raises(SandboxInitError):
            await loop.run_turn(self._cli_req(), _emit, lambda: [])
        assert stopped == [True], "executor.stop() must be called to avoid VM leak"


# ---------------------------------------------------------------------------
# connect_mcp_servers sandbox guard
# ---------------------------------------------------------------------------


class TestConnectMcpSandboxGuard:
    async def test_stdio_sandboxed_no_spawning_raises(self):
        """Sandboxed executor without process-spawning raises SandboxInitError for stdio."""
        from contextlib import AsyncExitStack

        from raven.agent.tools.mcp import connect_mcp_servers
        from raven.agent.tools.registry import ToolRegistry

        executor = MockExecutor()  # is_sandboxed=True, supports_process_spawning=False
        cfg = MagicMock()
        cfg.type = "stdio"
        cfg.command = "mcp-server"
        cfg.args = []
        with pytest.raises(SandboxInitError, match="stdio transport"):
            await connect_mcp_servers({"svc": cfg}, ToolRegistry(), AsyncExitStack(), executor=executor)

    async def test_stdio_no_executor_does_not_raise(self, monkeypatch):
        """executor=None falls through to the normal stdio path (no guard triggered)."""
        from contextlib import AsyncExitStack

        import mcp.client.stdio

        from raven.agent.tools.mcp import connect_mcp_servers
        from raven.agent.tools.registry import ToolRegistry

        reached = []

        def fake_stdio_client(params):
            reached.append(params.command)
            raise RuntimeError("stdio_client reached — expected in test")

        monkeypatch.setattr(mcp.client.stdio, "stdio_client", fake_stdio_client)

        cfg = MagicMock()
        cfg.type = "stdio"
        cfg.command = "mcp-server"
        cfg.args = []
        cfg.env = None
        cfg.tool_timeout = 30
        # Guard should NOT raise; the transport error is caught per-server and logged.
        await connect_mcp_servers({"svc": cfg}, ToolRegistry(), AsyncExitStack(), executor=None)

        assert reached == ["mcp-server"]

    async def test_stdio_sandboxed_with_spawning_does_not_raise(self):
        """Sandboxed executor that supports spawning does not trigger the guard."""
        from contextlib import AsyncExitStack

        from raven.agent.tools.mcp import connect_mcp_servers
        from raven.agent.tools.registry import ToolRegistry

        class SpawningExecutor(MockExecutor):
            @property
            def supports_process_spawning(self) -> bool:
                return True

            async def start_process(self, command, args, env=None):
                raise RuntimeError("start_process called — expected in test")

        cfg = MagicMock()
        cfg.type = "stdio"
        cfg.command = "mcp-server"
        cfg.args = []
        cfg.env = None
        cfg.tool_timeout = 30
        # Guard should NOT raise; error comes from start_process stub instead.
        try:
            await connect_mcp_servers({"svc": cfg}, ToolRegistry(), AsyncExitStack(), executor=SpawningExecutor())
        except SandboxInitError:
            pytest.fail("SandboxInitError should not be raised when spawning is supported")

    async def test_sandbox_guard_on_second_server_partial_registration(self):
        """SandboxInitError on server 2 aborts after server 1 was already processed.

        Verifies: the guard raises (not swallowed), even after prior servers connected.
        """
        from contextlib import AsyncExitStack

        from raven.agent.tools.mcp import connect_mcp_servers
        from raven.agent.tools.registry import ToolRegistry

        executor = MockExecutor()  # is_sandboxed=True, supports_process_spawning=False

        cfg_http = MagicMock()
        cfg_http.type = "streamableHttp"
        cfg_http.url = "http://example.com/mcp"
        cfg_http.command = None
        cfg_http.headers = None

        cfg_stdio = MagicMock()
        cfg_stdio.type = "stdio"
        cfg_stdio.command = "mcp-server"
        cfg_stdio.args = []

        # Server 1 is HTTP (no guard) — mock the transport so no real network request is
        # made. anyio's cancel scopes inside streamable_http_client break when a real HTTP
        # request fails in a pytest-asyncio test context; mocking avoids that.
        # Server 2 is stdio (guard fires). SandboxInitError must propagate, not be swallowed.
        from contextlib import asynccontextmanager
        from unittest.mock import patch

        @asynccontextmanager
        async def _failing_http(*args, **kwargs):
            raise ConnectionError("mock: no network in tests")
            yield  # make it a generator

        with patch("mcp.client.streamable_http.streamable_http_client", _failing_http):
            with pytest.raises(SandboxInitError, match="stdio transport"):
                await connect_mcp_servers(
                    {"http_svc": cfg_http, "stdio_svc": cfg_stdio},
                    ToolRegistry(),
                    AsyncExitStack(),
                    executor=executor,
                )


# ---------------------------------------------------------------------------
# SubagentManager sandbox lifecycle
# ---------------------------------------------------------------------------


class TestSubagentSandboxLifecycle:
    async def test_run_subagent_starts_and_stops_executor(self, mock_provider, tmp_path):
        """_run_subagent starts the executor via async with and stops it on completion."""
        from raven.agent.subagent import SubagentManager

        started = []
        stopped = []

        class TrackingExecutor(MockExecutor):
            async def start(self) -> None:
                started.append(True)

            async def stop(self) -> None:
                stopped.append(True)

        async def fake_build(cfg, workspace):
            return TrackingExecutor()

        manager = SubagentManager(
            provider=mock_provider,
            workspace=tmp_path,
        )
        manager.set_submit(lambda req: None)  # _announce_result asserts submit is wired

        # Patch build_executor inside the subagent.manager module for this test.
        # subagent.py now lives in a package
        # (``raven.agent.subagent``); the runtime call site is now
        # in ``raven.agent.subagent.manager`` and that's the module
        # whose snapshot of ``build_executor`` must be replaced.
        import raven.agent.subagent.manager as subagent_mod

        original = subagent_mod.build_executor

        def _patched_build(cfg, workspace, owned_ids=None, inherit_env=False):
            return TrackingExecutor()

        subagent_mod.build_executor = _patched_build
        try:
            # Patch the inner method so the agent loop completes quickly
            async def _fast_inner(task_id, task, label, origin, executor):
                await manager._announce_result(task_id, label, task, "done", origin, "ok")

            manager._run_subagent_inner = _fast_inner
            await manager._run_subagent(
                "t1", "test task", "test", {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"}
            )
        finally:
            subagent_mod.build_executor = original

        assert started == [True], "executor.start() should have been called"
        assert stopped == [True], "executor.stop() should have been called"

    async def test_announce_result_submits_subagent_origin_fire_and_forget(self, mock_provider, tmp_path):
        """With submit wired, result re-injection submits a SUBAGENT-origin turn
        (source=originating channel, conversation=originating session) and is
        fire-and-forget — never awaiting result()."""
        from raven.agent.subagent import SubagentManager
        from raven.spine import Origin

        captured = {}

        class _Handle:
            def __init__(self):
                self.result_awaited = False

            async def result(self):
                self.result_awaited = True
                return None

        handle = _Handle()
        manager = SubagentManager(provider=mock_provider, workspace=tmp_path)
        manager.set_submit(lambda req: (captured.__setitem__("req", req), handle)[1])

        await manager._announce_result(
            "t1",
            "label",
            "task",
            "done",
            {"channel": "weixin", "chat_id": "u1", "session_key": "weixin:u1"},
            "ok",
        )

        req = captured["req"]
        assert req.origin is Origin.SUBAGENT
        assert req.source.channel == "weixin" and req.source.chat_id == "u1"
        assert req.source.sender_id == "subagent"
        assert req.conversation == "weixin:u1"
        assert handle.result_awaited is False  # fire-and-forget


def test_build_executor_warns_when_backend_none(monkeypatch, tmp_path):
    """Running unsandboxed must surface a loud warning (it's silent otherwise)."""
    from loguru import logger

    import raven.sandbox as sandbox_mod
    from raven.sandbox import SandboxConfig, build_executor

    monkeypatch.setattr(sandbox_mod, "_warned_no_sandbox", False)
    msgs: list[str] = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        build_executor(SandboxConfig(backend="none"), tmp_path)
    finally:
        logger.remove(sink)
    assert any("no isolation" in m for m in msgs)


# ---------------------------------------------------------------------------
# Deny-list scope + configurability
# ---------------------------------------------------------------------------


class TestExecToolDenyListScope:
    """The default deny-list is scoped at unrecoverable operations, and an
    operator can replace it outright (deny_patterns=[] for throwaway containers)."""

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /",
            "rm -rf /*",
            "rm -rf ~",
            "rm -rf $HOME",
            "rm -rf /important",
            "rm -rf -- /important",
            "rm -fr /etc/nginx",
            "rm -r /usr/lib/python3",
            "dd if=/dev/zero of=/dev/sda",
            "mkfs.ext4 /dev/sdb1",
            "shutdown -h now",
            # Newline is a command separator too — a second line must not slip
            # past the command-position anchor.
            "echo hi\nshutdown -h now",
            "true\nmkfs.ext4 /dev/sdb1",
            "echo a\nformat c:",
        ],
    )
    async def test_unrecoverable_commands_still_blocked(self, tmp_path, command):
        from raven.agent.tools.shell import ExecTool

        executor = DirectMockExecutor()
        result = await ExecTool(executor=executor, working_dir=str(tmp_path)).execute(command)
        assert "blocked" in result, command
        assert len(executor.calls) == 0

    @pytest.mark.parametrize(
        "command",
        [
            "rm -f build/*.o",
            "rm -rf /tmp/test-clone",
            "rm -f /var/lib/dpkg/lock-frontend",
            "rm -f /etc/nginx/sites-enabled/default",
            "make clean; rm -f *.o",
            "dd if=/dev/zero of=disk.img bs=1M count=10",
            # Deny names as flags/arguments, not invocations — observed false
            # positives that stranded agents in eval (TB install-windows-3.11,
            # qemu-alpine-ssh).
            "qemu-system-x86_64 -hda win311.img -no-reboot -no-shutdown",
            "which unsquashfs mksquashfs mkfs.ext4",
            "ls /usr/sbin | grep mkfs",
        ],
    )
    async def test_ordinary_cleanup_allowed(self, tmp_path, command):
        """Routine destructive work below the first directory level runs: blocking
        it strands the agent with no alternative path."""
        from raven.agent.tools.shell import ExecTool

        executor = DirectMockExecutor()
        result = await ExecTool(executor=executor, working_dir=str(tmp_path)).execute(command)
        assert "blocked" not in result, command
        assert len(executor.calls) == 1

    async def test_guard_error_names_pattern_and_match(self, tmp_path):
        """A blocked command reports what matched, so the model can tell a real
        denial from a false positive instead of retrying blind."""
        from raven.agent.tools.shell import ExecTool

        executor = DirectMockExecutor()
        result = await ExecTool(executor=executor, working_dir=str(tmp_path)).execute("shutdown -h now")
        assert "matched deny pattern" in result
        assert "shutdown" in result

    async def test_over_limit_timeout_is_clamped_with_note(self, tmp_path):
        """timeout above the ceiling runs clamped instead of being rejected —
        the intent (needs longer) is valid, only the number is out of range."""
        from raven.agent.tools.shell import ExecTool

        executor = DirectMockExecutor()
        tool = ExecTool(executor=executor, working_dir=str(tmp_path))
        result = await tool.execute("echo hi", timeout=900)
        assert "clamped" in result
        assert "background:true" in result
        assert len(executor.calls) == 1

    async def test_empty_deny_patterns_disables_guard(self, tmp_path):
        from raven.agent.tools.shell import ExecTool

        executor = DirectMockExecutor()
        tool = ExecTool(executor=executor, working_dir=str(tmp_path), deny_patterns=[])
        result = await tool.execute("rm -rf /")
        assert "blocked" not in result
        assert len(executor.calls) == 1

    async def test_none_deny_patterns_keeps_defaults(self, tmp_path):
        from raven.agent.tools.shell import ExecTool

        tool = ExecTool(executor=DirectMockExecutor(), working_dir=str(tmp_path), deny_patterns=None)
        assert tool.deny_patterns == list(ExecTool.DEFAULT_DENY_PATTERNS)

    async def test_custom_deny_patterns_replace_defaults(self, tmp_path):
        from raven.agent.tools.shell import ExecTool

        executor = DirectMockExecutor()
        tool = ExecTool(
            executor=executor,
            working_dir=str(tmp_path),
            deny_patterns=[r"\bcurl\b"],
        )
        assert "blocked" in await tool.execute("curl http://x")
        assert "blocked" not in await tool.execute("rm -rf /")


# ---------------------------------------------------------------------------
# Persistent exec sessions (stdin / background)
# ---------------------------------------------------------------------------


class TestExecSessions:
    """DirectExecutor-backed pty sessions: state persists across calls, stdin can
    be driven, and long commands keep running after exec returns."""

    @pytest.fixture
    def tools(self, tmp_path):
        from raven.agent.tools.shell import (
            ExecReadTool,
            ExecSessionRegistry,
            ExecTool,
            ExecWriteTool,
        )

        executor = DirectExecutor()
        registry = ExecSessionRegistry(executor)
        return (
            ExecTool(executor=executor, working_dir=str(tmp_path), sessions=registry),
            ExecWriteTool(registry),
            ExecReadTool(registry),
            registry,
        )

    async def test_state_persists_across_calls(self, tools, tmp_path):
        exec_tool, _, _, registry = tools
        try:
            await exec_tool.execute("export MARKER=persisted", session="s1", timeout=10)
            out = await exec_tool.execute("echo value=$MARKER", session="s1", timeout=10)
            assert "value=persisted" in out
            assert "[session: s1]" in out
        finally:
            await registry.close_all()

    async def test_cwd_persists_across_calls(self, tools, tmp_path):
        exec_tool, _, _, registry = tools
        (tmp_path / "sub").mkdir()
        try:
            await exec_tool.execute(f"cd {tmp_path / 'sub'}", session="s1", timeout=10)
            out = await exec_tool.execute("pwd", session="s1", timeout=10)
            assert "sub" in out
        finally:
            await registry.close_all()

    async def test_exec_write_answers_a_prompt(self, tools):
        exec_tool, write_tool, _, registry = tools
        try:
            await exec_tool.execute(
                "read -p 'name? ' answer && echo got=$answer",
                session="s1",
                timeout=5,
            )
            out = await write_tool.execute(session="s1", input="raven", timeout=10)
            assert "got=raven" in out
        finally:
            await registry.close_all()

    async def test_background_returns_while_command_runs(self, tools):
        exec_tool, _, read_tool, registry = tools
        try:
            # Sleep past _BACKGROUND_GRACE_S so the command demonstrably outlives
            # the exec call that started it.
            out = await exec_tool.execute("sleep 6; echo finished-later", background=True, timeout=600)
            assert "finished-later" not in out
            name = out.split("[session: ", 1)[1].split("]", 1)[0]
            follow_up = await read_tool.execute(session=name, timeout=15, close=True)
            assert "finished-later" in follow_up
            assert registry.get(name) is None
        finally:
            await registry.close_all()

    async def test_unknown_session_reports_open_ones(self, tools):
        exec_tool, _, read_tool, registry = tools
        try:
            await exec_tool.execute("true", session="alive", timeout=10)
            out = await read_tool.execute(session="ghost", timeout=1)
            assert "no session named 'ghost'" in out
            assert "alive" in out
        finally:
            await registry.close_all()

    async def test_deny_list_applies_to_session_commands(self, tmp_path):
        from raven.agent.tools.shell import ExecSessionRegistry, ExecTool

        registry = ExecSessionRegistry(DirectExecutor())
        tool = ExecTool(executor=DirectExecutor(), working_dir=str(tmp_path), sessions=registry)
        try:
            assert "blocked" in await tool.execute("rm -rf /", session="s1", timeout=5)
            assert registry.names() == []
        finally:
            await registry.close_all()

    async def test_session_cap_is_enforced(self, tools):
        exec_tool, _, _, registry = tools
        try:
            for i in range(registry.MAX_SESSIONS):
                await exec_tool.execute("true", session=f"s{i}", timeout=5)
            out = await exec_tool.execute("true", session="one-too-many", timeout=5)
            assert "too many open sessions" in out
        finally:
            await registry.close_all()

    async def test_close_all_terminates_sessions(self, tools):
        exec_tool, _, _, registry = tools
        await exec_tool.execute("true", session="s1", timeout=5)
        live = registry.get("s1")
        assert live is not None and live.running
        await registry.close_all()
        assert registry.names() == []
        assert not live.running

    async def test_unsupported_backend_explains_the_fallback(self, tmp_path):
        from raven.agent.tools.shell import ExecSessionRegistry, ExecTool

        executor = DirectMockExecutor()
        registry = ExecSessionRegistry(executor)
        tool = ExecTool(executor=executor, working_dir=str(tmp_path), sessions=registry)
        out = await tool.execute("echo hi", session="s1", timeout=5)
        assert "not available" in out
        assert "read_file" in out


@pytest.mark.asyncio
class TestBackgroundJobs:
    """Detached jobs: they outlive the agent, record an outcome that survives it,
    and stay reclaimable rather than becoming orphans."""

    @pytest.fixture
    def jobs(self, tmp_path):
        from raven.agent.tools.background import BackgroundJobRegistry

        return BackgroundJobRegistry(DirectExecutor(), owner="test", root=tmp_path / "jobs")

    async def test_a_job_is_detached_from_the_agent_process(self, jobs, tmp_path):
        """The whole point: no controlling terminal and its own session.

        A background command hosted in a pty session dies of SIGHUP when that
        terminal closes, which is why a server started this way used to vanish the
        moment its agent exited.
        """
        job = await jobs.start("svc", "sleep 30", cwd=str(tmp_path), env=None)
        try:
            assert job.running
            assert os.getsid(job.pid) == job.pid, "job must lead its own session"
            assert os.getpgid(job.pid) == job.pid, "job must lead its own process group"
        finally:
            await jobs.cancel("svc")

    async def test_teardown_leaves_jobs_running(self, jobs, tmp_path):
        """Shutdown reports survivors; it must never cancel them."""
        job = await jobs.start("svc", "sleep 30", cwd=str(tmp_path), env=None)
        try:
            jobs.report_survivors()
            assert [j.name for j in jobs.survivors()] == ["svc"]
            assert job.running
        finally:
            await jobs.cancel("svc")

    async def test_exit_status_survives_the_process_that_started_it(self, jobs, tmp_path):
        """Outcome is read from a file, not a live handle.

        A command ending in an explicit ``exit`` never reaches a line appended after
        it, so the status is recorded from an EXIT trap.
        """
        await jobs.start("quick", "echo hello; exit 7", cwd=str(tmp_path), env=None)
        job = await jobs.wait("quick", timeout=15)
        assert not isinstance(job, str)
        assert job.exit_code == 7
        assert not job.running
        assert "hello" in jobs.read("quick")

    async def test_a_cancelled_job_is_not_reported_as_success(self, jobs, tmp_path):
        """Without signal traps bash runs the EXIT trap with $? still 0, so a killed
        server would be indistinguishable from one that finished cleanly."""
        await jobs.start("svc", "sleep 30", cwd=str(tmp_path), env=None)
        assert "terminated" in await jobs.cancel("svc")
        assert jobs.get("svc").exit_code == 143

    async def test_cancel_reaches_the_whole_process_group(self, jobs, tmp_path):
        """A job is usually a shell that spawned the real workload; signalling only
        the shell would leave that workload running."""
        await jobs.start("svc", "sleep 30 & sleep 30; wait", cwd=str(tmp_path), env=None)
        await asyncio.sleep(0.5)
        job = jobs.get("svc")
        await jobs.cancel("svc")
        for _ in range(40):
            await asyncio.sleep(0.1)
            try:
                os.killpg(job.pid, 0)
            except ProcessLookupError:
                break
        else:
            pytest.fail("process group survived cancel")

    async def test_reads_return_only_new_output(self, jobs, tmp_path):
        await jobs.start("chatty", "echo one; sleep 1; echo two", cwd=str(tmp_path), env=None)
        # A read straight after start can legitimately land before the first write;
        # ExecTool covers this with a grace period, so poll for it here.
        for _ in range(50):
            first = jobs.read("chatty")
            if "one" in first:
                break
            await asyncio.sleep(0.1)
        assert "one" in first
        await jobs.wait("chatty", timeout=15)
        second = jobs.read("chatty")
        assert "two" in second
        assert "one" not in second

    async def test_the_registry_is_persisted_for_later_reaping(self, jobs, tmp_path):
        """Leaving a job running is only safe if something can still find it."""
        job = await jobs.start("svc", "sleep 30", cwd=str(tmp_path), env=None)
        try:
            record = json.loads((tmp_path / "jobs" / "registry.json").read_text())
            assert [entry["pid"] for entry in record["test"]] == [job.pid]
        finally:
            await jobs.cancel("svc")

    async def test_exec_background_starts_a_job_not_a_session(self, tmp_path):
        """exec(background=true) must take the detached path, leaving no pty session
        behind to be torn down with the agent."""
        from raven.agent.tools.background import BackgroundJobRegistry
        from raven.agent.tools.shell import ExecSessionRegistry, ExecTool

        executor = DirectExecutor()
        sessions = ExecSessionRegistry(executor)
        jobs = BackgroundJobRegistry(executor, owner="test", root=tmp_path / "jobs")
        tool = ExecTool(executor=executor, working_dir=str(tmp_path), sessions=sessions, jobs=jobs)
        try:
            out = await tool.execute("sleep 30", background=True)
            assert "[job: job1" in out
            assert sessions.names() == [], "background must not open a pty session"
            assert jobs.get("job1").running
        finally:
            await jobs.cancel("job1")

    async def test_background_with_session_becomes_a_named_job(self, tmp_path):
        """`background: true` always detaches, even with `session` set — the name
        is used for the job. The old session-hosted reading silently killed
        servers at agent exit (models combining the two always meant "named
        background process", never "die with my shell")."""
        from raven.agent.tools.background import BackgroundJobRegistry
        from raven.agent.tools.shell import ExecSessionRegistry, ExecTool

        executor = DirectExecutor()
        sessions = ExecSessionRegistry(executor)
        jobs = BackgroundJobRegistry(executor, owner="test", root=tmp_path / "jobs")
        tool = ExecTool(executor=executor, working_dir=str(tmp_path), sessions=sessions, jobs=jobs)
        try:
            result = await tool.execute("sleep 30", session="mine", background=True, timeout=2)
            assert "[job: mine" in result
            assert jobs.names() == ["mine"]
            assert sessions.names() == []
        finally:
            await jobs.cancel("mine")
            await sessions.close_all()


class TestForegroundOomDeprioritized:
    """Foreground exec children get oom_score_adj 500 so a cgroup OOM kills the
    recoverable tool command, not the agent (the single-largest process when a
    build/install spreads memory across many small children)."""

    @pytest.mark.skipif(not os.path.exists("/proc/self/oom_score_adj"), reason="Linux-only")
    @pytest.mark.asyncio
    async def test_foreground_child_reports_adj_500(self):
        from raven.sandbox.direct_executor import DirectExecutor

        # Raising oom_score_adj is unprivileged, lowering is not: when the test
        # itself already runs at >= 500 the child write is a no-op denial.
        if int(open("/proc/self/oom_score_adj").read()) >= 500:
            pytest.skip("environment baseline oom_score_adj >= 500")
        result = await DirectExecutor().exec("cat /proc/self/oom_score_adj")
        assert result.stdout.strip() == "500"

    @pytest.mark.skipif(not os.path.exists("/proc/self/oom_score_adj"), reason="Linux-only")
    @pytest.mark.asyncio
    async def test_background_job_not_deprioritized(self, tmp_path):
        from raven.sandbox.direct_executor import DirectExecutor

        ex = DirectExecutor()
        log, status = str(tmp_path / "j.log"), str(tmp_path / "j.status")
        await ex.start_background(
            "cat /proc/self/oom_score_adj", cwd=str(tmp_path), env=None, log_path=log, status_path=status
        )
        for _ in range(100):
            if os.path.exists(status):
                break
            await asyncio.sleep(0.05)
        assert open(log).read().strip() != "500"


@pytest.mark.asyncio
class TestBackgroundJobGroupTruth:
    """A `cmd &` inside a job exits the wrapper instantly while the workload
    lives on in the process group. Status/cancel must track the group, not the
    wrapper: 'exited 0' with a live server behind it misleads the model, and a
    cancel that refuses ('already finished') strands the workload."""

    @pytest.fixture
    def jobs(self, tmp_path):
        from raven.agent.tools.background import BackgroundJobRegistry

        return BackgroundJobRegistry(DirectExecutor(), owner="test", root=tmp_path / "jobs")

    async def test_ampersand_job_reports_group_alive_and_cancels(self, jobs, tmp_path):
        await jobs.start("amp", "sleep 30 &", cwd=str(tmp_path), env=None)
        job = await jobs.wait("amp", timeout=15)  # wrapper exits ~instantly
        assert not isinstance(job, str)
        assert job.exit_code == 0 and not job.running
        assert job.group_alive, "the &-child must keep the process group alive"
        assert "still" in job.describe()
        out = await jobs.cancel("amp")
        assert "already finished" not in out
        for _ in range(50):
            if not job.group_alive:
                break
            await asyncio.sleep(0.1)
        assert not job.group_alive, "cancel must reap the whole group"

    async def test_truly_finished_job_still_refuses_cancel(self, jobs, tmp_path):
        await jobs.start("done", "true", cwd=str(tmp_path), env=None)
        job = await jobs.wait("done", timeout=15)
        assert not isinstance(job, str)
        for _ in range(50):
            if not job.group_alive:
                break
            await asyncio.sleep(0.1)
        out = await jobs.cancel("done")
        assert "already finished" in out

    async def test_pid_reuse_is_not_reported_as_running(self, tmp_path):
        from raven.agent.tools.background import BackgroundJob

        job = BackgroundJob(
            name="stale",
            command="sleep 999",
            pid=os.getpid(),  # alive, but started long after the job claims
            cwd=str(tmp_path),
            log_path=str(tmp_path / "x.log"),
            status_path=str(tmp_path / "x.status"),
            started_at=time.time() - 100_000,
        )
        assert job.running is False

    async def test_registry_adopts_persisted_jobs_of_prior_process(self, tmp_path):
        from raven.agent.tools.background import BackgroundJobRegistry

        first = BackgroundJobRegistry(DirectExecutor(), owner="one", root=tmp_path / "jobs")
        await first.start("svc", "sleep 30", cwd=str(tmp_path), env=None)
        try:
            second = BackgroundJobRegistry(DirectExecutor(), owner="two", root=tmp_path / "jobs")
            adopted = second.get("svc")
            assert adopted is not None and adopted.running
            assert second.next_name() != "svc"
        finally:
            await first.cancel("svc")

    async def test_registry_ignores_malformed_registry_file(self, tmp_path):
        from raven.agent.tools.background import BackgroundJobRegistry

        root = tmp_path / "jobs"
        root.mkdir()
        (root / "registry.json").write_text("{not json", encoding="utf-8")
        jobs = BackgroundJobRegistry(DirectExecutor(), owner="x", root=root)
        assert jobs.names() == []


class TestJobsRootWorkspaceScoping:
    """Same workspace -> shared job view (continue/restart sees its server);
    different workspaces -> isolated (one session cannot cancel another's)."""

    def test_same_workspace_same_root_different_workspace_different(self, tmp_path):
        from raven.agent.tools.background import _jobs_root

        a1 = _jobs_root(tmp_path / "proj-a")
        a2 = _jobs_root(tmp_path / "proj-a")
        b = _jobs_root(tmp_path / "proj-b")
        assert a1 == a2 and a1 != b
        assert a1 != _jobs_root(None)
