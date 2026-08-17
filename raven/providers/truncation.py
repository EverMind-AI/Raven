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

from raven.providers.base import RunMeta, ToolCallRequest, TruncationInfo


def flag_truncation(
    *,
    finish_reason: str | None,
    usage: dict[str, Any] | None,
    tool_calls: list[ToolCallRequest],
    sent: int | None = None,
) -> tuple[int | None, bool]:
    """Decide whether this turn was cut, and refuse the calls it puts in doubt.

    Returns ``(ceiling_that_was_sent, turn_was_cut)``.

    Two pieces of evidence, and neither is arithmetic.

    **The upstream says so** -- ``finish_reason == "length"``. Reliable when it
    speaks and not to be waited on: gpt-4o answers a ceiling hit with
    ``"tool_calls"``, a positive claim of success, in 4 of 4 probes through
    openrouter. It underreports rather than overreports, so it is taken at its
    word and never required.

    **The last call's arguments did not parse.** Generation is sequential, so a
    cut leaves nothing after it -- a repair on the *last* call means the turn
    ended while writing it. A repair on any earlier call cannot be that, since
    calls arrived after it, so it stays plain bad JSON. Measured over four
    realistic argument blobs, every one of 323 cut points that drops a field
    leaves JSON that will not parse, because JSON is only complete once the
    object closes and closing it means nothing was lost.

    Which call is refused is the same answer either way: the last one. A turn
    can finish a call and then hit the limit in the prose after it, so that is
    an inference, deliberately not refined. Refusing a whole call costs one
    retry; dispatching a cut one writes half a file, or silently turns an
    append into an overwrite because the optional `mode` never arrived.

    ``sent`` is the ceiling the request carried, when the caller knows it, and
    is used only to name a number in the message. Nothing is compared against
    it and none is derived when it is absent -- which is why this function no
    longer needs the generation settings or the model id at all. A comparison
    would have to agree with the request on both a number and a model id, and
    that coupling is what broke five times on this branch. No surveyed agent
    (LiteLLM, OpenClaw, opencode, hermes-agent) carries such a comparison.
    """
    truncated = finish_reason == "length"

    last = tool_calls[-1] if tool_calls else None
    last_unparsed = bool(last is not None and last.run_meta and last.run_meta.arguments_repaired)

    if last is not None and truncated:
        last.run_meta = replace(last.run_meta or RunMeta(), truncation=TruncationInfo(at_tokens=sent))
    elif last_unparsed:
        # Not recorded as a truncation: the two facts are that the arguments did
        # not parse and that nothing arrived after this call. Reading them as a
        # cut is what the registry's message does, hedged, because a model can
        # also just write bad JSON -- and a record that asserted it would be
        # claiming more than the sentence it produces.
        last.run_meta = replace(last.run_meta, last_of_turn=True)

    # Outside the branch above: a turn can be cut off with no tool call at all
    # -- a plain reply that ran past the limit -- and that is worth seeing in
    # the log even though there is nothing to refuse. It is also the one shape
    # neither piece of evidence covers when the upstream stays quiet, since
    # there are no arguments to fail to parse.
    if truncated or last_unparsed:
        logger.warning(
            "LLM output truncated (finish_reason={}, output_tokens={}, max_tokens={}, unparsed_last_call={})",
            finish_reason,
            (usage or {}).get("completion_tokens"),
            sent,
            last_unparsed,
        )
    return sent, truncated
