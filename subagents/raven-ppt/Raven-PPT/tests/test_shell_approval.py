from __future__ import annotations

import pytest

from raven.agent.tools.base import ToolResult
from raven.agent.tools.shell import ExecTool
from raven.agent.tools.shell_policy import CommandDecision, ShellCommandPolicy
from raven.sandbox import ExecResult, SandboxExecutor


@pytest.fixture
def policy() -> ShellCommandPolicy:
    return ShellCommandPolicy(
        deny_patterns=[
            r"\brm\s+-[rf]{1,2}\b",
            r"\b(mkfs|diskpart)\b",
        ]
    )


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "ls -la",
        "echo hello",
        "find . -name '*.py'",
        "printf 'rm file.txt'",
        "echo 'find . -delete'",
        "git grep -n shutdown",
        "grep -rn reboot /var/log",
        "man shutdown",
        "systemctl show reboot.target",
        "grep -rn 'systemctl poweroff' docs/",
        "bash -lc 'ls'",
    ],
)
def test_safe_commands_are_allowed(policy: ShellCommandPolicy, command: str) -> None:
    assert policy.evaluate(command) is CommandDecision.ALLOW


@pytest.mark.parametrize(
    "command",
    [
        "rm -r tmp",
        "rm -f file.txt",
        "rm -rf tmp",
        "rm -fr tmp",
        "echo ready && rm -rf tmp",
        "mkfs /dev/test",
        "shutdown now",
        "halt",
        "sudo -n reboot",
        'bash -c "poweroff"',
        "systemctl poweroff",
        "systemctl reboot",
        "sudo systemctl reboot",
        "busybox poweroff",
        "loginctl poweroff",
        "systemctl -i poweroff",
        "init 0",
        "init 6",
        "telinit 0",
        "telinit 6",
    ],
)
def test_hard_denied_commands_cannot_be_approved(policy: ShellCommandPolicy, command: str) -> None:
    assert policy.evaluate(command) is CommandDecision.HARD_DENY


@pytest.mark.parametrize(
    "command",
    [
        "rm file.txt",
        "rm file1 file2",
        "/bin/rm file.txt",
        "rm --force file.txt",
        "sudo rm file.txt",
        "sudo -u root rm file.txt",
        "sudo --user=root unlink file.txt",
        "command unlink file.txt",
        "MODE=test rm file.txt",
        "env MODE=test rm file.txt",
        "env -u MODE rm file.txt",
        "unlink file.txt",
        "find ./tmp -delete",
        "echo ready && rm file.txt",
        "printf done | unlink file.txt",
        "cd /tmp\nrm file.txt",
        "cd /tmp\r\nunlink file.txt",
        "(rm file.txt)",
        "{ rm file.txt; }",
        "echo $(rm file.txt)",
        "echo `rm file.txt`",
        "nohup rm file.txt &",
        'bash -c "rm file.txt"',
        "bash -c'rm file.txt'",
        'bash -c"rm file.txt"',
        'bash --rcfile setup.sh -c "rm file.txt"',
        'sh -lc "find tmp -delete"',
        'find . -name "*.log" -exec rm {} \\;',
        'find . -name "*.log" -execdir unlink {} \\;',
        'find . -exec sh -c "rm \\"$1\\"" _ {} \\;',
    ],
)
def test_delete_commands_require_approval(policy: ShellCommandPolicy, command: str) -> None:
    assert policy.evaluate(command) is CommandDecision.REQUIRE_APPROVAL


def test_hard_deny_wins_when_command_also_matches_approval(policy: ShellCommandPolicy) -> None:
    assert policy.evaluate("unlink old.txt && rm -rf tmp") is CommandDecision.HARD_DENY


def test_matcher_failure_is_fail_closed(policy: ShellCommandPolicy) -> None:
    def broken_matcher(command: str) -> bool:
        raise RuntimeError("broken")

    policy.register_approval_matcher("broken", broken_matcher)

    assert policy.evaluate("echo harmless") is CommandDecision.HARD_DENY


@pytest.mark.parametrize("command", ["echo 'unterminated", "echo trailing\\"])
def test_shell_parse_failure_is_fail_closed(policy: ShellCommandPolicy, command: str) -> None:
    assert policy.evaluate(command) is CommandDecision.HARD_DENY


class _RecordingExecutor(SandboxExecutor):
    def __init__(self, *, sandboxed: bool) -> None:
        self._sandboxed = sandboxed
        self.commands: list[str] = []

    @property
    def is_sandboxed(self) -> bool:
        return self._sandboxed

    async def exec(self, command: str, **kwargs) -> ExecResult:
        self.commands.append(command)
        return ExecResult(stdout="ok", stderr="", exit_code=0)


class _ApprovalResponder:
    def __init__(self, answers: list[bool]) -> None:
        self.answers = answers
        self.requests: list[dict] = []

    async def await_approval(self, **request) -> bool:
        self.requests.append(request)
        return self.answers.pop(0)


async def test_direct_delete_executes_once_after_approval(tmp_path) -> None:
    executor = _RecordingExecutor(sandboxed=False)
    responder = _ApprovalResponder([True])
    tool = ExecTool(executor=executor, working_dir=str(tmp_path))
    tool.start_approval_turn(responder, conversation_id="session-a", turn_id="turn-a")
    tool.set_tool_call_id("call-a")

    result = await tool.execute("rm file.txt")

    assert "Exit code: 0" in result
    assert executor.commands == ["rm file.txt"]
    assert responder.requests == [
        {
            "conversation_id": "session-a",
            "turn_id": "turn-a",
            "tool_call_id": "call-a",
            "command": "rm file.txt",
            "description": "Delete files using a shell command",
        }
    ]


async def test_direct_delete_without_responder_is_denied(tmp_path) -> None:
    executor = _RecordingExecutor(sandboxed=False)
    tool = ExecTool(executor=executor, working_dir=str(tmp_path))

    result = await tool.execute("unlink file.txt")

    assert isinstance(result, ToolResult)
    assert result.retryable is False
    assert result.abort_action is True
    assert "requires user approval" in result.model_text
    assert "Do not retry" in result.model_text
    assert executor.commands == []


async def test_denied_command_is_not_prompted_again_in_same_turn(tmp_path) -> None:
    executor = _RecordingExecutor(sandboxed=False)
    responder = _ApprovalResponder([False])
    tool = ExecTool(executor=executor, working_dir=str(tmp_path))
    tool.start_approval_turn(responder, conversation_id="session-a", turn_id="turn-a")

    first = await tool.execute("find tmp -delete")
    second = await tool.execute("find tmp -delete")

    assert isinstance(first, ToolResult)
    assert first.retryable is False
    assert first.abort_action is True
    assert "denied" in first.model_text.lower()
    assert isinstance(second, ToolResult)
    assert "denied" in second.model_text.lower()
    assert executor.commands == []
    assert len(responder.requests) == 1


async def test_allow_once_does_not_cover_a_second_execution(tmp_path) -> None:
    executor = _RecordingExecutor(sandboxed=False)
    responder = _ApprovalResponder([True, False])
    tool = ExecTool(executor=executor, working_dir=str(tmp_path))
    tool.start_approval_turn(responder, conversation_id="session-a", turn_id="turn-a")

    first = await tool.execute("rm file.txt")
    second = await tool.execute("rm file.txt")

    assert "Exit code: 0" in first
    assert isinstance(second, ToolResult)
    assert "denied" in second.model_text.lower()
    assert executor.commands == ["rm file.txt"]
    assert len(responder.requests) == 2


async def test_new_turn_can_prompt_for_a_previously_denied_command(tmp_path) -> None:
    executor = _RecordingExecutor(sandboxed=False)
    responder = _ApprovalResponder([False, True])
    tool = ExecTool(executor=executor, working_dir=str(tmp_path))
    tool.start_approval_turn(responder, conversation_id="session-a", turn_id="turn-a")

    first = await tool.execute("unlink file.txt")
    tool.start_approval_turn(responder, conversation_id="session-a", turn_id="turn-b")
    second = await tool.execute("unlink file.txt")

    assert isinstance(first, ToolResult)
    assert "denied" in first.model_text.lower()
    assert "Exit code: 0" in second
    assert executor.commands == ["unlink file.txt"]
    assert len(responder.requests) == 2


async def test_hard_denied_command_never_requests_approval(tmp_path) -> None:
    executor = _RecordingExecutor(sandboxed=False)
    responder = _ApprovalResponder([True])
    tool = ExecTool(executor=executor, working_dir=str(tmp_path))
    tool.start_approval_turn(responder, conversation_id="session-a", turn_id="turn-a")

    result = await tool.execute("rm -rf tmp")

    assert isinstance(result, ToolResult)
    assert result.retryable is False
    assert result.abort_action is True
    assert "blocked" in result.model_text
    assert "Do not retry" in result.model_text
    assert responder.requests == []
    assert executor.commands == []


async def test_sandboxed_delete_skips_approval_and_deny_policy(tmp_path) -> None:
    executor = _RecordingExecutor(sandboxed=True)
    responder = _ApprovalResponder([False])
    tool = ExecTool(executor=executor, working_dir=str(tmp_path))
    tool.start_approval_turn(responder, conversation_id="session-a", turn_id="turn-a")

    result = await tool.execute("rm -rf tmp")

    assert "Exit code: 0" in result
    assert responder.requests == []
    assert executor.commands == ["rm -rf tmp"]
