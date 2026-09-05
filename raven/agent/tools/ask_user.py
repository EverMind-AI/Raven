"""ask_user tool — pause the turn to ask the user a question and await the reply.

Blocking interaction: the registry does NOT wrap this in a timeout (the
QuestionBroker manages its own fail-safe). On execute the tool hands the turn's
conversation_id and the prompt to the broker, which emits a ``clarify.request``
notification and blocks until an inbound answer arrives (or the broker's
fail-safe default fires). The returned answer is rendered as a natural-language
tool result; the loop never sees an exception.

A batch shares one deadline rather than one per question, and a call whose
shape would waste the user's time -- a single-option question, a repeated
question, more questions than the cap -- is rejected before anything is
rendered, with a message that steers the next attempt.
"""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from raven.agent.tools.base import Tool, ToolResult
from raven.tui_rpc.question_broker import DEFAULT_TIMEOUT_S, QuestionBroker

MAX_QUESTIONS = 4
"""Cap on one call. Each question is its own round-trip, so an uncapped
batch is an uncapped number of prompts in front of one user."""

_MAX_HEADER_CHARS = 12


@dataclass(frozen=True)
class _Question:
    """One validated question: what to ask, and what to fall back on."""

    question: str
    header: str
    options: list[str]
    recommended: str


def _dedup(labels: list[str]) -> list[str]:
    seen: set[str] = set()
    return [label for label in labels if not (label in seen or seen.add(label))]


def _prepare(entries: list[dict[str, Any]]) -> tuple[list["_Question"], str]:
    """Validate and normalize the questions, or return why to reject the call.

    Rejection happens before any question reaches a human, so a malformed
    call costs the user nothing and the message is what steers the retry.
    """
    if len(entries) > MAX_QUESTIONS:
        return [], (
            f"Error: ask_user accepts at most {MAX_QUESTIONS} questions per call "
            f"(got {len(entries)}); split them across calls."
        )
    prepared: list[_Question] = []
    seen: set[str] = set()
    for entry in entries:
        question = str(entry.get("question", "")).strip()
        if not question:
            continue
        if question in seen:
            return [], (
                f'Error: ask_user rejected the call -- duplicate question text "{question}". Ask each question once.'
            )
        seen.add(question)
        # A repeated label is a typo with one obvious reading, so drop it; a
        # repeated question would prompt the same human twice, so reject that.
        submitted = [str(option) for option in entry.get("options") or []]
        options = _dedup(submitted)
        if len(options) == 1:
            return [], (
                f'Error: ask_user rejected the call -- question "{question}" has exactly one '
                "option, which is not a decision. Do not retry with a filler second option: "
                "state that single path as the approach you are taking and continue. A question "
                "needs two or more options, or none at all for a free-form answer."
            )
        pick = entry.get("recommended")
        # The index counts the options as submitted, so resolve it before dedup
        # narrows the list. Dedup keeps every distinct label, so the label this
        # resolves to is still one of the choices the surface can mark.
        recommended = (
            submitted[pick]
            if isinstance(pick, int) and not isinstance(pick, bool) and 0 <= pick < len(submitted)
            else ""
        )
        prepared.append(
            _Question(
                question=question,
                header=str(entry.get("header", "")).strip()[:_MAX_HEADER_CHARS],
                options=options,
                recommended=recommended,
            )
        )
    return prepared, ""


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
        timeout_s: float | None = None,
    ) -> None:
        # The broker is the shared transport singleton (not per-turn). The
        # conversation_id is per-turn, so it lives in a ContextVar — a turn runs
        # in its own lane task, so a concurrent turn cannot clobber it. A str is
        # immutable, so a plain set/get is task-isolated without copy-on-write.
        self._broker = broker
        self._cid: ContextVar[str] = ContextVar("ask_user_cid", default=conversation_id)
        # Configured budget for one whole call. It arrives here rather than at
        # the broker because the broker is built before any config is loaded on
        # the TUI transport, and re-reading config there would put a schema
        # failure in the path of the RPC server coming up.
        self._timeout_s = timeout_s

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
            "When you can name a few likely answers, pass them as 'options' -- two or "
            "more, or none at all for a free-form question (the user can always type an "
            "answer instead). Point at the one you would pick with 'recommended'. Batch "
            f"related questions into one call, up to {MAX_QUESTIONS}; they share one deadline."
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
                            "header": {
                                "type": "string",
                                "description": (
                                    f"Very short label ({_MAX_HEADER_CHARS} chars or fewer) shown "
                                    "as a chip beside the question, e.g. 'Base branch'. Optional."
                                ),
                            },
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Suggested answers: give two or more, or none at all for a "
                                    "free-form question. One option is not a decision and is "
                                    "rejected. Do not add an 'Other' entry -- the user always "
                                    "has a free-form answer."
                                ),
                            },
                            "recommended": {
                                "type": "integer",
                                "description": (
                                    "0-based index into 'options' of the option you recommend. "
                                    "Omit when you have no preference."
                                ),
                            },
                        },
                        "required": ["question"],
                    },
                    "maxItems": MAX_QUESTIONS,
                    "description": f"One to {MAX_QUESTIONS} questions to ask the user",
                }
            },
            "required": ["questions"],
        }

    def display_call(self, args: dict[str, Any]) -> str | None:
        """Show the question itself, not the raw arguments blob. A batch keeps
        every question visible (joined) so the row still says what was asked;
        the UI elides whatever does not fit."""
        questions = [str(q.get("question", "")).strip() for q in args.get("questions") or []]
        questions = [q for q in questions if q]
        if not questions:
            return None
        if len(questions) == 1:
            return questions[0]
        return " | ".join(questions)

    async def execute(self, questions: list[dict[str, Any]], **kwargs: Any) -> "str | ToolResult":
        cid = self._cid.get()
        if not self._broker:
            return "Error: ask_user not configured (no question broker)"
        if not cid:
            return "Error: ask_user has no conversation context"
        if not questions:
            return "Error: ask_user requires at least one question"

        prepared, rejection = _prepare(questions)
        if rejection:
            return rejection
        if not prepared:
            return "Error: ask_user requires at least one non-empty question"

        # One budget for the whole batch, not one per question: each question is
        # its own round-trip, so a per-question timeout let three questions hold
        # a turn open for three times the surface's wait.
        budget = float(self._timeout_s or getattr(self._broker, "default_timeout_s", DEFAULT_TIMEOUT_S))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        batch = [{"question": item.question, "header": item.header} for item in prepared]

        told: list[str] = []  # model-facing
        # Human-facing display: one "question -> answer" line per question, so a
        # batch shows which answer belongs to which question. The UI renders each
        # line as its own row.
        picks: list[str] = []
        for index, item in enumerate(prepared):
            remaining = deadline - loop.time()
            # A spent budget stops the batch rather than opening a fresh wait on
            # every question that is left.
            answer = (
                await self._broker.await_question(
                    cid,
                    prompt=item.question,
                    choices=item.options,
                    timeout_s=remaining,
                    header=item.header,
                    recommended=item.recommended,
                    index=index,
                    total=len(prepared),
                    batch=batch,
                )
                if remaining > 0
                else ""
            )
            if answer:
                told.append(f'User answered: "{item.question}" -> "{answer}".')
                picks.append(f"{item.question} -> {answer}" if len(prepared) > 1 else str(answer))
            else:
                # Naming the option the model recommended is what lets it carry on
                # the way it intended; without it the only signal is "no answer".
                hint = f' recommended option was "{item.recommended}";' if item.recommended else ""
                told.append(f'For "{item.question}": (user did not answer;{hint} proceed with best judgment).')
                picks.append(f"{item.question} -> (no answer)" if len(prepared) > 1 else "(no answer)")

        return ToolResult(
            model_text=" ".join(told) + " Continue.",
            display_text="\n".join(picks) if len(picks) > 1 else f"answered: {picks[0]}",
        )
