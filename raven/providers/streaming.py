"""Driving one ``provider.chat_stream`` call to a finished ``LLMResponse``.

Shared by the agent loop and by the sub-agent backend that streams a direct
chat reply: both run the same shape of call -- tools, reasoning, reconnects --
and a copy would drift on the first provider quirk fixed in one and not the
other.

Nothing here knows about a turn, a session, or an outlet. The caller supplies
the provider and the two delta callbacks; what it does with them is its own.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import aclosing
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.contracts.tool import RAW_ARGUMENTS_KEY
from raven.providers.base import ErrorClassification, LLMResponse, RunMeta, ToolCallRequest
from raven.providers.reasoning import split_orphan_think
from raven.providers.tool_names import normalized_tool_name
from raven.providers.transport_failure import flag_transport_failure, prompt_chars
from raven.providers.truncation import flag_truncation

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider


def _transport_failed(
    finish_reason: str | None,
    content_buf: list[str],
    reasoning_buf: list[str],
    tool_call_slots: list[dict[str, Any]],
    usage: dict[str, Any] | None,
    messages: list[dict[str, Any]],
) -> str | None:
    """Whether this attempt's stream was a request the upstream never processed.

    The same question the non-streaming response exit asks, on the same
    evidence, so one fault does not read differently inside the TUI than
    outside it.
    """
    return flag_transport_failure(
        # The upstream's own word, not a value synthesised for a stream that
        # ended without a terminal delta.
        finish_reason=finish_reason,
        content="".join(content_buf),
        reasoning="".join(reasoning_buf),
        tool_calls=_finalize_tool_calls(tool_call_slots),
        usage=usage,
        sent_chars=prompt_chars(messages),
    )


async def stream_llm_call(
    provider: "LLMProvider | Any",
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    model: str | None,
    on_token_delta: Callable[[str], Awaitable[None]] | None = None,
    on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
    max_reconnects: int = 1,
    retry_delays: Sequence[float] = (),
    retry_after_output: bool = False,
    **stream_kwargs: Any,
) -> LLMResponse:
    """Stream an LLM response via ``provider.chat_stream`` + accumulate to LLMResponse.

    When a turn caller wires ``on_token_delta``, its loop diverts here instead
    of to ``chat_with_retry``. Each non-empty content chunk fires the callback;
    tool_call fragments are merged positionally; the final response object is
    shape-compatible with what ``chat()`` would have returned.

    The tool-call merge is by ``index``: ``id`` and ``function.name`` are taken
    from the first fragment carrying them, and ``function.arguments`` is the
    concatenation of the per-fragment argument strings.

    A failure that already streamed deltas is not retried -- the caller has
    rendered them, so a second attempt would duplicate its output. Before the
    first delta there is nothing to duplicate, so a retryable error reconnects up
    to ``max_reconnects`` times at once, then waits out ``retry_delays`` -- the
    loop's own ladder, seconds to minutes, for a gateway that serves error pages
    for a while -- asking again after each wait. Once both are spent the exception
    propagates: a mid-turn provider error is the turn's failure (the lane emits
    TurnFailed), not a text reply about one. The one verdict handed back as an
    error response instead is ``strip_images``: the recovery for it is a change to
    the messages, which only the loop can make.

    ``stream_kwargs`` reaches ``chat_stream`` unchanged. It exists because that
    signature carries *literal* generation defaults rather than the provider's
    own ``generation`` settings, so a caller whose non-streaming path went
    through ``chat_with_retry`` has to pass them explicitly to keep the two
    paths answering under the same budget.
    """
    content_buf: list[str] = []
    reasoning_buf: list[str] = []
    tool_call_slots: list[dict[str, Any]] = []
    # How long the model thought, measured here because this is the only layer
    # that sees the deltas arrive. A browser clock cannot stand in for it: it
    # only exists while the page that watched the stream is open, so a reload
    # or a session switch has nothing left to read.
    think_t0: float | None = None
    reasoning_ms: int | None = None

    def stop_thinking() -> int | None:
        """Close the thinking clock at the first output that is not a thought."""
        nonlocal reasoning_ms
        if think_t0 is not None and reasoning_ms is None:
            reasoning_ms = int((time.monotonic() - think_t0) * 1000)
        return reasoning_ms

    final_usage: dict[str, Any] | None = None
    had_error = False
    error_content: str | None = None
    error_classification: ErrorClassification | None = None
    upstream_finish_reason: str | None = None

    for attempt in range(max_reconnects + len(retry_delays) + 1):
        # aclosing() guarantees the async generator (and its underlying stream)
        # is closed when an error from the per-chunk idle cap or the provider
        # unwinds the loop, so a stalled or broken stream never hangs or leaks
        # the connection - and a reconnect starts from a closed socket.
        try:
            async with aclosing(
                provider.chat_stream(messages=messages, tools=tools, model=model, **stream_kwargs)
            ) as stream:
                async for delta in stream:
                    if delta.finish_reason == "error":
                        # A non-streaming provider's chat() error, replayed
                        # through the fallback as its single terminal delta.
                        # Its content is the error text, not a token to render
                        # or accumulate -- surface it via error_classification
                        # instead of the normal success collation below.
                        had_error = True
                        error_content = delta.content
                        error_classification = delta.error_classification
                        if delta.usage is not None:
                            final_usage = delta.usage
                        # No reconnect for it: the fallback already spent
                        # chat()'s own retries, so the stream ends here and
                        # ``had_error`` answers for the call.
                        continue
                    if delta.finish_reason:
                        upstream_finish_reason = delta.finish_reason
                    reasoning_delta = getattr(delta, "reasoning_content", None)
                    if reasoning_delta:
                        if think_t0 is None:
                            think_t0 = time.monotonic()
                        reasoning_buf.append(reasoning_delta)
                        if on_reasoning_delta is not None:
                            await on_reasoning_delta(reasoning_delta)
                    if delta.content:
                        stop_thinking()
                        content_buf.append(delta.content)
                        if on_token_delta is not None:
                            await on_token_delta(delta.content)
                    if delta.tool_call_delta:
                        stop_thinking()
                        _merge_tool_call_fragments(
                            tool_call_slots,
                            delta.tool_call_delta,
                        )
                    if delta.usage is not None:
                        final_usage = delta.usage
            # Asked inside the attempt loop so the answer can be acted on. The
            # verdict requires that nothing was emitted, so a second attempt
            # duplicates no rendered output -- the same condition the reconnect
            # below already tests before retrying a mid-stream error. Returning
            # `finish_reason="error"` here instead would end the turn: this path
            # has no ladder of its own, and the loop breaks on an error response
            # before `classify_empty_response` can recover it.
            if attempt < max_reconnects and _transport_failed(
                upstream_finish_reason, content_buf, reasoning_buf, tool_call_slots, final_usage, messages
            ):
                logger.warning(
                    "upstream reported a failed call as a normal end (attempt {}/{}), reconnecting",
                    attempt + 1,
                    max_reconnects + 1,
                )
                content_buf.clear()
                reasoning_buf.clear()
                tool_call_slots.clear()
                final_usage = None
                upstream_finish_reason = None
                continue
            break
        except TimeoutError:
            # The idle cap already waited the full timeout; reconnecting would
            # double an already-long stall, so a stall ends the call. After output
            # it ends the turn too, unless the caller asked for a retry: handed back
            # as a retryable response, the loop's own ladder asked again and a
            # person watching the stream saw the words twice, which is the rule
            # the branch below holds and this one did not.
            if (content_buf or reasoning_buf or tool_call_slots) and not retry_after_output:
                raise
            return LLMResponse(
                content="".join(content_buf),
                finish_reason="error",
                error_classification=provider.classify_error(TimeoutError()),
            )
        except Exception as exc:
            # Every path out of here but one is a bare `raise` so the provider's
            # own exception reaches the caller unchanged: per N-TURNFAILED the turn
            # must fail (the lane emits TurnFailed) rather than resolve into a
            # "Sorry" text reply.
            emitted = bool(content_buf or reasoning_buf or tool_call_slots)
            if emitted and not retry_after_output:
                raise
            # Duck-typed providers need not implement classify_error; treat a
            # missing classifier as fatal so the real error surfaces instead of
            # an AttributeError raised from inside this handler.
            classify = getattr(provider, "classify_error", None)
            classification = classify(exc) if classify is not None else None
            if classification is None:
                raise
            if emitted and classification.retryable:
                # Asked for by an unattended caller: the words already streamed are
                # produced again, which its machine client does not mind, against a
                # turn with hours of work behind it ending on a dropped connection.
                logger.warning(
                    "Stream LLM error [{}] after {} chars of output; the caller asked to retry anyway: {}",
                    classification.category,
                    sum(len(part) for part in content_buf),
                    exc,
                )
                content_buf.clear()
                reasoning_buf.clear()
                tool_call_slots.clear()
                final_usage = None
                upstream_finish_reason = None
            if classification.strip_images:
                # The one recovery this function cannot make: the picture has to
                # come out of the messages it only reads. Handed back as the error
                # response the loop's strip-and-retry acts on; raising ended a
                # deck build on the first render an endpoint refused for its size,
                # and waiting would not have shrunk the bytes.
                return LLMResponse(
                    content=f"Error calling LLM ({classification.category}): {exc}",
                    finish_reason="error",
                    error_classification=classification,
                )
            if not classification.retryable:
                raise
            if attempt < max_reconnects:
                logger.warning(
                    "Stream LLM error [{}] before first delta (attempt {}/{}), reconnecting: {}",
                    classification.category,
                    attempt + 1,
                    max_reconnects + 1,
                    exc,
                )
                continue
            # The reconnects were spent on a failure that is still transient. The
            # provider's ladder is seconds long and right for a dropped connection;
            # a gateway serving error pages for a few minutes outlasts it, and one
            # measured deck build lost 62 minutes of work to a 40-second outage
            # here. So the loop's longer ladder is waited out before the turn fails.
            waited = attempt - max_reconnects
            if waited >= len(retry_delays):
                raise
            delay = retry_delays[waited]
            logger.warning(
                "Stream LLM error [{}] outlasted the reconnects; asking again in {:.0f}s (wait {}/{}): {}",
                classification.category,
                delay,
                waited + 1,
                len(retry_delays),
                exc,
            )
            await asyncio.sleep(delay)

    if had_error:
        return LLMResponse(
            content=error_content,
            finish_reason="error",
            error_classification=error_classification,
            usage=final_usage or {},
        )

    tool_calls = _finalize_tool_calls(tool_call_slots)

    # No ceiling is passed in: the main loop deliberately lets chat_stream's own
    # defaults stand (see the caller's docstring), so the number this turn
    # carried is not knowable here. Nothing is compared against it, so nothing
    # is missing.
    sent_max_tokens, truncated = flag_truncation(
        finish_reason=upstream_finish_reason,
        usage=final_usage,
        tool_calls=tool_calls,
    )

    # No terminal reason arrived means none may be fabricated: a reply that died
    # mid-stream has to stay distinguishable from one that finished.
    finish_reason = upstream_finish_reason or "unknown"

    # A call that emitted nothing but thought still thought for a measurable
    # time; the end of the stream is where that thought stopped.
    stop_thinking()

    content = "".join(content_buf)
    reasoning_content = "".join(reasoning_buf) or None
    # getattr because the loop accepts duck-typed providers (test stubs and
    # thin adapters implement just chat/chat_stream); absent means the
    # LLMProvider default, False.
    emits_unparsed = getattr(provider, "emits_unparsed_reasoning", None)
    if reasoning_content is None and emits_unparsed is not None and emits_unparsed():
        split_reasoning, content = split_orphan_think(content)
        reasoning_content = split_reasoning

    # No verdict here: it was asked inside the attempt loop, where a reconnect
    # is still possible. Once the reconnects are spent the response is returned
    # as what it is -- an empty one -- so the loop's own empty-response
    # recovery still owns it rather than being pre-empted by an error.
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=final_usage or {},
        reasoning_content=reasoning_content,
        truncated=truncated,
        max_tokens=sent_max_tokens,
        reasoning_ms=reasoning_ms,
    )


def generation_kwargs(provider: Any) -> dict[str, Any]:
    """The provider's own generation defaults, as ``chat_stream`` keywords.

    ``chat_stream`` declares ``max_tokens=4096, temperature=0.7`` as literals
    while ``chat_with_retry`` reads ``provider.generation``, so a caller that
    streams on one path and calls ``chat_with_retry`` on another would answer the
    same instance under two different budgets -- a reply truncated at 4096 tokens
    on the streaming path alone.

    Duck-typed: a provider stub without ``generation`` contributes nothing and
    the signature defaults stand.
    """
    generation = getattr(provider, "generation", None)
    if generation is None:
        return {}
    kwargs: dict[str, Any] = {}
    for field in ("max_tokens", "temperature", "reasoning_effort"):
        value = getattr(generation, field, None)
        if value is not None:
            kwargs[field] = value
    return kwargs


def _merge_tool_call_fragments(
    slots: list[dict[str, Any]],
    delta: dict[str, Any],
) -> None:
    """Merge a single chat_stream tool_call_delta into accumulator slots.

    Each slot follows the shape ``{id, function: {name, arguments_buf: [str]}}``.
    Per provider chunk semantics (OpenAI/LiteLLM): each tool call fragment
    carries an ``index`` field; ``id`` / ``function.name`` typically appear in
    the first fragment for that index, ``function.arguments`` is a JSON string
    streamed in pieces.

    Respects the ``index`` field so parallel multi-tool streams do not
    collapse into ``slots[0]``. Fragments without an ``index`` default to 0
    (single-tool case, backward-compatible).
    """
    incoming = delta.get("tool_calls") or []
    if not incoming:
        return
    for tc in incoming:
        idx = int(tc.get("index", 0) or 0)
        while len(slots) <= idx:
            slots.append({"id": None, "function": {"name": None, "arguments_buf": []}})
        slot = slots[idx]
        if tc.get("id") and not slot["id"]:
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name") and not slot["function"]["name"]:
            slot["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments_buf"].append(fn["arguments"])


def _finalize_tool_calls(slots: list[dict[str, Any]]) -> list[ToolCallRequest]:
    """Convert accumulator slots into final ToolCallRequest list."""
    result: list[ToolCallRequest] = []
    for slot in slots:
        # Normalised at this exit, the single place the streaming path turns an
        # accumulator into a call: a name that is nothing but whitespace comes
        # out empty and is dropped by the check below, which is what it is.
        name = normalized_tool_name(slot["function"]["name"])
        if not name:
            continue
        args_text = "".join(slot["function"]["arguments_buf"])
        repaired = False
        try:
            args = json.loads(args_text) if args_text else {}
        except json.JSONDecodeError:
            # Kept rather than dropped so the registry can quote the text back;
            # it is the only evidence of what the model actually emitted.
            args = {RAW_ARGUMENTS_KEY: args_text}
            repaired = True
        result.append(
            ToolCallRequest(
                id=slot["id"] or "",
                name=name,
                arguments=args,
                # Flagged on the call, not the turn: this is the one truncation
                # signal that needs no cooperation from the backend, and the
                # streaming path assembles its calls here, where no provider is
                # left to attach a conclusion to.
                run_meta=RunMeta(arguments_repaired=True) if repaired else None,
            )
        )
    return result


__all__ = ["generation_kwargs", "stream_llm_call"]
