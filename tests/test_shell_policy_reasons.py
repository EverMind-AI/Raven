"""A refusal carries the reason it happened.

Four different causes -- a denied pattern, a recursive delete, a power-off, a
command that cannot be parsed -- all reached the user as one sentence:
"Command blocked by safety guard (policy evaluation failed)". In the session
this work came from, that is what the user asked about four times, and what the
model then guessed wrong about twice.

The decision itself is unchanged here. What changes is that the rule which
fired travels with it, so the tool result, and later the trace and the UI, stop
having to infer a cause from a generic string.
"""

from __future__ import annotations

import pytest

from raven.agent.tools.shell import ExecTool
from raven.agent.tools.shell_policy import CommandDecision, ShellCommandPolicy


@pytest.fixture
def policy() -> ShellCommandPolicy:
    return ShellCommandPolicy(deny_patterns=[r"\b(mkfs|diskpart)\b"])


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("mkfs.ext4 /dev/sdb1", "deny_pattern"),
        ("rm -rf /tmp/tree", "recursive_delete"),
        ("shutdown -h now", "system_power"),
        ("echo 'unterminated", "parse_error"),
    ],
)
def test_a_refusal_names_the_rule_that_refused_it(command: str, reason: str, policy: ShellCommandPolicy) -> None:
    """Each cause is its own answer, not four spellings of one."""
    outcome = policy.classify(command)

    assert outcome.decision is CommandDecision.HARD_DENY
    assert outcome.reason_code == reason


def test_an_allowed_command_has_no_reason_to_give(policy: ShellCommandPolicy) -> None:
    outcome = policy.classify("ls -la")

    assert outcome.decision is CommandDecision.ALLOW
    assert outcome.reason_code == ""


def test_an_approval_names_its_family(policy: ShellCommandPolicy) -> None:
    """The family is what the prompt describes, so it is the reason code here
    rather than a separate lookup that could disagree with the decision."""
    outcome = policy.classify("rm /tmp/x")

    assert outcome.decision is CommandDecision.REQUIRE_APPROVAL
    assert outcome.reason_code == "delete_command"


def test_the_old_answers_are_the_new_one_read_two_ways(policy: ShellCommandPolicy) -> None:
    """`evaluate` and `approval_reason` keep their signatures and answer from
    the same classification, so the two cannot disagree about one command --
    which they could while each re-ran the matchers itself.
    """
    for command in ("ls -la", "rm /tmp/x", "rm -rf /tmp/tree", "mkfs.ext4 /dev/sdb1", "echo 'unterminated"):
        outcome = policy.classify(command)

        assert policy.evaluate(command) is outcome.decision, command
        expected = outcome.reason_code if outcome.decision is CommandDecision.REQUIRE_APPROVAL else None
        assert policy.approval_reason(command) == expected, command


def test_a_comment_still_cannot_choose_the_reason(policy: ShellCommandPolicy) -> None:
    """The lexical view applies to the reason as well as to the decision."""
    assert policy.classify("ls -la  # mkfs is not being run here").reason_code == ""
    assert policy.classify("rm /tmp/x  # careful, not rm -rf /").reason_code == "delete_command"


# ---------- what the reader is actually told ---------------------------------


@pytest.fixture
def tool(tmp_path) -> ExecTool:
    return ExecTool(working_dir=str(tmp_path), deny_patterns=[r"\b(mkfs|diskpart)\b"])


@pytest.mark.parametrize(
    ("command", "phrase"),
    [
        ("mkfs.ext4 /dev/sdb1", "denied pattern"),
        ("rm -rf /tmp/tree", "recursively"),
        ("shutdown -h now", "powers the machine off"),
        ("echo 'unterminated", "could not be parsed"),
    ],
)
async def test_the_refusal_the_user_reads_says_which_rule_fired(command, phrase, tool: ExecTool) -> None:
    """The four causes used to be one sentence. Each is now its own, and each
    names something its reader can act on -- an operator list to edit, or a
    quote to close."""
    result = await tool.execute(command=command)

    assert not result.ok
    assert phrase in result.model_text, result.model_text
    assert "policy evaluation failed" not in result.model_text


async def test_an_unknown_reason_falls_back_rather_than_guessing(tool: ExecTool, monkeypatch) -> None:
    """A refusal from a rule this map does not know about says less rather than
    something wrong -- the same stance `_APPROVAL_DESCRIPTIONS` takes."""
    from raven.agent.tools import shell_policy

    monkeypatch.setattr(
        tool._policy,
        "classify",
        lambda *a, **kw: shell_policy.PolicyOutcome(CommandDecision.HARD_DENY, "a_rule_from_the_future"),
    )

    result = await tool.execute(command="ls")

    assert "Command blocked by safety guard" in result.model_text
    assert "a_rule_from_the_future" not in result.model_text
