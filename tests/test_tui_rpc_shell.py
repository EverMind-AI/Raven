"""Tests for ``shell.exec`` -- the TUI's ``!command`` escape.

The client prints the streams and then ``exit <code>``, so a failing command
has to come back as a result. Only an RPC rejection would stop it rendering.
"""

from __future__ import annotations

from raven.tui_rpc.methods.shell import shell_exec


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class _Executor:
    def __init__(self, result: object = None, raises: Exception | None = None) -> None:
        self._result = result or _Result()
        self._raises = raises
        self.calls: list[tuple[str, str | None]] = []

    async def exec(self, command: str, cwd: str | None = None, timeout: int | None = None, env=None):
        self.calls.append((command, cwd))
        if self._raises is not None:
            raise self._raises
        return self._result


def _factory(executor: object, working_dir: str = "/tmp"):
    tool = type("_Tool", (), {"_executor": executor, "working_dir": working_dir})()
    loop = type("_Loop", (), {"tools": {"exec": tool}})()
    return lambda: loop


async def test_a_successful_command_returns_its_stdout_and_zero() -> None:
    executor = _Executor(_Result(stdout="hello\n"))
    result = await shell_exec({"command": "echo hello"}, agent_loop_factory=_factory(executor))
    assert result == {"code": 0, "stdout": "hello\n", "stderr": ""}


async def test_a_failing_command_is_a_result_not_a_rejection() -> None:
    executor = _Executor(_Result(stderr="no such file\n", exit_code=2))
    result = await shell_exec({"command": "cat missing"}, agent_loop_factory=_factory(executor))
    assert result["code"] == 2
    assert result["stderr"] == "no such file\n"


async def test_it_runs_through_the_live_agent_executor() -> None:
    executor = _Executor()
    await shell_exec({"command": "ls"}, agent_loop_factory=_factory(executor, working_dir="/srv/work"))
    assert executor.calls == [("ls", "/srv/work")]


async def test_an_empty_command_does_not_reach_the_executor() -> None:
    executor = _Executor()
    assert await shell_exec({"command": "   "}, agent_loop_factory=_factory(executor)) == {
        "code": 0,
        "stdout": "",
        "stderr": "",
    }
    assert executor.calls == []


async def test_an_executor_failure_comes_back_as_a_failed_command() -> None:
    executor = _Executor(raises=RuntimeError("sandbox is down"))
    result = await shell_exec({"command": "ls"}, agent_loop_factory=_factory(executor))
    assert result["code"] == 127
    assert "sandbox is down" in result["stderr"]


async def test_with_no_live_loop_it_still_runs_directly() -> None:
    result = await shell_exec({"command": "printf ok"}, agent_loop_factory=None)
    assert result["code"] == 0
    assert result["stdout"].strip() == "ok"
