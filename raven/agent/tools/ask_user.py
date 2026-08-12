"""ask_user tool — pause the turn to ask the user a question and await the reply.

Blocking interaction: the registry does NOT wrap this in a timeout (the
QuestionBroker manages its own fail-safe). On execute the tool hands the turn's
conversation_id and the prompt to the broker, which emits a ``clarify.request``
notification and blocks until an inbound answer arrives (or the broker's
fail-safe default fires). The returned answer is rendered as a natural-language
tool result; the loop never sees an exception.
"""

import json
from contextvars import ContextVar
from typing import Any

from raven.agent.tools.base import Tool, ToolResult
from raven.tui_rpc.question_broker import QuestionBroker

# How many times an argument may be JSON-decoded before it is treated as text.
_MAX_JSON_LAYERS = 3


def _normalize_questions(raw: Any) -> list[dict[str, Any]]:
    """Coerce the model's ``questions`` argument into the documented shape.

    Models routinely emit an array-typed argument as a JSON *string*, and a
    string is iterable, so every ``entry.get(...)`` below would raise
    ``AttributeError`` on a character. That escapes as far as the scheduler and
    kills the whole turn, leaving the user with no reply at all -- so parse what
    was plainly meant and drop what cannot be read, rather than trusting the
    declared schema.
    """
    raw = _loads(raw)
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    entries = [q for q in raw if isinstance(q, dict)]
    if entries:
        return entries
    # Nothing object-shaped in the list at all: the model wrote its questions as
    # plain strings. Dropping them returned "requires at least one question" for
    # a question that was perfectly clear, which loses the round for the same
    # kind of formatting quirk this function exists to absorb. Only when no
    # entry is an object, so a mixed list still means "the objects are the
    # questions and the loose string is noise".
    return [{"question": q} for q in raw if isinstance(q, str) and q.strip()]


def _normalize_options(raw: Any) -> list[str]:
    """Coerce one entry's ``options`` into a list of strings.

    Same declared shape as ``questions`` and the same habit of arriving as a
    JSON string, so it needs the same treatment: iterating a string yields
    characters, and here that reaches the user as one suggested answer per
    letter instead of one per option. A string that is not JSON is one option,
    not its letters.
    """
    if isinstance(raw, str):
        parsed = _loads(raw)
        raw = parsed if parsed is not raw else [raw]
    if raw is None:
        return []
    if not isinstance(raw, list):
        return [str(raw)]
    return [str(o) for o in raw]


def _loads(raw: Any) -> Any:
    """``json.loads`` for a string, unchanged for anything else, never raising.

    The exception list is the point. ``json.loads`` answers deeply nested input
    with ``RecursionError``, which is not a ``ValueError``, so catching only
    ``TypeError``/``ValueError`` let that one escape by exactly the route this
    module exists to close: out of ``display_call``, which only labels a
    transcript row, and on to the scheduler, killing the turn.

    Unwraps repeatedly because the encoding is sometimes applied twice -- a
    JSON string holding a JSON string holding the array -- and one pass leaves
    that as an unusable string. Bounded rather than looped to exhaustion: past
    a couple of layers this is no longer a quirk to absorb, and the bound is
    what keeps a crafted argument from spending the turn on unwrapping.
    """
    for _ in range(_MAX_JSON_LAYERS):
        if not isinstance(raw, str):
            return raw
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            return raw
        if parsed is raw:
            return raw
        raw = parsed
    return raw


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

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Normalize before the registry validates, not after it dispatches.

        ``ToolRegistry.execute`` casts, then validates against the declared
        schema, and returns the error without ever calling ``execute``
        (``registry.py:80-85``). ``questions`` is declared ``array`` of
        ``object`` and ``options`` ``array``, so precisely the shapes worth
        absorbing -- the array arriving as a JSON string, an entry that is a
        plain string, ``options`` as a JSON string -- are rejected one step
        before the coercion that would have handled them. Normalizing in
        ``execute`` therefore looked right and never ran on anything.

        This hook is where the registry expects the adjustment, so the schema
        stays honest about what the model should send while a near miss still
        reaches the user. Runs before ``super()`` so the base cast can coerce
        the leaves this exposes -- a non-string ``question``, options that are
        not strings -- exactly as it does for a well-formed call.
        """
        params = dict(params)
        if "questions" in params:
            entries = []
            for entry in _normalize_questions(params["questions"]):
                entry = dict(entry)
                if "options" in entry:
                    entry["options"] = _normalize_options(entry["options"])
                entries.append(entry)
            params["questions"] = entries
        return super().cast_params(params)

    def display_call(self, args: dict[str, Any]) -> str | None:
        """Show the question itself, not the raw arguments blob. A batch keeps
        every question visible (joined) so the row still says what was asked;
        the UI elides whatever does not fit.

        Keeps its own normalization: this is handed the raw
        ``tool_call.arguments`` at ``loop/main.py`` with no cast or validation
        in between, which is the path that took the whole turn down."""
        entries = _normalize_questions(args.get("questions"))
        questions = [str(q.get("question", "")).strip() for q in entries]
        questions = [q for q in questions if q]
        if not questions:
            return None
        if len(questions) == 1:
            return questions[0]
        return " | ".join(questions)

    async def execute(self, questions: Any, **kwargs: Any) -> "str | ToolResult":
        cid = self._cid.get()
        if not self._broker:
            return "Error: ask_user not configured (no question broker)"
        if not cid:
            return "Error: ask_user has no conversation context"
        entries = _normalize_questions(questions)
        if not entries:
            return "Error: ask_user requires at least one question"

        told: list[str] = []  # model-facing
        # Human-facing display: one "question -> answer" line per question, so a
        # batch shows which answer belongs to which question. The UI renders each
        # line as its own row.
        picks: list[str] = []
        for entry in entries:
            question = str(entry.get("question", "")).strip()
            if not question:
                continue
            answer = await self._broker.await_question(
                cid,
                prompt=question,
                choices=_normalize_options(entry.get("options")),
            )
            if answer:
                told.append(f'User answered: "{question}" -> "{answer}".')
                picks.append(f"{question} -> {answer}" if len(entries) > 1 else str(answer))
            else:
                told.append(f'For "{question}": (user did not answer; proceed with best judgment).')
                picks.append(f"{question} -> (no answer)" if len(entries) > 1 else "(no answer)")

        if not told:
            return "Error: ask_user requires at least one non-empty question"
        return ToolResult(
            model_text=" ".join(told) + " Continue.",
            display_text="\n".join(picks) if len(picks) > 1 else f"answered: {picks[0]}",
        )
