"""RoutineValidator — Stage 1 LLM verdict on learned candidate routines.

RoutineLearner produces candidates by deterministic binning + keyword
frequency. That catches real patterns but also catches keyword coincidences
("three different Tuesdays happened to mention dinner ≠ Tuesday-dinner
routine"). RoutineValidator asks an LLM to look at the candidate plus the
relevant user history and return a structured verdict:

    {is_routine: bool, confidence: 0-1, reason: str}

The result becomes ``Routine.llm_validation`` and is persisted via
routine_store, so we don't re-call the LLM on subsequent ticks for the same
routine_id. Downstream surfacing (TaskDiscoverer) can use the verdict to
decide which candidates to promote to user-facing routine_confirm options.

Design constraints:

- **Stateless**: caller owns caching. Validator just makes the LLM call.
- **Returns None on failure**: never raises, never silently fabricates a
  verdict. Caller decides retry/policy.
- **Bounded prompt**: history is truncated so the validation call cost is
  predictable regardless of HISTORY.md size.
- **No side effects**: doesn't write to routine_store, doesn't mutate the
  input Routine. Pure (routine, history) → verdict.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from loguru import logger

from raven.proactive_engine.sentinel.types import LLMValidation, Routine

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider


# 8000 chars ≈ ~2000 tokens for Chinese text — enough recurrence context
# for the validator without blowing per-call cost.
_HISTORY_MAX_CHARS = 8000

_VALIDATOR_MAX_TOKENS = 256
_VALIDATOR_TEMPERATURE = 0.1


VALIDATOR_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "routine_validation",
        "description": (
            "Report whether the candidate user-behavior pattern is a real "
            "recurring habit or just a keyword coincidence in the history."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "is_routine": {
                    "type": "boolean",
                    "description": (
                        "True if the pattern represents a real recurring "
                        "user habit (multiple intentional repetitions of "
                        "the same behavior at the same time/context). False "
                        "if it is a keyword coincidence (different topics "
                        "that happen to share a token, or one-off events)."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": (
                        "Your certainty in the is_routine verdict, 0 = totally unsure, 1 = clearly correct."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": ("Short rationale (<= 300 chars) — what in the history led you to this verdict."),
                },
            },
            "required": ["is_routine", "confidence", "reason"],
        },
    },
}


_SYSTEM_PROMPT = (
    "You validate candidate user-behavior routines learned from a personal "
    "AI assistant's history log. A 'routine' is a real recurring habit "
    "(same intent, same time window, repeated by the user with apparent "
    "intent). A 'coincidence' is when the deterministic learner grouped "
    "unrelated events that happened to share a keyword or time slot. "
    "Bias toward False when the evidence is ambiguous — a false positive "
    "wastes user attention later; a false negative just means the routine "
    "stays unsurfaced for now. Return your verdict via the "
    "routine_validation tool."
)


def _build_user_prompt(routine: Routine, history_md: str) -> str:
    if len(history_md) > _HISTORY_MAX_CHARS:
        history_md = history_md[-_HISTORY_MAX_CHARS:]
    dow_label = "any day"
    if routine.day_of_week is not None:
        dow_label = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][routine.day_of_week]
    slot_label = "any time"
    if routine.time_slot is not None:
        slot_label = f"{routine.time_slot[0]:02d}:00-{routine.time_slot[1]:02d}:00"
    return (
        f"## Candidate routine\n"
        f"- pattern: {routine.pattern}\n"
        f"- keywords: {routine.keywords}\n"
        f"- day_of_week: {dow_label}\n"
        f"- time_slot: {slot_label}\n"
        f"- occurrence_count: {routine.occurrence_count}\n"
        f"\n"
        f"## User HISTORY (truncated to last {_HISTORY_MAX_CHARS} chars)\n"
        f"{history_md}\n"
    )


class RoutineValidator:
    """Async, stateless Stage 1 validator.

    Construction takes a provider + model; ``validate`` makes one LLM call
    per candidate. Caller is responsible for caching results in
    routine_store (via ``Routine.llm_validation``) so we don't re-call.
    """

    def __init__(
        self,
        provider: "LLMProvider",
        model: str,
        *,
        temperature: float = _VALIDATOR_TEMPERATURE,
        max_tokens: int = _VALIDATOR_MAX_TOKENS,
    ) -> None:
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def validate(
        self,
        routine: Routine,
        history_md: str,
        *,
        now_ms: int,
    ) -> LLMValidation | None:
        """Return an LLMValidation or None on LLM/parse failure.

        Never raises. The caller decides whether to retry, skip, or
        treat a None result as a soft "no" for surfacing purposes.
        """
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(routine, history_md)},
        ]
        try:
            response = await self.provider.chat_with_retry(
                messages=messages,
                tools=[VALIDATOR_TOOL],
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )
        except Exception as exc:
            logger.warning(
                "RoutineValidator LLM call raised for {}: {}",
                routine.id,
                exc,
            )
            return None

        if response.finish_reason == "error":
            logger.warning(
                "RoutineValidator LLM error for {}: {}",
                routine.id,
                (response.content or "")[:200],
            )
            return None
        if not response.has_tool_calls:
            logger.warning(
                "RoutineValidator got no tool call for {}; content={!r}",
                routine.id,
                (response.content or "")[:120],
            )
            return None

        args = response.tool_calls[0].arguments
        if not isinstance(args, dict):
            logger.warning(
                "RoutineValidator tool args not a dict for {}: {!r}",
                routine.id,
                args,
            )
            return None

        try:
            is_routine = bool(args["is_routine"])
            confidence = float(args.get("confidence", 0.0))
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "RoutineValidator could not parse args for {}: {} args={!r}",
                routine.id,
                exc,
                args,
            )
            return None

        # max/min propagate NaN silently (max(0.0, nan) == nan); guard
        # so a NaN/inf from the LLM doesn't sink the floor comparison
        # downstream (NaN >= 0.6 is False, masking a "valid" verdict).
        if not math.isfinite(confidence):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        reason = str(args.get("reason", ""))[:300]
        return LLMValidation(
            is_routine=is_routine,
            confidence=confidence,
            reason=reason,
            validated_at_ms=now_ms,
        )


__all__ = ["RoutineValidator", "VALIDATOR_TOOL"]
