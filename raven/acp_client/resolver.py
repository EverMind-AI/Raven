"""The turn's autofill: the recall, the model call, and the budget around both.

Separate from `autofill.py` on purpose, the split `elicitor.py` makes against
`elicitation.py`: that module decides what an answer means and is pure, this one
holds the awaits and is the only part that needs a running loop.

One object per turn, bound on the same ContextVar as the turn's asker, because
the two callers -- `Elicitor` and `AskUserResponder` -- are built from config
with no loop reference and cannot resolve either by looking one up.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from uuid import uuid4

from loguru import logger

from raven.acp_client import autofill
from raven.acp_client.autofill import Question, Resolution
from raven.spine.events import ToolEvent, ToolPhase

RECALL_BUDGET_S = 5.0
"""Matches `MemorySegmentBuilder._RECALL_BUDGET_S`. The same store, the same
turn, and the same rule: memory improves an answer, it never gates one."""

TOOL_NAME = "answer_for_user"
"""The name this step is rendered and written back under.

Never registered in the `ToolRegistry`: registering it would hand the model an
interface for claiming it had answered on the user's behalf. Both frontends
render an unknown tool name generically, so nothing had to learn it.
"""


def summarise(questions: list[Question], resolutions: list[Resolution]) -> str:
    """One line per question saying where it went, in the order asked."""
    lines = []
    for question, resolution in zip(questions, resolutions, strict=True):
        if resolution.status == "answer":
            lines.append(f"{question.prompt} -> {resolution.answer} (answered for you)")
        elif resolution.status == "partial":
            lines.append(f"{question.prompt} -> asked you, with: {resolution.known}")
        else:
            lines.append(f"{question.prompt} -> asked you")
    return "\n".join(lines)


class Autofill:
    """One turn's attempt to answer its sub-agents' questions."""

    def __init__(self, loop: Any, *, emit: Any, conversation_id: str, config: Any) -> None:
        self._loop = loop
        self._emit = emit
        self._conversation_id = conversation_id
        self._config = config
        self._ledger: list[str] = []
        self._rows: list[dict[str, Any]] = []
        self._messages: list[dict[str, Any]] | None = None
        self._provider: Any = None
        self._model: str | None = None

    def set_snapshot(self, messages: list[dict[str, Any]]) -> None:
        """Publish the turn's message list and its binding, from the turn's own task.

        Handed over rather than looked up. The list is a local of
        `_run_agent_loop` that is reassigned on every append, so nothing outside
        can hold it; and it cannot be fetched by key either, because a direct-chat
        turn runs under `session_of(cid)` rather than under the conversation id,
        so a lookup keyed on the conversation would silently read nothing in
        exactly the lane where a sub-agent is most likely to ask.

        The provider and the model are taken here for the reason the callers read
        `current_autofill` at construction: both are properties over
        `active_binding()`, which a turn enters in `run_turn`, while `_resolve`
        runs on the ACP connection's read loop, whose ContextVars are a copy of
        the *first* turn's. Read there, a session that had switched model would
        send this call to the previous binding's provider and credential --
        `AgentLoop._verdict_provider` documents the same hazard for the node
        judge, which resolves it per dispatch instead.
        """
        self._messages = messages
        self._provider = getattr(self._loop, "provider", None)
        self._model = getattr(self._loop, "model", None)

    async def resolve(self, questions: list[Question], *, agent: str, instance: str) -> list[Resolution]:
        """One decision per question, in the order asked. Never raises."""
        if not questions or not self._config.autofill_enabled:
            return autofill.defer_all(questions)
        try:
            return await asyncio.wait_for(
                self._resolve(questions, agent=agent, instance=instance),
                timeout=self._config.autofill_timeout_seconds,
            )
        except TimeoutError:
            logger.debug(
                "question autofill: call outran its {}s budget; the user will be asked",
                self._config.autofill_timeout_seconds,
            )
            return autofill.defer_all(questions)
        except Exception as exc:  # noqa: BLE001 - asking the user is always available
            logger.debug("question autofill: call failed ({}); the user will be asked", exc)
            return autofill.defer_all(questions)

    async def _resolve(self, questions: list[Question], *, agent: str, instance: str) -> list[Resolution]:
        if self._messages is None:
            # No turn was ever published, so there is nothing to continue: an
            # unwired host (a test, an offline entry point) would be deciding
            # from recall and priors alone, which is strictly less than raven
            # itself can see and not what this step is allowed to answer from.
            return autofill.defer_all(questions)
        # Captured first, live second: the fallback keeps a host that published
        # a snapshot before it had a provider to publish working, at the cost of
        # the turn's own binding -- which is what the capture is here to keep.
        provider = self._provider or getattr(self._loop, "provider", None)
        if provider is None:
            return autofill.defer_all(questions)
        snapshot = self._snapshot()
        memories = await self._recall(questions)
        messages = autofill.build_messages(
            snapshot=snapshot,
            ledger=list(self._ledger),
            memories=memories,
            questions=questions,
            agent=agent,
            instance=instance,
        )
        response = await provider.chat_with_retry(
            messages=messages,
            tools=autofill.answer_tool_schema(),
            model=self._model,
            tool_choice="auto",
        )
        resolutions = autofill.extract_resolutions(response, questions)
        await self._note(questions, resolutions, agent, instance)
        return resolutions

    def _snapshot(self) -> list[dict[str, Any]]:
        """The turn's messages as they stand.

        Copied on read, so no caller can mutate the turn; the stored reference
        is the live list, so appends made since `set_snapshot` are included --
        which is the point, since a question arrives mid-tool-call.

        An empty list is a published turn that has nothing in it yet, which is
        a context; never-published is refused one step above, in `_resolve`.
        """
        return list(self._messages or [])

    async def _recall(self, questions: list[Question]) -> list[str]:
        """Memory for these questions, keyed on the questions.

        A separate recall rather than the one already in the assembled context:
        that one was keyed on the user's message, which is the wrong query for
        "who should review this". A turn recalls on its new message, and this is
        the new message.
        """
        backend = getattr(self._loop, "backend", None)
        if backend is None:
            return []
        query = "\n".join(q.prompt for q in questions)
        try:
            hits = await asyncio.wait_for(
                backend.recall(
                    query=query,
                    user_id=self._loop.memory_config.user_id,
                    top_k=self._loop.memory_config.memory_top_k,
                ),
                timeout=RECALL_BUDGET_S,
            )
        except Exception as exc:  # noqa: BLE001 - the conversation alone is still a decision
            logger.debug("question autofill: recall unavailable ({}); deciding on the turn alone", exc)
            return []
        out = []
        for hit in hits or []:
            text = getattr(hit, "content", None) or getattr(hit, "text", None)
            if isinstance(text, str) and text.strip():
                out.append(text.strip())
        return out

    async def _note(self, questions: list[Question], resolutions: list[Resolution], agent: str, instance: str) -> None:
        """Record and render one form's outcome.

        Nothing here may fail a question: a row is an account of a decision that
        has already been made, and losing the account is better than losing the
        answer.
        """
        if not any(r.status == "answer" for r in resolutions):
            # A form that deferred everything must not add a row saying so, or
            # every sub-agent question grows a second row for no information.
            return
        summary = summarise(questions, resolutions)
        who = f"{agent}({instance})" if instance else agent
        self._ledger.append(f"- from {who}:\n{summary}")
        # The sub-agent's own wording is deliberately not carried here: a row
        # becomes the *arguments* of a synthetic assistant `tool_calls` entry,
        # which nothing fences, while the summary reaches the model through
        # `add_tool_result` and is. The summary names every question anyway.
        self._rows.append({"agent": agent, "instance": instance, "summary": summary})
        await self._emit_row(agent, instance, questions, summary)

    async def _emit_row(self, agent: str, instance: str, questions: list[Question], summary: str) -> None:
        if self._emit is None:
            return
        call_id = f"autofill-{uuid4().hex[:8]}"
        who = f"{agent}({instance})" if instance else agent
        try:
            await self._send(
                ToolEvent(
                    phase=ToolPhase.START,
                    tool_call_id=call_id,
                    name=TOOL_NAME,
                    arguments={
                        "agent": agent,
                        "instance": instance,
                        "questions": [q.prompt for q in questions],
                    },
                    display=f"answering for you: {who}",
                    conversation_id=self._conversation_id,
                )
            )
            await self._send(
                ToolEvent(
                    phase=ToolPhase.COMPLETE,
                    tool_call_id=call_id,
                    result_preview=summary,
                    ok=True,
                    conversation_id=self._conversation_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - see `_note`
            logger.debug("question autofill: could not render the row ({})", exc)

    async def _send(self, event: ToolEvent) -> None:
        out = self._emit(event)
        if inspect.isawaitable(out):
            await out

    def pending_rows(self) -> list[dict[str, Any]]:
        """The rows not yet written into the conversation, and forget them."""
        rows, self._rows = self._rows, []
        return rows


__all__ = ["RECALL_BUDGET_S", "TOOL_NAME", "Autofill", "summarise"]
