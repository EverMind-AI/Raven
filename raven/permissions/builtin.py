"""The platform's own rulings: what ships with the code and no config lifts.

Two rulings, in the order the waterfall consumes them. The unconditional deny
list holds catastrophe-class commands only -- disk devices, fork bombs, power
control, and a recursive delete aimed at the filesystem root or the home tree;
it outranks everything including a user allow rule, in every mode. The
command families the running surface declared via
``shell_policy.set_surface_approval_families`` (none by default; the ACP editor
declares deletion and the external-effect families) do not decide anything:
they name the prompt -- "Publish or push work to a remote" rather than the bare
command -- when the tiers land the call on one. Every mutation, deletes
included, answers to the permission tiers.

Both rulings read shell commands only. Classification is
``ShellCommandPolicy``'s -- lexical segmentation, wrapper stripping, embedded
shells, fail-closed on matcher errors -- reused in place rather than moved, so
its tests and its surface-registration hook keep their one home.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from hashlib import sha256
from typing import Any

from loguru import logger

from raven.contracts.permissions import DecisionSource, Deny, NeedsApproval
from raven.permissions.shell_policy import CommandDecision, ShellCommandPolicy

# Windows del/rmdir are absent here on purpose too: an ordinary del /f or
# rmdir /s is daily cleanup and answers to the tiers, so only a recursive
# Windows delete of a drive root or the user profile is refused, by the
# target-sensitive matcher in shell_policy (the same stance as `rm -rf /` vs
# `rm -rf build`). `rm` is absent here on purpose: the policy classifies it from tokens
# (`_matches_recursive_delete` hard-denies a recursive one, everything else
# goes to approval). A regexp here cannot tell `rm -rf /` from
# `rm -f a.py b.json`, and denying both means the agent cannot clean up
# after itself -- with no prompt offered, because hard deny outranks approval.
BUILTIN_DENY_PATTERNS: tuple[str, ...] = (
    r"(?:^|[;&|]\s*)format\b",  # format (as standalone command only)
    r"\b(mkfs|diskpart)\b",  # disk operations
    r"\bdd\s+if=",  # dd
    r">\s*/dev/sd",  # write to disk
    r":\(\)\s*\{.*\};\s*:",  # fork bomb
)

# Why a command was refused, in the words the reader needs. Each line names the
# rule and where it lives, because these refusals are acted on: a deny pattern
# is the operator's list to edit, a parse error is the command's own to fix.
# Absent from this map, the generic text stands: a message that names the wrong
# rule is worse than one that names none.
_DENY_REASONS: dict[str, str] = {
    "deny_pattern": "matches a denied pattern (tools.exec.extraDenyPatterns, plus the built-in list)",
    "catastrophic_delete": "recursively deletes the filesystem root or the home directory",
    "system_power": "powers the machine off or reboots it",
    "parse_error": "could not be parsed as a shell command; an unbalanced quote is the usual cause",
}

# What the reader is being asked about, per family. An unlisted family falls
# back to deliberately vague text rather than a guess, for the reason the map
# above gives.
_APPROVAL_DESCRIPTIONS: dict[str, str] = {
    "delete_command": "Delete files using a shell command",
    "publish_command": "Publish or push work to a remote",
    "install_command": "Install software, which runs code from the network",
    "remote_exec_command": "Run a command on, or copy files to, another machine",
    "credential_command": "Read or change stored credentials",
    "destructive_vcs_command": "Discard uncommitted work in this repository",
    "fetch_side_effect": "Download to a file, upload data, or run what it downloads",
}


def _exec_machine(params: dict[str, Any]) -> str:
    """The registered machine an exec call names, or "" for this computer.

    Where a command runs changes what it puts at risk, so the machine is part
    of the action: the human reads it on the prompt, and a refusal for one
    machine does not pre-refuse the same command on another.
    """
    machine = params.get("machine")
    return machine.strip() if isinstance(machine, str) else ""


def action_digest(tool_name: str, params: dict[str, Any]) -> str:
    """The per-turn dedup key for one exact call."""
    if tool_name == "exec" and isinstance(params.get("command"), str):
        material = params["command"]
        if machine := _exec_machine(params):
            material = f"{material}\x00on:{machine}"
    else:
        material = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    return sha256(f"{tool_name}\x00{material}".encode()).hexdigest()


def session_keys(tool_name: str, params: dict[str, Any], ask_segments: tuple[str, ...] = ()) -> tuple[str, ...]:
    """What a "for this session" grant remembers, one key per part still asking.

    For exec that is each segment no rule covers, keyed with the machine and
    the directory it runs in -- the same command text in another directory is
    another action, so a grant never travels; the directory is the one the
    call named, else the turn's binding, else the construction-time default
    (stable for a conversation, so leaving it empty still distinguishes). The
    two builtin file writers are keyed by path and directory: the content
    changes every call, the path is what the human looked at, and a relative
    path names another file once the directory is rebound. Every other tool
    keys the exact call -- a `path` field on an unknown tool says nothing about
    what its other fields do.
    """
    if tool_name == "exec" and ask_segments:
        working_dir = params.get("working_dir")
        cwd = (working_dir.strip() if isinstance(working_dir, str) else "") or _bound_workdir()
        machine = _exec_machine(params)
        return tuple(
            sha256(f"exec\x00{segment}\x00cwd:{cwd}\x00on:{machine}".encode()).hexdigest() for segment in ask_segments
        )
    path = params.get("path")
    if tool_name in PATH_KEYED_TOOLS and isinstance(path, str) and path.strip():
        material = f"{tool_name}\x00path:{os.path.normpath(path.strip())}\x00cwd:{_bound_workdir()}"
        return (sha256(material.encode()).hexdigest(),)
    return (action_digest(tool_name, params),)


PATH_KEYED_TOOLS: frozenset[str] = frozenset({"edit_file", "write_file"})


def _bound_workdir() -> str:
    from raven.agent.workdir import current as current_workdir

    return str(current_workdir() or "")


def action_line(tool_name: str, params: dict[str, Any]) -> str:
    """The action as a human should read it in an approval prompt."""
    if tool_name == "exec" and isinstance(params.get("command"), str):
        if machine := _exec_machine(params):
            return f"{params['command']} (on {machine})"
        return params["command"]
    compact = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    if len(compact) > 200:
        compact = compact[:200] + "..."
    return f"{tool_name} {compact}"


class BuiltinRulings:
    """Both platform rulings over one call, answered in one classification."""

    def __init__(
        self,
        *,
        extra_deny_patterns: list[str] | None = None,
        extra_deny_source: Callable[[], list[str] | None] | None = None,
    ) -> None:
        self._extra_deny_current = list(extra_deny_patterns or [])
        self._extra_deny_source = extra_deny_source
        self._policy = ShellCommandPolicy(deny_patterns=list(BUILTIN_DENY_PATTERNS) + self._extra_deny_current)

    def _refresh_deny_patterns(self) -> None:
        """Track the operator's extra deny list as it stands on disk.

        Runs before every classification, not once per turn: tightening a
        permission must bind the tool call that is about to run, and loosening
        works the same way -- the list is the operator's own choice in both
        directions. A reader answering ``None`` means "no live answer" and
        keeps the current extras; a pattern that does not compile rejects the
        whole edit and keeps the current policy (``set_deny_patterns`` swaps
        the deny set in place, so registered approval families survive).
        """
        if self._extra_deny_source is None:
            return
        try:
            extras = self._extra_deny_source()
        except Exception:
            return
        if extras is None:
            return
        extras = [str(pattern) for pattern in extras]
        if extras == self._extra_deny_current:
            return
        try:
            self._policy.set_deny_patterns(list(BUILTIN_DENY_PATTERNS) + extras)
        except re.error as exc:
            logger.warning("tools.exec extra deny patterns rejected ({}); keeping the current set", exc)
            return
        self._extra_deny_current = extras

    def ruling(self, tool_name: str, params: dict[str, Any]) -> Deny | NeedsApproval | None:
        """This call's builtin ruling, or None where the platform has no say.

        A ``Deny`` is a verdict. A ``NeedsApproval`` from here is only the
        declared family's description for the gate to carry -- the gate still
        runs the tiers and the mode before deciding whether anyone is asked.
        Only shell commands are classified today; every other tool answers
        None and is governed by tiers alone.
        """
        if tool_name != "exec":
            return None
        command = params.get("command")
        if not isinstance(command, str):
            return None
        self._refresh_deny_patterns()
        outcome = self._policy.classify(command)
        if outcome.decision is CommandDecision.HARD_DENY:
            why = _DENY_REASONS.get(outcome.reason_code, "")
            reason = f"Command blocked by safety guard: it {why}" if why else "Command blocked by safety guard"
            if outcome.reason_code == "parse_error":
                # Fail-closed but not protected: no rule refused this command,
                # the parse did. Naming it apart is what lets the gate answer
                # with "fix the quote and retry" instead of "abandon this".
                return Deny(reason=reason, source=DecisionSource.BUILTIN_PARSE_ERROR)
            return Deny(reason=reason, source=DecisionSource.BUILTIN_DENY)
        if outcome.decision is CommandDecision.REQUIRE_APPROVAL:
            description = _APPROVAL_DESCRIPTIONS.get(outcome.reason_code, "Run a command that needs your approval")
            return NeedsApproval(
                reason=f"Command requires user approval: {description.lower()}",
                description=description,
                digest=action_digest(tool_name, params),
                family=outcome.reason_code,
            )
        return None


__all__ = [
    "BUILTIN_DENY_PATTERNS",
    "BuiltinRulings",
    "action_digest",
    "action_line",
]
