"""Empty-response recovery for the agent loop.

Pure decision logic, no I/O — the loop owns the side effects (appending
messages, incrementing counters, ``continue``). Kept separate from the loop so
the branching can be unit-tested in isolation, mirroring the repo's other small
policy units (nudge_policy, decision_router).

A turn that ends with no visible text would otherwise surface a canned
"no response to give" dud — a zero-score turn on weaker models. This recovers
the turn before giving up, in three bounded modes:

  PREFILL  thinking-only — the model emitted only reasoning (a structured field
           or an inline <think> block) and no body. Re-feed its own reasoning so
           it continues into the answer.
  NUDGE    post-tool empty — the model ran a tool then returned nothing. Inject a
           short user nudge so it processes the tool result.
  RETRY    plain empty — re-request as-is.

This is distinct from Sentinel's NudgeInjector / NudgePolicy, which inject
*proactive suggestions* onto an outbound reply; this module instead recovers an
empty turn before it is ever sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto

from raven.providers.base import LLMResponse

# In-content thinking markers. Some models (Ollama, certain Qwen gateways) put
# the reasoning in ``content`` as <think>…</think> rather than in the structured
# reasoning_content field, so a content scan is required — checking only the
# structured fields would miss them.
_THINK_TAG_RE = re.compile(r"<think>|<thinking>|<reasoning>", re.IGNORECASE)

POST_TOOL_NUDGE = (
    "You executed tool calls but returned an empty response. Use the tool "
    "results above to continue the task, or give your final answer now."
)


class RecoveryAction(Enum):
    """What the loop should do about an empty assistant response."""

    COMPLETE = auto()  # visible text present, or budgets spent → finish the turn
    PREFILL = auto()  # thinking-only → re-feed reasoning, re-request
    NUDGE = auto()  # post-tool empty → inject (empty) + user nudge, re-request
    RETRY = auto()  # plain empty → re-request as-is


@dataclass(frozen=True)
class RecoveryLimits:
    """Per-turn retry budgets."""

    enabled: bool = True
    post_tool_empty_max_nudges: int = 1
    thinking_prefill_max_retries: int = 2
    empty_content_max_retries: int = 3


def limits_from_defaults(defaults: object) -> RecoveryLimits:
    """Build limits from an ``agents.defaults`` config object (duck-typed).

    Centralizes the config→RecoveryLimits mapping so the several AgentLoop
    construction sites don't each repeat the field plumbing.
    """
    return RecoveryLimits(
        enabled=getattr(defaults, "empty_recovery_enabled", True),
        post_tool_empty_max_nudges=getattr(defaults, "post_tool_empty_max_nudges", 1),
        thinking_prefill_max_retries=getattr(defaults, "thinking_prefill_max_retries", 2),
        empty_content_max_retries=getattr(defaults, "empty_content_max_retries", 3),
    )


def has_inline_thinking(content: str | None) -> bool:
    """True when raw content carries a <think>/<thinking>/<reasoning> marker."""
    return bool(content) and bool(_THINK_TAG_RE.search(content))


def has_thinking(response: LLMResponse) -> bool:
    """True when the response produced reasoning in any form (structured or inline)."""
    return bool(response.reasoning_content or response.thinking_blocks or has_inline_thinking(response.content))


def classify_empty_response(
    response: LLMResponse,
    visible: str | None,
    *,
    prev_had_tool_calls: bool,
    nudges_done: int,
    prefill_retries: int,
    empty_retries: int,
    limits: RecoveryLimits,
) -> RecoveryAction:
    """Decide how to handle a no-tool-call assistant response.

    ``visible`` is ``response.content`` after stripping <think> blocks — i.e. the
    user-facing text. Non-empty ``visible`` (or recovery disabled) means the turn
    is done.

    Ordering puts PREFILL before NUDGE so a thinking-only response is continued
    via prefill rather than spending the post-tool nudge on it; the
    ``not thinking`` guard on NUDGE keeps them mutually exclusive.
    """
    if visible or not limits.enabled:
        return RecoveryAction.COMPLETE

    thinking = has_thinking(response)

    # thinking-only prefill — the model reasoned but produced no body.
    if thinking and prefill_retries < limits.thinking_prefill_max_retries:
        return RecoveryAction.PREFILL

    # post-tool empty nudge — exclude thinking-only (handled above).
    if prev_had_tool_calls and not thinking and nudges_done < limits.post_tool_empty_max_nudges:
        return RecoveryAction.NUDGE

    # Fallback plain retry. The ``prefill_exhausted`` clause is load-bearing:
    # some models (e.g. mimo-v2-pro via OpenRouter) always populate a reasoning
    # field, so gating retry on ``not thinking`` alone would permanently block
    # retries for every reasoning model once prefill is spent.
    prefill_exhausted = thinking and prefill_retries >= limits.thinking_prefill_max_retries
    if empty_retries < limits.empty_content_max_retries and (not thinking or prefill_exhausted):
        return RecoveryAction.RETRY

    return RecoveryAction.COMPLETE
