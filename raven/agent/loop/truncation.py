"""Was this turn cut off, and which call did it cut.

Both response paths ask this. The streaming path assembles the reply chunk by
chunk; the non-streaming one gets it whole. What they can observe differs --
only the streaming path sees whether a tool call's JSON failed to parse, since
the non-streaming parser repairs it before the loop is handed the result -- but
the decision must not, or a turn would be reported as truncated on one path and
clean on the other.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from loguru import logger

from raven.providers.base import RunMeta, ToolCallRequest, TruncationInfo, send_max_tokens


def flag_truncation(
    generation: Any,
    *,
    model: str | None,
    finish_reason: str | None,
    usage: dict[str, Any] | None,
    tool_calls: list[ToolCallRequest],
    cut_inside_tool_call: bool | None = None,
) -> tuple[int, bool]:
    """Decide truncation, mark the calls it applies to, and log it.

    Returns ``(ceiling_that_was_sent, truncated)``.

    Signals come at two granularities, and conflating them marks the wrong
    call:

      **response-level** -- ``finish_reason == "length"``, or ``output_tokens``
      reaching the ceiling. Both say "this turn was cut", not which call. The
      cut lands on the last one, because generation is sequential.

      **call-level** -- a call whose arguments had to be repaired to parse.
      That is evidence about that call, wherever it sits in the turn. A model
      writing malformed JSON in its first of three calls would otherwise get
      the third one refused and the first one dispatched.

      Where it sits is itself evidence. Only the last call can have been cut,
      because a cut leaves no later calls to arrive, so a repair anywhere else
      is the model writing bad JSON no matter what the turn-level signals say.

    ``finish_reason`` alone is not enough to rest on: measured through
    openrouter, gpt-4o answers a ceiling hit with ``"tool_calls"`` -- not a
    clean stop but a positive claim of success -- in 4 of 4 probes, with usage
    sitting exactly at the ceiling.

    ``cut_inside_tool_call`` says whether the stream was still writing tool
    arguments when it stopped. A turn can hit the ceiling in prose that follows
    a complete call, and marking that call would refuse a good one. Only the
    streaming path can answer: deltas arrive in generation order, while the
    non-streaming response has already been flattened into separate `content`
    and `tool_calls` fields with no record of which came last. ``None`` means
    "cannot tell", and is treated as "assume it was cut" -- one wasted retry
    is cheaper than dispatching a call whose arguments are incomplete.

    A repaired call is left with its observation and no ``truncation`` unless a
    response-level signal also fired, and the returned flag stays False in that
    case: bad JSON is not a ceiling hit and must not be recorded as one. The
    registry refuses both, and tells them apart -- a call that was cut is asked
    to resend in pieces, one that was malformed to resend it well-formed.
    """
    sent_max_tokens = send_max_tokens(generation, model)
    output_tokens = (usage or {}).get("completion_tokens")
    hit_ceiling = isinstance(output_tokens, int) and output_tokens >= sent_max_tokens
    response_level = finish_reason == "length" or hit_ceiling
    repaired = [tc for tc in tool_calls if tc.run_meta and tc.run_meta.arguments_repaired]
    # Only the response-level signals mean "this turn was cut". A repair on its
    # own means the model wrote bad JSON, which the registry refuses just the
    # same but reports differently -- and which must not be recorded as a
    # ceiling hit in tracing.
    truncated = response_level

    # Only the last call can be the cut one, so only it can be re-read as
    # truncated. An earlier call whose arguments failed to parse was written
    # badly by the model -- a cut there would have left no later calls to
    # arrive. Marking those as truncated would tell a model to resend in
    # pieces something that was never too long.
    if response_level and tool_calls and cut_inside_tool_call is not False:
        last = tool_calls[-1]
        last.run_meta = replace(
            last.run_meta or RunMeta(),
            truncation=TruncationInfo(at_tokens=sent_max_tokens),
        )

    if truncated:
        logger.warning(
            "LLM output truncated (finish_reason={}, output_tokens={}, max_tokens={}, repaired_calls={})",
            finish_reason,
            output_tokens,
            sent_max_tokens,
            len(repaired),
        )
    return sent_max_tokens, truncated
