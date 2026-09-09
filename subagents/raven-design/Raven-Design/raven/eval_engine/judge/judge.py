"""LLM judge — answers "did this turn complete?" with a verdict tag.

The judge is intentionally minimal: one prompt, one LLM call, three
possible verdicts. Real production use will deepen the rubric
(per-dimension scores, structured failure reasons) as a follow-up.

Design choices:

- **Verdict enum, not free text.** ``JudgeVerdict`` is a string enum so
  the rest of the engine (adapter, hook decisions) can branch on it
  cleanly without LLM-prose parsing in the hot path.
- **Pluggable provider.** The judge takes any object exposing
  ``chat_with_retry`` — production wires it to the same ``LLMProvider``
  AgentLoop holds, tests pass an ``AsyncMock`` with a canned response.
- **Timeout + safe fallback.** ``asyncio.wait_for`` enforces the
  config's ``judge_timeout_seconds``; any timeout / exception /
  unparseable response falls back to ``JudgeVerdict.unknown`` so the
  hook can short-circuit to pass-through cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any

from raven.eval_engine.prompts.task_completion import TASK_COMPLETION_PROMPT

logger = logging.getLogger(__name__)


class JudgeVerdict(str, Enum):
    """Three-state verdict returned by ``EvalJudge.judge``.

    Encoded as a string enum so the values serialize cleanly into
    ``case.md`` frontmatter and ``behaviors.md`` entries via the
    adapter.
    """

    completed = "completed"  # User goal addressed; turn ended cleanly.
    failed = "failed"  # Visible error / refusal / missed objective.
    unknown = "unknown"  # Indeterminate (timeout, parse failure,
    # judge disabled, ambiguous turn).


class EvalJudge:
    """Single-call LLM judge over a turn's final response."""

    def __init__(
        self,
        provider: Any,
        *,
        model: str = "claude-haiku-4-5",
        timeout_seconds: float = 8.0,
    ) -> None:
        self._provider = provider
        self._model = model
        self._timeout = timeout_seconds

    async def judge(
        self,
        user_goal: str,
        final_response: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> JudgeVerdict:
        """Run one judge call and return the parsed verdict.

        ``user_goal`` is the original user message that opened this
        turn; ``final_response`` is whatever AgentLoop is about to
        return as the reply content. ``messages`` is the
        optional full message stream — currently unused but threaded
        through so a deeper rubric can land without changing the
        signature.

        Returns ``JudgeVerdict.unknown`` on any error path. Callers
        should treat ``unknown`` as "no signal" rather than "failed".
        """
        prompt = TASK_COMPLETION_PROMPT.format(
            user_goal=user_goal,
            final_response=final_response or "(no response produced)",
        )

        try:
            response = await asyncio.wait_for(
                self._provider.chat_with_retry(
                    messages=[
                        {"role": "system", "content": "You are an evaluation judge."},
                        {"role": "user", "content": prompt},
                    ],
                    model=self._model,
                    max_tokens=64,
                    temperature=0.0,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            logger.debug("EvalJudge: timed out after %.1fs", self._timeout)
            return JudgeVerdict.unknown
        except Exception as exc:  # noqa: BLE001 — judge must never crash AgentLoop
            logger.debug("EvalJudge: provider raised %s: %s", type(exc).__name__, exc)
            return JudgeVerdict.unknown

        text = (getattr(response, "content", "") or "").strip().lower()
        for verdict in JudgeVerdict:
            if verdict.value in text:
                return verdict
        return JudgeVerdict.unknown


__all__ = ["EvalJudge", "JudgeVerdict"]
