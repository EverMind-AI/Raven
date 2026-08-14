"""Was this turn cut off, and which call did it cut.

Lives beside the provider rather than in the agent loop: everything it reads --
tool calls, generation settings, the model catalogue -- belongs to this layer,
and the non-streaming caller has to reach it *before* returning, while its
tracing span is still open and while it still knows which model in a fallback
chain actually answered.

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
) -> tuple[int, bool]:
    """Decide whether this turn was cut, and refuse the calls it puts in doubt.

    Returns ``(ceiling_that_was_sent, truncated)``.

    Two things are asked, and only one of them is a guess.

    **Did the turn stop at the ceiling?** ``finish_reason == "length"``, or
    ``output_tokens`` reaching the ceiling we sent. Neither alone is enough:
    measured through openrouter, gpt-4o answers a ceiling hit with
    ``"tool_calls"`` -- a positive claim of success -- in 4 of 4 probes, with
    usage sitting exactly at the ceiling. So finish_reason is trusted when it
    speaks and never relied on when it does not.

    **Which call does that put in doubt?** The last one, because generation is
    sequential. This is where the guess lives: a turn can finish a call and
    then hit the ceiling in prose that follows it, and that call was fine. The
    guess is deliberately not refined further. Refusing a whole call costs one
    retry; dispatching a cut one writes half a file, or silently turns an
    append into an overwrite because the optional `mode` never arrived.

    A call whose arguments had to be repaired to parse is refused on its own
    evidence, wherever it sits -- see ``RunMeta.arguments_repaired``. That path
    needs no ceiling signal and makes no guess about position.
    """
    sent_max_tokens = send_max_tokens(generation, model)
    output_tokens = (usage or {}).get("completion_tokens")
    hit_ceiling = isinstance(output_tokens, int) and output_tokens >= sent_max_tokens
    truncated = finish_reason == "length" or hit_ceiling

    if truncated and tool_calls:
        last = tool_calls[-1]
        last.run_meta = replace(
            last.run_meta or RunMeta(),
            truncation=TruncationInfo(at_tokens=sent_max_tokens),
        )

    # Outside the branch above: a turn can be cut off with no tool call at all
    # -- a plain reply that ran past the ceiling -- and that is worth seeing in
    # the log even though there is nothing to refuse.
    if truncated:
        logger.warning(
            "LLM output truncated (finish_reason={}, output_tokens={}, max_tokens={})",
            finish_reason,
            output_tokens,
            sent_max_tokens,
        )
    return sent_max_tokens, truncated
