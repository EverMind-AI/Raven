"""What a tool does with the connections a sandbox's network policy refused while it ran.

A sandbox refuses a connection at run time, after the gate has already let the
command through, so the refusal never meets the gate on the way in. This module
brings it to the gate on the way out. A target one of the user's deny rules
names is refused without asking. The rest are asked about in one prompt
(``PermissionGate.authorize_runtime``), under the same mode, grants and
responder as any tool call, and what the person allows is opened where it
applies: this sandbox for "once", every sandbox of the conversation for "this
session", every sandbox from now on for "always". The command is never run
again here; the model is told it may run it.

With no gate bound (a tool run outside the registry), with nobody to ask, or for
a refusal that names no program or port to open, the targets are named to the
model, which otherwise sees only a connection error, and recorded on the turn,
so a run nobody watched still reports what it could not reach.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.config.update import allow_network_rule
from raven.contracts.permissions import DecisionSource
from raven.permissions.judge import JudgeOutcome, review_network
from raven.permissions.network_grants import GRANTS
from raven.permissions.runtime import RuntimeChoice, current_gate
from raven.permissions.turn import current_turn, note_refusal
from raven.sandbox.denials import NetworkDenial, denial_line
from raven.sandbox.network_policy import NetworkGrant, refusing_rule

if TYPE_CHECKING:
    from raven.config.schema import PermissionsConfig
    from raven.permissions.gate import PermissionGate
    from raven.sandbox.interfaces import SandboxExecutor

REFUSAL_SOURCE = "sandbox_network"
KIND = "sandbox.network"
# A command can be refused hundreds of hosts in one run (a script looping over
# a list); the output it appends to is already capped, so this notice is too,
# and one prompt asks about no more targets than it names.
_MAX_LISTED = 10
_MAX_LINE = 160


@dataclass(frozen=True)
class DenialNotice:
    """The text to append to the tool's output, and whether the person asked to stop the turn."""

    text: str = ""
    stop: bool = False


async def handle_denials(
    executor: SandboxExecutor | None,
    denials: Sequence[NetworkDenial],
    *,
    command: str,
    tool_name: str = "exec",
) -> DenialNotice:
    """Settle the connections ``denials`` names, and say what came of it."""
    if not denials:
        return DenialNotice()
    gate = current_gate()
    if gate is None:
        _record_blocked(denials, tool_name)
        return DenialNotice(_said([_blocked(denials)]))
    network = gate.permissions().network
    refused: list[NetworkDenial] = []
    askable: list[NetworkDenial] = []
    unaskable: list[NetworkDenial] = []
    for denial in denials:
        if refusing_rule(network, denial.binary, denial.host, denial.port) is not None:
            refused.append(denial)
        elif denial.binary and denial.port is not None:
            askable.append(denial)
        else:
            unaskable.append(denial)
    sentences: list[str] = []
    stop = False
    if refused:
        for line in _lines(refused):
            note_refusal(
                tool_name, line, "refused by a deny rule in permissions.network", DecisionSource.USER_DENY.value
            )
        sentences.append(f"Your network rules refuse {_listed(refused)}; do not try another route to them.")
    if askable:
        sentence, stop = await _ask(gate, executor, askable, command=command, tool_name=tool_name)
        sentences.append(sentence)
    if unaskable:
        _record_blocked(unaskable, tool_name)
        sentences.append(_blocked(unaskable))
    return DenialNotice(_said(sentences), stop)


async def _ask(
    gate: PermissionGate,
    executor: SandboxExecutor | None,
    denials: list[NetworkDenial],
    *,
    command: str,
    tool_name: str,
) -> tuple[str, bool]:
    shown = denials[:_MAX_LISTED]
    lines = _lines(shown)
    asked = ", ".join(lines)
    rest = len(denials) - len(shown)
    unasked = f" {rest} more blocked targets were not asked about." if rest else ""
    grants = [NetworkGrant(denial.binary, denial.host, denial.port) for denial in shown]
    keys = tuple(f"net:{grant.binary}@{grant.host}:{grant.port}" for grant in grants)
    targets = [
        {"binary": denial.binary, "host": denial.host, "port": denial.port, "reason": denial.reason} for denial in shown
    ]

    async def review(provider: Any, config: PermissionsConfig) -> JudgeOutcome:
        return await review_network(
            provider,
            targets=targets,
            command=command,
            model=config.judge_model or None,
            timeout_s=config.judge_timeout_seconds,
        )

    answer = await gate.authorize_runtime(
        tool_name=tool_name,
        kind=KIND,
        action="; ".join(lines),
        description="The sandbox blocked these connections. Allow them?",
        digest=f"{KIND}:" + hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()[:16],
        session_keys=keys,
        evidence={
            "targets": targets,
            "command": command,
            # The main sandbox is every conversation's: what is opened in it
            # stays open for all of them until Raven restarts, and the card says so.
            "shared": executor is not None and GRANTS.is_shared(executor),
        },
        # No rule is offered to save: a card's saved rule, its undo and its
        # editor are the exec pattern's, so the broader grant offered is the session.
        suggested_pattern="",
        review=review,
        # A deny rule names hosts, so an address given as an IP (a cloud
        # metadata endpoint among them) is never opened without a person.
        needs_person=any(_is_address(denial.host) for denial in shown),
    )
    choice = answer.choice
    if choice.approved:
        await _open(executor, grants, choice)
        return f"Network access for {asked} was approved; run the command again.{unasked}", False
    said = f' The user said: "{answer.feedback}"' if answer.feedback else ""
    if choice is RuntimeChoice.DENY_STOP:
        return f"The user declined network access to {asked} and asked to stop here.{said}", True
    if choice is RuntimeChoice.DENY:
        return f"The user declined network access to {asked}; do not try another route to them.{said}{unasked}", False
    if choice is RuntimeChoice.LAPSED:
        return (
            f"The request to allow network access to {asked} expired with no answer. Nobody refused it. "
            f"Tell the user the approval lapsed; do not try another route to them in this turn.{unasked}"
        ), False
    return _blocked(denials), False


async def _open(executor: SandboxExecutor | None, grants: list[NetworkGrant], choice: RuntimeChoice) -> None:
    """Open ``grants`` where ``choice`` reaches, and wait until the sandboxes have loaded them.

    The sandbox that was refused is granted directly whatever the scope, since
    its next command is the one the model is told to run; the conversation's
    other sandboxes read a session grant or a new rule from their network
    source, and are synced here so they do not wait for it on their next command.
    """
    scope = current_turn().session_key
    if choice is RuntimeChoice.ALWAYS:
        for grant in grants:
            try:
                allow_network_rule(grant.host, grant.port, grant.binary)
            except (ValueError, OSError) as exc:
                # The person did say yes: it holds for the session, and why it
                # is not on disk is logged.
                logger.warning("Could not write a permissions.network rule for {}: {}", grant, exc)
                GRANTS.grant(scope, grant)
    elif choice is RuntimeChoice.SESSION:
        for grant in grants:
            GRANTS.grant(scope, grant)
    loads = [executor.grant_network(grants)] if executor is not None else []
    if choice is not RuntimeChoice.ONCE:
        loads += [other.sync_network() for other in GRANTS.executors_for(scope) if other is not executor]
    for outcome in await asyncio.gather(*loads, return_exceptions=True):
        if outcome is not True:
            logger.warning("A sandbox did not load an approved network grant: {}", outcome)


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _blocked(denials: Sequence[NetworkDenial]) -> str:
    return (
        f"The sandbox network policy blocked: {_listed(denials)}. "
        "This is a policy block, not a network failure. Tell the user; it can be allowed under permissions.network."
    )


def _record_blocked(denials: Sequence[NetworkDenial], tool_name: str) -> None:
    for line in _lines(denials):
        note_refusal(tool_name, line, "blocked by the sandbox network policy", REFUSAL_SOURCE)
    rest = len(denials) - _MAX_LISTED
    if rest > 0:
        note_refusal(
            tool_name, f"and {rest} more blocked targets", "blocked by the sandbox network policy", REFUSAL_SOURCE
        )


def _lines(denials: Sequence[NetworkDenial]) -> list[str]:
    return [_shortened(denial_line(denial)) for denial in denials[:_MAX_LISTED]]


def _listed(denials: Sequence[NetworkDenial]) -> str:
    rest = len(denials) - _MAX_LISTED
    return ", ".join(_lines(denials)) + (f", and {rest} more" if rest > 0 else "")


def _said(sentences: list[str]) -> str:
    return "".join(f"\n\n[sandbox] {sentence}" for sentence in sentences)


def _shortened(line: str) -> str:
    return line if len(line) <= _MAX_LINE else line[: _MAX_LINE - 3] + "..."
