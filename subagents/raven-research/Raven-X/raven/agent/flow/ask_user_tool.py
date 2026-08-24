"""The ``ask_user`` tool as DR mode presents it — a signal, not a round trip.

Why this is a second class rather than a mode on ``agent/tools/ask_user.py``.
That one is live on two transports: the gateway hands it a ``QuestionBroker``
(``cli/gateway_commands.py``) and the TUI RPC layer resolves its pending question
(``tui_rpc/methods``). It blocks, by contract, and the registry skips its timeout
for that reason. DR mode's ``ask_user`` never blocks: ``AskUserGate`` (phase 2) reads the
proposed call in ``before_execute_tools`` and short-circuits the turn, so the
call becomes this turn's reply and the answer arrives as the next user message.
One class serving both would carry two opposite blocking semantics under one
tool name, which is the shape this flow rejected when it declined to reuse
``Session.pending_clarification``.

The description and the parameter schema below are prompt text: they enter the
tool schema the model reads and they move the call rate. They are stamped
alongside the prompt segment (``scripts/stamp_dr_segment.py``), which is why the
instance is built in ``build_dr_flow`` and carried on the assembly rather than
constructed at the registration site — a stamp that instantiated its own copy
would be a probe on a different path than the code it certifies.
"""

from __future__ import annotations

from typing import Any

from raven.agent.tools.base import Tool

# Returned when the model's call reaches execution, which happens whenever the
# gate declined to hand off. NOT dead code - ``CompositeHook`` halts a phase only
# on ``short_circuit_result`` or ``rollback``, and the guardrail returns neither,
# so the loop proceeds and the model reads this string. Both are stamped with the
# schema for that reason.
#
# Two strings, because there are two reasons and only one of them is the model's
# doing. Naming the wrong one is not cosmetic: the outline-only sentence is the
# whole of what the guardrail teaches, so a single string covering both cases
# either drops that lesson or tells a model that asked three good questions that
# it asked none.
_FALLBACK_NO_QUESTIONS = (
    "ask_user needs at least one question the user can answer; an outline alone "
    "is not one. Proceed with your own best reading of the question and continue "
    "researching."
)

_FALLBACK_NOT_DELIVERED = (
    "ask_user did not reach the user on this turn. Proceed with your own best "
    "reading of the question and continue researching."
)


class DRAskUserTool(Tool):
    """Ask the user before researching. One call, at the start of a turn.

    ``blocking_interaction`` stays False: nothing here waits on a human, and the
    registry must treat it as an ordinary tool.
    """

    def __init__(self, *, outline: bool = True, mode: str = "when_needed",
                 max_questions: int = 3, max_outline_items: int = 5) -> None:
        self._outline = outline
        self._mode = mode
        # Clamped, not validated in the config: both numbers are rendered INTO
        # prompt text below, so a negative one ships "Up to -3 questions" to the
        # model. The stamp would catch the drift after the fact; this stops it
        # being written.
        self._max_questions = max(1, max_questions)
        self._max_outline_items = max(1, max_outline_items)

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        # No "wait for their answer" (nothing waits) and no "gather a preference"
        # (that is the personalizer's job, and conflating the two is what makes
        # the two clarify paths hard to tell apart in a trajectory).
        base = (
            "Ask the user the questions that decide how to research this, and end "
            "your turn. Their next message answers you. Every line you write here "
            "is addressed to them, so use the second person throughout. "
        )
        # The description has to agree with the contract clause, or the two prompt
        # surfaces disagree about the same call and the reading is unattributable.
        if self._mode == "first_turn":
            base += (
                "On the first turn of a conversation, call it before any search, "
                "even when the question looks complete - name the readings you "
                "would otherwise be choosing between, or the scope you would "
                "otherwise assume. On later turns use it only when answering well "
                "depends on something only they can settle. Either way: do not ask "
                "what you can look up, and never use it as a way to stop working."
            )
        else:
            base += (
                "Use it when answering well depends on something only they can "
                "settle - which entity, period or jurisdiction they mean, which of "
                "two readings of the question, what the deliverable is. Do not use "
                "it to confirm something you can look up, and never as a way to "
                "stop working: a question you could answer yourself costs the user "
                "a round trip and gains nothing."
            )
        if self._outline:
            base += (
                " Pass 'outline' as well: the sub-questions you will settle, what "
                "kind of evidence each needs, and what you will deliver. An "
                "outline names decisions, not the searches you would run, and it "
                "begins after their answers - never list asking them as a step."
            )
        return base

    @property
    def parameters(self) -> dict[str, Any]:
        # No top-level "required": ``"questions": []`` satisfies it, so it cannot
        # carry the guardrail, and the guardrail lives in exactly one place
        # (``AskUserGate``, phase 2). ``multiple`` / ``custom`` from the blocking tool are
        # absent: rendering is markdown text, so nothing would honour them.
        props: dict[str, Any] = {
            "questions": {
                "type": "array",
                "description": (
                    f"Up to {self._max_questions} questions, each self-contained."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": (
                                "The full question, phrased to stand alone."
                            ),
                        },
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Likely answers, if you can name them. The user "
                                "can always answer in their own words."
                            ),
                        },
                    },
                    "required": ["question"],
                },
            }
        }
        if self._outline:
            props["outline"] = {
                "type": "array",
                "description": (
                    f"Up to {self._max_outline_items} steps, all of them AFTER the "
                    "questions above are answered. Decisions, not queries."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": "The sub-question this settles.",
                        },
                        "evidence": {
                            "type": "string",
                            "description": (
                                "What kind of source settles it - a source, never "
                                "the user; their answers are already in hand by "
                                "the time this step runs."
                            ),
                        },
                        "why": {
                            "type": "string",
                            "description": (
                                "Why the ANSWER depends on this step - not who "
                                "benefits from it."
                            ),
                        },
                    },
                    "required": ["goal", "evidence"],
                },
            }
        return {"type": "object", "properties": props}

    async def execute(
        self,
        questions: list[dict[str, Any]] | None = None,
        outline: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        # Every parameter defaults: with no top-level "required" the registry's
        # validation passes a call carrying only ``outline``, and that is the call
        # that reaches here most often.
        if not questions:
            return _FALLBACK_NO_QUESTIONS
        return _FALLBACK_NOT_DELIVERED


__all__ = ["DRAskUserTool"]
