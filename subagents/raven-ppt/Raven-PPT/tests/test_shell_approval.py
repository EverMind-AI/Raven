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


@pytest.mark.parametrize("command", ["echo 'unterminated", "echo trailing\\", "echo it's fine"])
def test_a_command_shlex_cannot_read_is_still_classified(
    policy: ShellCommandPolicy, command: str
) -> None:
    """This used to deny outright, and the denial was the expensive half.

    `shlex` raises "No closing quotation" for any command whose quotes do not
    balance as shell words -- which an English contraction inside a heredoc or a
    comment does -- so `echo it's fine` was refused. Three measured runs died on
    it: two with eighteen of twenty pages unwritten, and one that spent 43
    minutes and 7.15M tokens before a `python3 - <<EOF` whose comment read
    "labels don't collide" was blocked, answered "no alternative method will be
    attempted", and stopped.

    Classifying it is safe because `_bare_segments` removes the quote and escape
    characters before splitting, which can only join what the shell would have
    joined and never separates a token -- see the two tests below for both
    halves of that claim.
    """
    assert policy.evaluate(command) is CommandDecision.ALLOW


def test_the_fallback_resolves_the_concatenation_a_whitespace_split_would_miss(
    policy: ShellCommandPolicy,
) -> None:
    """The reason quotes are stripped rather than treated as separators.

    `"r""m"` is one word to the shell and two to a naive split, so a fallback
    that merely split on whitespace would let a disguised delete through -- which
    is a weaker gate than the one being replaced, and the whole point is that
    this one is not.

    Every case here has to reach the fallback, which means its quotes must not
    balance -- `"r""m" -rf /` on its own balances, `shlex` reads it, and the test
    would pass while proving nothing about the fallback at all. The trailing
    apostrophe is what sends each of these down the other path.
    """
    for command in ('"r""m" unlink /tmp/it\'s', "r'm' -rf /tmp/it\'s", 'unl"i"nk /tmp/it\'s'):
        assert policy.evaluate(command) is not CommandDecision.ALLOW, command


def test_the_fallback_errs_towards_classifying_too_much(policy: ShellCommandPolicy) -> None:
    """Boundaries come off the raw characters, so a separator inside a quote splits too.

    Only where the fallback is actually taken: `echo "; unlink x"` balances its
    quotes, so `shlex` reads it as one argument and the leading token is `echo`.
    Leave one quote open and the same text is classified from the bare split,
    where `unlink` leads a segment and the command goes for approval although it
    only prints that text. Over-classification costs an approval;
    under-classification costs the gate.

    `unlink` rather than `rm -rf`, because this fixture's deny patterns match the
    latter in the raw string and would answer before any segmentation runs.
    """
    assert policy.evaluate('echo "; unlink x"') is CommandDecision.ALLOW, "shlex reads this one"
    assert policy.evaluate('echo "; unlink x') is CommandDecision.REQUIRE_APPROVAL


def test_a_faulty_matcher_still_denies(policy: ShellCommandPolicy) -> None:
    """Unchanged, and the distinction the change rests on: text this module cannot
    lex is a fact about the text, while a matcher that raises is a bug in the gate
    itself and there is nothing to fall back to."""

    def broken(command: str) -> bool:
        raise RuntimeError("broken")

    policy.register_approval_matcher("broken", broken)

    assert policy.evaluate("echo harmless") is CommandDecision.HARD_DENY


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
    # Not an abort. A denial is something to respect rather than route around, and
    # `abort_action` ends the turn so the next plan cannot restate a rejected
    # operation as a script. An absent approval channel is not a denial: nobody
    # refused anything, every command needing approval meets the same wall, and
    # ending the turn there cost two headless runs eighteen of their twenty pages.
    assert result.abort_action is False
    assert "nobody to ask" in result.model_text
    assert "Carry on with the rest of the task" in result.model_text
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


def test_a_refusal_names_the_path_that_was_outside(tmp_path) -> None:
    """One refusal covers a whole command, so it has to say which part earned it.

    A live run wrote `ls <workspace>/figures/ && ls /tmp/`: the first half was
    inside the fence and the second was not, the whole call came back as "a path
    outside working dir", and with nothing naming which path the run could not
    repair it. It ended by asking a user who was not there whether to continue,
    and published no deck.
    """
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)

    refusal = tool._guard_command(f"ls {tmp_path}/figures/ && ls /etc/", str(tmp_path))

    assert refusal is not None
    assert "/etc" in refusal
    assert str(tmp_path) in refusal
    assert tool._guard_command(f"ls {tmp_path}/figures/", str(tmp_path)) is None
