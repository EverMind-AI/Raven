"""Was this turn cut off, and which call did it cut.

Both response paths ask this. The streaming path assembles the reply chunk by
chunk; the non-streaming one gets it whole. What they can observe differs --
only the streaming path sees whether a tool call's JSON failed to parse, since
the non-streaming parser repairs it before the loop is handed the result -- but
the decision must not, or a turn would be reported as truncated on one path and
clean on the other.
"""

from __future__ import annotations

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
    args_parse_failed: bool = False,
) -> tuple[int, bool]:
    """Decide truncation, mark the call that was cut, and log it.

    Returns ``(ceiling_that_was_sent, truncated)``.

    Three independent signals, OR'd. They cover different failure shapes and
    none subsumes the others, so a miss by one is not a miss overall:

      ``finish_reason == "length"``  costs nothing to trust, but some backends
                                     report a clean stop on a truncated reply
      ``output_tokens`` vs ceiling   survives a lying finish_reason, and the
                                     ceiling is always known because we are the
                                     ones who sent it
      ``args_parse_failed``          computed locally from what actually
                                     arrived, but exists only on a tool-call
                                     turn, and only where the caller can see
                                     the raw fragments

    Deliberately permissive: a false positive costs the model one extra
    sentence it can weigh against what it just wrote. A false negative costs a
    retry loop -- the model re-sends the same oversized payload, is told again
    that a field is missing, and never learns the real reason.
    """
    sent_max_tokens = send_max_tokens(generation, model)
    output_tokens = (usage or {}).get("completion_tokens")
    hit_ceiling = isinstance(output_tokens, int) and output_tokens >= sent_max_tokens
    truncated = finish_reason == "length" or hit_ceiling or args_parse_failed

    if truncated and tool_calls:
        # The last call, and not only the ones whose JSON failed to parse: when
        # the transport closes the braces for us the blob parses cleanly and
        # simply lacks whatever the model had not reached yet, which schema
        # validation then reports as a missing field. That message is true and
        # useless -- it sends the model looking for a field it never omitted.
        # Earlier calls need no marker: calls arrive in order, so each of them
        # finished before the ceiling was reached.
        tool_calls[-1].run_meta = RunMeta(truncation=TruncationInfo(at_tokens=sent_max_tokens))

    if truncated:
        logger.warning(
            "LLM output truncated (finish_reason={}, output_tokens={}, max_tokens={}, tool_args_incomplete={})",
            finish_reason,
            output_tokens,
            sent_max_tokens,
            args_parse_failed,
        )
    return sent_max_tokens, truncated
