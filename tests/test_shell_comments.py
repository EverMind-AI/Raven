"""A comment is text the shell will not run, and must not decide safety.

The policy turns `#` off in its lexer, so an English comment keeps taking part
in quote parsing. Three read-only commands were refused in one session because
their comments said `there's`, `what's` and `raven's`: the apostrophe opened a
quote that never closed, `shlex` raised, and the fail-closed branch answered
`hard_deny`. The user could only see "policy evaluation failed", asked four
times why, and the model -- given nothing to go on -- guessed wrong twice.

Fail-closed is right and stays: an executable region nobody can parse is still
refused. What changes is that the region the shell would ignore stops being
read as if it would run.

Both gates are tested here, but they no longer answer the same question. The
deny list has one owner now; what `ExecTool._guard_command` still decides on
its own is the allowlist and the workspace boundary. A comment could reach
both -- so those are what it is asked about here, rather than a second deny
read that no longer exists.
"""

from __future__ import annotations

import pytest

from raven.agent.tools.shell import ExecTool
from raven.agent.tools.shell_policy import CommandDecision, ShellCommandPolicy


@pytest.fixture
def policy() -> ShellCommandPolicy:
    return ShellCommandPolicy(deny_patterns=[r"\b(mkfs|diskpart)\b", r"\bdd\s+if="])


@pytest.fixture
def tool(tmp_path) -> ExecTool:
    """The same deny list the policy fixture carries, on the other gate."""
    return ExecTool(
        working_dir=str(tmp_path),
        deny_patterns=[r"\b(mkfs|diskpart)\b", r"\bdd\s+if="],
        restrict_to_workspace=False,
    )


@pytest.fixture
def allowlisted(tmp_path) -> ExecTool:
    """The other one. `\\bls\\b` matches inside English prose on purpose: that is
    the whole question -- whether a command can be talked onto the allowlist by
    text the shell will not run."""
    return ExecTool(
        working_dir=str(tmp_path),
        allow_patterns=[r"\bls\b"],
        restrict_to_workspace=False,
    )


# ---------- the session, verbatim --------------------------------------------

#: The three commands from the incident, each with the apostrophe that broke it.
INTERCEPTED = [
    (
        "there's",
        "# Check if there's a raven-code specific config somewhere\n"
        'find /tmp/state -name "*.json" -path "*code*" -not -path "*/traces/*" 2>/dev/null | head -20',
    ),
    (
        "what's",
        "# Check what's listening on common ports\n"
        'curl -s -o /dev/null -w "%{http_code}" http://localhost:18791/ 2>/dev/null; echo " :18791"',
    ),
    (
        "raven's",
        "# Check how the main raven's exec tool gets initialized - look at the tool registry\n"
        'grep -n "ExecTool" /tmp/raven/tool_index.py 2>/dev/null | head -20',
    ),
]


@pytest.mark.parametrize(("name", "command"), INTERCEPTED, ids=[n for n, _ in INTERCEPTED])
def test_a_read_only_command_survives_its_own_comment(name: str, command: str, policy: ShellCommandPolicy) -> None:
    """Each of the three, and the control the ledger ran beside them: the same
    command without the comment was allowed every time."""
    assert policy.evaluate(command) is CommandDecision.ALLOW
    assert policy.evaluate(command.split("\n", 1)[1]) is CommandDecision.ALLOW


# ---------- what a comment must not be able to do ----------------------------


def test_a_comment_cannot_smuggle_a_denied_pattern_into_a_refusal(policy: ShellCommandPolicy) -> None:
    """The other direction of the same fault: text the shell ignores deciding
    that a safe command is dangerous."""
    assert policy.evaluate("ls -la  # unlike dd if=/dev/zero, this one only lists") is CommandDecision.ALLOW


async def test_the_real_thing_is_still_denied(tool: ExecTool) -> None:
    """Asserted through `execute` rather than at a named gate.

    The deny list used to be read twice -- once in `_guard_command`, once in the
    policy -- and this pinned the first. It now has one owner, so the question
    worth asking is whether the command is refused, not which of two places
    refused it.
    """
    result = await tool.execute(command="dd if=/dev/zero of=/tmp/x")

    assert not result.ok
    assert "blocked by safety guard" in result.model_text


def test_a_denied_pattern_outside_the_comment_still_denies(policy: ShellCommandPolicy) -> None:
    assert policy.evaluate("dd if=/dev/zero of=/tmp/x  # make a file") is CommandDecision.HARD_DENY


@pytest.mark.parametrize(
    "command",
    [
        "# tidy up\nrm -rf /tmp/tree",
        "rm -rf /tmp/tree  # tidy up",
        "# shutdown notes\nshutdown -h now",
        "shutdown -h now  # going down",
    ],
)
def test_a_comment_does_not_excuse_the_command_beside_it(command: str, policy: ShellCommandPolicy) -> None:
    """The direction that matters most. Stripping comments must not become a
    way to hide the executable half from the classifier."""
    assert policy.evaluate(command) is CommandDecision.HARD_DENY


def test_a_dangerous_command_inside_a_comment_is_not_run_and_not_refused(policy: ShellCommandPolicy) -> None:
    """`rm -rf /` written in a comment is a sentence about `rm -rf /`."""
    assert policy.evaluate("ls  # never run rm -rf / on a live host") is CommandDecision.ALLOW


# ---------- what is not a comment --------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "grep -n 'foo#bar' file.txt",
        'echo "a # b"',
        "grep -rn '#!/bin/sh' scripts/",
        "echo ${#PATH}",
        "echo $#",
        "git show HEAD:file#1 2>/dev/null",
    ],
)
def test_a_hash_that_is_not_a_comment_is_left_alone(command: str, policy: ShellCommandPolicy) -> None:
    """A `#` starts a comment only at the start of a word and outside quotes.
    Cutting on every `#` would silently shorten commands, which is how a
    classifier stops seeing the operative half."""
    assert policy.evaluate(command) is CommandDecision.ALLOW


@pytest.mark.parametrize(
    "command",
    [
        # Checked against real bash, not from memory:
        #   $ bash -c 'echo X$(echo b)#; echo PWNED'  ->  Xb#  then  PWNED
        #   $ bash -c 'echo X`echo b`#; echo PWNED'   ->  Xb#  then  PWNED
        # The `#` stays part of the word and the `;` still separates a command,
        # so the delete after it runs.
        "echo $(echo b)#; rm -rf /tmp/tree",
        "echo `echo b`#; rm -rf /tmp/tree",
    ],
    ids=["command-substitution", "backtick"],
)
def test_a_hash_after_a_substitution_does_not_open_a_comment(command: str, policy: ShellCommandPolicy) -> None:
    """The comment-open set is not the operator set.

    `)` and a backtick end an operator's reach but not a word, so cutting there
    dropped the rest of the line out of every check while the shell went on
    running it. Both of these are `hard_deny` on `main`.
    """
    assert policy.evaluate(command) is CommandDecision.HARD_DENY


def test_a_hash_glued_to_a_word_does_not_hide_what_follows(policy: ShellCommandPolicy) -> None:
    """The adversarial version of the case above: if `foo#` were read as a
    comment, everything after it would leave the classifier's view."""
    assert policy.evaluate("echo a#b; rm -rf /tmp/tree") is CommandDecision.HARD_DENY


# ---------- fail-closed, still --------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "echo 'unterminated",
        'grep "no closing quote /tmp',
        "# a comment\necho 'still unterminated",
    ],
)
def test_an_executable_region_that_cannot_be_parsed_is_still_refused(command: str, policy: ShellCommandPolicy) -> None:
    """The invariant that must survive the fix. The last case is the pointed
    one: a well-formed comment does not license a broken command after it."""
    assert policy.evaluate(command) is CommandDecision.HARD_DENY


def test_a_wrapper_cannot_launder_a_denied_command_through_a_comment(policy: ShellCommandPolicy) -> None:
    """`bash -c` carries its own script, comments and all."""
    assert policy.evaluate("bash -c '# tidy\nrm -rf /tmp/tree'") is CommandDecision.HARD_DENY


# ---------- one command, one classification ----------------------------------


def test_the_approval_prompt_describes_the_command_not_the_comment(policy: ShellCommandPolicy) -> None:
    """`evaluate` and `approval_reason` answer about the same text.

    Read from the raw command, a comment mentioning a recursive delete tripped
    the short-circuit that returns no reason at all -- so the user was asked to
    approve a deletion under the generic "needs your approval" line, decided
    entirely by prose the shell will not run.
    """
    commented = "rm /tmp/x  # careful, not rm -rf /"

    assert policy.evaluate(commented) is CommandDecision.REQUIRE_APPROVAL
    assert policy.approval_reason(commented) == policy.approval_reason("rm /tmp/x") == "delete_command"


# ---------- what the guard still decides on its own -------------------------


@pytest.mark.parametrize(
    "command",
    ["ls -la  # see ../notes for why", "ls -la  # config lives in /etc/raven"],
    ids=["traversal", "absolute"],
)
def test_a_comment_is_not_a_path_the_command_reaches_for(command: str, tmp_path) -> None:
    """Same shape as the incident: a read-only command refused for English
    written beside it, and a message just as opaque about why."""
    fenced = ExecTool(working_dir=str(tmp_path), deny_patterns=[], restrict_to_workspace=True)

    assert fenced._guard_command(command, cwd=str(tmp_path)) is None


@pytest.mark.parametrize(
    "command",
    ["ls ../outside", "cat /etc/passwd"],
    ids=["traversal", "absolute"],
)
def test_a_real_path_outside_the_workspace_is_still_refused(command: str, tmp_path) -> None:
    fenced = ExecTool(working_dir=str(tmp_path), deny_patterns=[], restrict_to_workspace=True)

    assert fenced._guard_command(command, cwd=str(tmp_path)) is not None


def test_a_comment_cannot_talk_a_command_onto_the_allowlist(allowlisted: ExecTool, tmp_path) -> None:
    """The guard's other half, and the one that costs something if it is wrong.

    A surface with an allowlist runs only what it named, so text the shell
    discards satisfying that list is a command running which was never allowed
    -- the opposite direction from the incident, and the reason this gate reads
    the executable view rather than being handed a stripped string.
    """
    refusal = allowlisted._guard_command("cat /etc/passwd  # this is basically ls", cwd=str(tmp_path))

    assert "not in allowlist" in (refusal or "")
    assert allowlisted._guard_command("ls -la  # unlike cat, this only lists", cwd=str(tmp_path)) is None
