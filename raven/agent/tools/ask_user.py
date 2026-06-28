"""ask_user tool — pause the turn to ask the user a question and await the reply.

Blocking interaction: the registry does NOT wrap this in a timeout (the
QuestionBroker manages its own fail-safe). On execute the tool hands the turn's
conversation_id and the prompt to the broker, which emits a ``clarify.request``
notification and blocks until an inbound answer arrives (or the broker's
fail-safe default fires). The returned answer is rendered as a natural-language
tool result; the loop never sees an exception.
"""

from contextvars import ContextVar
from typing import Any

from raven.agent.tools.base import Tool
from raven.tui_rpc.question_broker import QuestionBroker


class AskUserTool(Tool):
    """Ask the user a question mid-turn and wait for their answer.

    Wiring: the layer that builds the per-turn tool set must inject a
    :class:`QuestionBroker` (constructor or :meth:`set_broker`) and the turn's
    conversation_id via :meth:`set_context` — the same conversation_id the
    scheduler derives (``req.conversation or f"{channel}:{chat_id}"``).
    """

    blocking_interaction = True

    def __init__(
        self,
        broker: QuestionBroker | None = None,
        conversation_id: str = "",
    ) -> None:
        # The broker is the shared transport singleton (not per-turn). The
        # conversation_id is per-turn, so it lives in a ContextVar — a turn runs
        # in its own lane task, so a concurrent turn cannot clobber it. A str is
        # immutable, so a plain set/get is task-isolated without copy-on-write.
        self._broker = broker
        self._cid: ContextVar[str] = ContextVar("ask_user_cid", default=conversation_id)

    def set_broker(self, broker: QuestionBroker | None) -> None:
        """Set the QuestionBroker. ``None`` disables the round-trip."""
        self._broker = broker

    def set_context(self, conversation_id: str) -> None:
        """Set the current turn's conversation_id (the broker key, turn-local)."""
        self._cid.set(conversation_id)

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return (
            "Ask the user one or more questions and wait for their answer — to gather "
            "a preference, clarify an ambiguous request, or decide a choice with real "
            "trade-offs. Reach for it when the answer genuinely depends on the user; "
            "for low-stakes or reversible choices, pick a sensible default instead. "
            "When you can name a few likely answers, pass them as 'options' (the user "
            "can always type a free-form answer instead); if you recommend one, list "
            "it first with '(Recommended)'. Batch related questions into one call."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": (
                                    "The full, self-contained question to ask. "
                                    "Phrase it so it stands alone — do not repeat "
                                    "it in a separate title."
                                ),
                            },
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional list of suggested answers",
                            },
                            "multiple": {
                                "type": "boolean",
                                "description": "Whether multiple options may be chosen",
                            },
                            "custom": {
                                "type": "boolean",
                                "description": "Whether a free-form answer is allowed",
                            },
                        },
                        "required": ["question"],
                    },
                    "description": "One or more questions to ask the user",
                }
            },
            "required": ["questions"],
        }

    async def execute(self, questions: list[dict[str, Any]], **kwargs: Any) -> str:
        cid = self._cid.get()
        if not self._broker:
            return "Error: ask_user not configured (no question broker)"
        if not cid:
            return "Error: ask_user has no conversation context"
        if not questions:
            return "Error: ask_user requires at least one question"

        results: list[str] = []
        for entry in questions:
            question = str(entry.get("question", "")).strip()
            if not question:
                continue
            options = entry.get("options") or []

            answer = await self._broker.await_question(
                cid,
                prompt=question,
                choices=[str(o) for o in options],
            )
            if answer:
                results.append(f'User answered: "{question}" -> "{answer}".')
            else:
                results.append(
                    f'For "{question}": (user did not answer; proceed with best judgment).'
                )

        if not results:
            return "Error: ask_user requires at least one non-empty question"
        return " ".join(results) + " Continue."
