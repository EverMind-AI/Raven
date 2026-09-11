"""The asking papers: turn-scoped capabilities a host lends the tools.

A tool that has to put a question to the user, ask approval for one exact
command, or hand a prompt to whoever is driving the session, types against one
of these shapes. The concrete brokers live with the transport that constructs
them (the RPC surface, the gateway's channels, the ACP host) and are injected
at assembly; nothing here imports a machine.

The host side of the same seam is here too: what a tool exposes for an entrance
to lend it a capability for one turn (``SupportsDirectAsk``; approval binds the
turn-scoped permission context instead of any tool). An entrance probes for
that shape, never for the class
-- a shelf may seat another Tool implementation, and the entrance must not care.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from raven.contracts.permissions import ApprovalOutcome


class ApprovalResponder(Protocol):
    """Turn-scoped capability that can approve one exact action.

    ``command`` is the action as the human should read it -- a shell command
    verbatim, any other tool as a short action line. The outcome distinguishes
    a refusal that continues the turn from the one click that ends it
    (:class:`~raven.contracts.permissions.ApprovalChoice`); every transport
    failure and timeout must come back as a deny, never as an exception.
    """

    async def await_approval(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        tool_call_id: str,
        command: str,
        description: str,
        suggested_pattern: str = "",
    ) -> ApprovalOutcome: ...


class Asker(Protocol):
    async def ask(
        self,
        prompt: str,
        choices: list[str] | None,
        conversation_id: str,
        *,
        index: int = 0,
        total: int = 1,
        batch: list[dict[str, str]] | None = None,
    ) -> str | None: ...


class QuestionResponder(Protocol):
    """Turn-scoped capability that can put questions to the user and await
    answers.

    The paper the tools type against -- the concrete broker lives with the
    transport that constructs it and is injected at assembly (mirror of
    ``ApprovalResponder`` in ``shell.py``). Structural: no machine imports
    this, nothing here imports a machine.
    """

    async def await_question(
        self,
        conversation_id: str,
        *,
        prompt: str,
        choices: list[str] | None = None,
        default: str = "",
        timeout_s: float | None = None,
        header: str = "",
        recommended: str = "",
        index: int = 0,
        total: int = 1,
        batch: list[dict[str, Any]] | None = None,
    ) -> str: ...


@runtime_checkable
class SupportsDirectAsk(Protocol):
    """A tool that can put one host-side question to the user outside a model
    tool call.

    What the turn's ``Asker`` is adapted from: the entrance probes whatever sits
    at ``ask_user`` for this shape, and a tool without it leaves the turn with no
    asker, so a sub-agent's question declines rather than reaching a method the
    tool does not have. ``None`` from ``ask_direct`` means the round-trip is
    structurally unavailable; ``""`` is the user's non-answer.
    """

    async def ask_direct(
        self,
        prompt: str,
        choices: list[str] | None,
        conversation_id: str,
        timeout_s: float | None = None,
        *,
        index: int = 0,
        total: int = 1,
        batch: list[dict[str, str]] | None = None,
    ) -> str | None: ...


__all__ = ["ApprovalResponder", "Asker", "QuestionResponder", "SupportsDirectAsk"]
__tier__ = "contract"
