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
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)


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

    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.commenters = ""
    lexer.whitespace_split = True
    segment: list[str] = []
    for token in lexer:
        if token and all(char in ";&|" for char in token):
            if segment:
                yield segment
                segment = []
            continue
        segment.append(token)
    if segment:
        yield segment


def _matches_delete_command(command: str) -> bool:
    """Recognize direct file-deletion commands after wrapper normalization."""

    for segment in _command_segments(command):
        segment = _unwrap_command_wrappers(segment)
        if not segment:
            continue
        executable = PurePath(segment[0]).name
        if executable in {"rm", "unlink"}:
            return True
        if executable == "find" and "-delete" in segment[1:]:
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
            if any(matcher(command) for _, matcher in self._approval_matchers):
                return CommandDecision.REQUIRE_APPROVAL
        except Exception:
            # Matchers inspect untrusted command text and may be extended later.
            # A faulty matcher must close the gate, not bypass it.
            return CommandDecision.HARD_DENY
        return CommandDecision.ALLOW


__all__ = ["ApprovalMatcher", "CommandDecision", "ShellCommandPolicy"]
