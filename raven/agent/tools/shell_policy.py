"""Classify shell commands before execution.

The policy deliberately operates on recognizable shell syntax, not on the
eventual effects of arbitrary programs. It identifies command families Raven
can classify reliably, including commands hidden behind common wrappers, while
the runtime sandbox remains responsible for its separate containment boundary.

Classification order is security-sensitive: hard-denied commands must never be
downgraded into approval requests, and matcher failures fail closed instead of
silently permitting execution.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Iterator
from enum import StrEnum
from pathlib import PurePath

ApprovalMatcher = Callable[[str], bool]

_WRAPPER_OPTIONS_WITH_VALUE = {
    "command": frozenset(),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "nohup": frozenset(),
    "sudo": frozenset(
        {
            "-C",
            "--close-from",
            "-D",
            "--chdir",
            "-g",
            "--group",
            "-h",
            "--host",
            "-p",
            "--prompt",
            "-r",
            "--role",
            "-t",
            "--type",
            "-T",
            "--command-timeout",
            "-u",
            "--user",
        }
    ),
}
# Programs whose own arguments are another command to run. These are not
# wrappers in the `_unwrap_command_wrappers` sense -- `xargs rm` runs `rm` once
# per input line rather than becoming it -- but the command they carry has to
# be classified, or `xargs rm -rf` and `timeout 5 rm -rf` land on the opposite
# side of the policy from the bare `rm -rf` they are.
_COMMAND_RUNNERS: dict[str, frozenset[str]] = {
    "ionice": frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "setsid": frozenset(),
    "stdbuf": frozenset({"-e", "--error", "-i", "--input", "-o", "--output"}),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "xargs": frozenset(
        {
            "-a",
            "--arg-file",
            "-d",
            "--delimiter",
            "-E",
            "-I",
            "-i",
            "--replace",
            "-L",
            "-l",
            "--max-lines",
            "-n",
            "--max-args",
            "-P",
            "--max-procs",
            "-s",
            "--max-chars",
        }
    ),
}
# `timeout` alone takes a positional before the command it runs.
_TIMEOUT_DURATION = re.compile(r"[0-9]+(?:\.[0-9]+)?[smhd]?")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_COMMAND_BOUNDARIES = frozenset(";&|\n(){}`")
_SHELL_COMMAND_WRAPPERS = frozenset({"bash", "dash", "ksh", "sh", "zsh"})
_SYSTEM_POWER_COMMANDS = frozenset({"halt", "poweroff", "reboot", "shutdown"})
_POWER_MULTIPLEXERS = frozenset({"busybox", "init", "loginctl", "systemctl", "telinit"})
_POWER_MULTIPLEXER_ACTIONS = _SYSTEM_POWER_COMMANDS | {"0", "6"}
_MAX_EMBEDDED_SHELL_DEPTH = 4


class CommandDecision(StrEnum):
    """Policy outcomes ordered from ordinary execution to terminal rejection."""

    ALLOW = "allow"
    HARD_DENY = "hard_deny"
    REQUIRE_APPROVAL = "require_approval"


def _command_segments(command: str) -> Iterator[list[str]]:
    """Yield compound shell commands as independently classified token lists.

    This conservative lexical split catches deletion in common sequence,
    conditional, and pipeline forms without pretending to evaluate expansions
    or reproduce the full shell grammar.
    """

    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|(){}`\n")
    lexer.commenters = ""
    # Newlines must remain visible as command boundaries. Quoted newlines are
    # still returned inside their quoted token and therefore do not split it.
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    segment: list[str] = []
    for token in lexer:
        if token and all(char in _COMMAND_BOUNDARIES for char in token):
            if segment:
                yield segment
                segment = []
            continue
        segment.append(token)
    if segment:
        yield segment


def _embedded_shell_command(segment: list[str]) -> str | None:
    """Return the command string supplied to a recognized shell ``-c``."""

    if not segment or PurePath(segment[0]).name not in _SHELL_COMMAND_WRAPPERS:
        return None
    for index, token in enumerate(segment[1:], start=1):
        if not token.startswith("-") or token.startswith("--"):
            continue
        command_option = token.find("c", 1)
        if command_option == -1:
            continue
        if command_option + 1 < len(token):
            return token[command_option + 1 :]
        if index + 1 < len(segment):
            return segment[index + 1]
    return None


def _runner_inner_command(segment: list[str]) -> str | None:
    """Return the command a recognized command-runner was handed, if any.

    Option values are consumed so the command position is found rather than
    guessed; an unrecognized option shape ends the scan, which leaves a token
    that is not an executable in front and matches nothing.
    """

    if not segment:
        return None
    options_with_value = _COMMAND_RUNNERS.get(PurePath(segment[0]).name)
    if options_with_value is None:
        return None
    tokens = segment[1:]
    while tokens and tokens[0].startswith("-") and tokens[0] != "-":
        option = tokens.pop(0)
        if option == "--":
            break
        # A value attached to its option (`-n5`, `--max-args=5`) is already
        # consumed; only a separate one has to be stepped over.
        if "=" in option or (not option.startswith("--") and len(option) > 2):
            continue
        if option in options_with_value and tokens:
            tokens.pop(0)
    if PurePath(segment[0]).name == "timeout" and tokens and _TIMEOUT_DURATION.fullmatch(tokens[0]):
        tokens = tokens[1:]
    return shlex.join(tokens) if tokens else None


def _matches_delete_command(command: str, *, _depth: int = 0) -> bool:
    """Recognize direct file-deletion commands after wrapper normalization."""

    for segment in _command_segments(command):
        segment = _unwrap_command_wrappers(segment)
        if not segment:
            continue
        executable = PurePath(segment[0]).name
        if executable in {"rm", "unlink"}:
            return True
        if executable == "find":
            if "-delete" in segment[1:]:
                return True
            for index, token in enumerate(segment[1:], start=1):
                if token not in {"-exec", "-execdir"}:
                    continue
                executed = _unwrap_command_wrappers(segment[index + 1 :])
                if not executed:
                    continue
                if PurePath(executed[0]).name in {"rm", "unlink"}:
                    return True
                embedded_exec = _embedded_shell_command(executed)
                if (
                    embedded_exec is not None
                    and _depth < _MAX_EMBEDDED_SHELL_DEPTH
                    and _matches_delete_command(embedded_exec, _depth=_depth + 1)
                ):
                    return True
        for nested in (_embedded_shell_command(segment), _runner_inner_command(segment)):
            if (
                nested is not None
                and _depth < _MAX_EMBEDDED_SHELL_DEPTH
                and _matches_delete_command(nested, _depth=_depth + 1)
            ):
                return True
    return False


def _is_recursive_delete(argv: list[str]) -> bool:
    """True when an ``rm`` argv carries a recursive flag.

    ``rm -rf`` walks a tree it was never shown; ``rm -f a.py b.json`` removes
    exactly the files it names. Only the first is unconditional, which is why
    the flags are read from tokens rather than matched in the raw string: a
    regexp anchored right after ``rm`` misses ``rm -f -r`` and a looser one
    matches the ``-f`` that belongs to a different family entirely.
    """

    for arg in argv[1:]:
        if arg == "--":
            return False
        if arg in {"--recursive", "--dir", "-d"}:
            return True
        if arg.startswith("--") or not arg.startswith("-"):
            continue
        if "r" in arg[1:] or "R" in arg[1:]:
            return True
    return False


def _matches_recursive_delete(command: str, *, _depth: int = 0) -> bool:
    """Recognize recursive deletion, the one delete no approval can rescue.

    Reach has to match `_matches_delete_command` exactly. Anything this misses
    that the other catches is not merely unclassified: it is downgraded from a
    refusal into a prompt, and the reader is asked to approve the one command
    the policy exists to refuse.
    """

    for segment in _command_segments(command):
        segment = _unwrap_command_wrappers(segment)
        if not segment:
            continue
        executable = PurePath(segment[0]).name
        if executable == "rm" and _is_recursive_delete(segment):
            return True
        if executable == "find":
            for index, token in enumerate(segment[1:], start=1):
                if token not in {"-exec", "-execdir"}:
                    continue
                executed = _unwrap_command_wrappers(segment[index + 1 :])
                if not executed:
                    continue
                if PurePath(executed[0]).name == "rm" and _is_recursive_delete(executed):
                    return True
                embedded_exec = _embedded_shell_command(executed)
                if (
                    embedded_exec is not None
                    and _depth < _MAX_EMBEDDED_SHELL_DEPTH
                    and _matches_recursive_delete(embedded_exec, _depth=_depth + 1)
                ):
                    return True
        for nested in (_embedded_shell_command(segment), _runner_inner_command(segment)):
            if (
                nested is not None
                and _depth < _MAX_EMBEDDED_SHELL_DEPTH
                and _matches_recursive_delete(nested, _depth=_depth + 1)
            ):
                return True
    return False


def _matches_system_power_command(command: str, *, _depth: int = 0) -> bool:
    """Recognize power-control executables without matching argument text."""

    for segment in _command_segments(command):
        segment = _unwrap_command_wrappers(segment)
        if not segment:
            continue
        executable = PurePath(segment[0]).name
        if executable in _SYSTEM_POWER_COMMANDS:
            return True
        if executable in _POWER_MULTIPLEXERS and any(arg in _POWER_MULTIPLEXER_ACTIONS for arg in segment[1:]):
            return True
        embedded = _embedded_shell_command(segment)
        if (
            embedded is not None
            and _depth < _MAX_EMBEDDED_SHELL_DEPTH
            and _matches_system_power_command(embedded, _depth=_depth + 1)
        ):
            return True
    return False


def _unwrap_command_wrappers(segment: list[str]) -> list[str]:
    """Expose an executable hidden behind assignments, env, sudo, or command.

    Normalizing well-known wrappers prevents trivial approval bypasses. Unknown
    executables and option shapes remain untouched rather than being guessed at.
    """

    tokens = list(segment)
    while tokens:
        while tokens and _ASSIGNMENT.fullmatch(tokens[0]):
            tokens.pop(0)
        if not tokens:
            return tokens
        wrapper = PurePath(tokens[0]).name
        options_with_value = _WRAPPER_OPTIONS_WITH_VALUE.get(wrapper)
        if options_with_value is None:
            return tokens
        tokens = tokens[1:]
        while tokens and tokens[0].startswith("-"):
            option = tokens.pop(0)
            if option == "--":
                break
            option_name = option.split("=", 1)[0]
            if option_name in options_with_value and "=" not in option and tokens:
                tokens.pop(0)
        if wrapper == "env":
            while tokens and "=" in tokens[0] and not tokens[0].startswith("="):
                tokens = tokens[1:]
    return tokens


class ShellCommandPolicy:
    """Apply hard-deny and approval rules in their required precedence order."""

    def __init__(self, *, deny_patterns: list[str]) -> None:
        # Compile once because every direct shell execution crosses this policy.
        self._deny_patterns = tuple(re.compile(pattern, re.IGNORECASE) for pattern in deny_patterns)
        self._approval_matchers: list[tuple[str, ApprovalMatcher]] = [("delete_command", _matches_delete_command)]

    def register_approval_matcher(self, name: str, matcher: ApprovalMatcher) -> None:
        """Extend approval classification with a named command-family matcher."""

        self._approval_matchers.append((name, matcher))

    def evaluate(self, command: str) -> CommandDecision:
        """Classify a command, reducing authority when a matcher cannot decide."""

        # Hard deny runs first so an approval matcher can never convert an
        # unconditionally forbidden command into an approvable operation.
        if any(pattern.search(command) for pattern in self._deny_patterns):
            return CommandDecision.HARD_DENY
        try:
            if _matches_recursive_delete(command):
                return CommandDecision.HARD_DENY
            if _matches_system_power_command(command):
                return CommandDecision.HARD_DENY
            if any(matcher(command) for _, matcher in self._approval_matchers):
                return CommandDecision.REQUIRE_APPROVAL
        except Exception:
            # Matchers inspect untrusted command text and may be extended later.
            # A faulty matcher must close the gate, not bypass it.
            return CommandDecision.HARD_DENY
        return CommandDecision.ALLOW


__all__ = ["ApprovalMatcher", "CommandDecision", "ShellCommandPolicy"]
