"""The asking papers: turn-scoped capabilities a host lends the tools.

A tool that has to put a question to the user, ask approval for one exact
command, or hand a prompt to whoever is driving the session, types against one
of these shapes. The concrete brokers live with the transport that constructs
them (the RPC surface, the gateway's channels, the ACP host) and are injected
at assembly; nothing here imports a machine.
"""

from __future__ import annotations

from typing import Any, Protocol


class ApprovalResponder(Protocol):
    """Turn-scoped capability that can approve one exact shell command."""

    async def await_approval(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        tool_call_id: str,
        command: str,
        description: str,
    ) -> bool: ...


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


__all__ = ["ApprovalResponder", "Asker", "QuestionResponder"]
__tier__ = "contract"
