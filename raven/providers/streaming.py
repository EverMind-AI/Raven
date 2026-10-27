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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.contracts.tool import RAW_ARGUMENTS_KEY
from raven.providers.base import (
    CallRecord,
    ErrorClassification,
    LLMResponse,
    RunMeta,
    ToolCallRequest,
    canonical_llm_error,
    format_llm_error,
    send_max_tokens,
)
from raven.providers.reasoning import split_orphan_think
from raven.providers.tool_names import normalized_tool_name
from raven.providers.transport_failure import (
    flag_transport_failure,
    may_fold_to_nothing,
    prompt_chars,
    transport_failure_message,
    without_sentinels,
)
from raven.providers.truncation import flag_truncation
from raven.providers.usage import merge_usage

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider


#: Finish reasons that are the upstream's whole verdict on a call. The ``stop``
#: that closes a stream after one only says the stream is over.
DECLINED_FINISH_REASONS = frozenset({"refusal", "content_filter"})

#: The finish of an answer handed back from a stream that died; ``unknown`` is what a clean close without one reads.
STREAM_CUT = "stream_cut"


def _finish_after(held: str | None, incoming: str) -> str:
    """The finish reason a stream stands on once ``incoming`` has arrived."""
    return held if held in DECLINED_FINISH_REASONS else incoming


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


def _spent(
    verdict: ErrorClassification | None,
    provider: "LLMProvider | Any",
    content: str | None,
) -> ErrorClassification | None:
    """The same verdict with its asking-again used up: not retryable, and not worth a fallback model.

    Both put the same step to a model a second time, and a verdict is spent
    because something of the first attempt has already reached a watcher -- an
    attempt on this model or on another draws it twice. Kept as a verdict rather
    than raised so the recoveries reading its other flags, which repair the
    request itself -- shrink the window, take the pictures out -- still run. A
    response that carries none is classified here first: the caller classifies
    the error text itself when the verdict is missing, and would read a retryable
    failure straight back out of it.
    """
    if verdict is None:
        classify = getattr(provider, "classify_error", None)
        verdict = classify(content=content or None) if classify is not None else None
    return replace(verdict, retryable=False, should_fallback=False) if verdict is not None else None


def _split_unparsed_thought(
    provider: "LLMProvider | Any", content: str, reasoning_content: str | None
) -> tuple[str, str | None]:
    """The reply and the thought, for a backend that leaves its thinking unmarked.

    ``getattr`` because the loop accepts duck-typed providers (test stubs and
    thin adapters implement just chat/chat_stream); absent means the
    LLMProvider default, False.
    """
    emits_unparsed = getattr(provider, "emits_unparsed_reasoning", None)
    if reasoning_content is None and emits_unparsed is not None and emits_unparsed():
        reasoning_content, content = split_orphan_think(content)
    return content, reasoning_content


@dataclass(frozen=True)
class _Produced:
    """What a stream attempt had produced when it ended after its output reached the reader.

    The state lives in ``stream_llm_call``'s own variables, and the three exits that
    can hand the answer back instead of failing need all of it; one value keeps their
    call sites from each taking a dozen arguments.
    """

    provider: "LLMProvider | Any"
    content_buf: list[str]
    reasoning_buf: list[str]
    tool_call_slots: list[dict[str, Any]]
    on_token_delta: Callable[[str], Awaitable[None]] | None
    content_released: bool
    usage: dict[str, Any] | None
    thinking_blocks: list[dict[str, Any]] | None
    record: CallRecord | None
    reasoning_ms: int | None
    hand_back: bool
    finish_reason: str | None = None


async def _hand_back(produced: _Produced, cause: str) -> LLMResponse | None:
    """The answer a stream died in, as a ``STREAM_CUT`` reply -- or None where there is none to hand back.

    What the reader has seen of it is all a failure leaves, and a caller that can
    ask the model to go on from there (``hand_back``) is better served by it than by
    the failure. Only an answer counts: thought alone, a sentinel, or words beside a
    tool call still in the making stay the failure they were. The upstream sent no
    finish, and the reply says it was cut rather than ``unknown``: that is also what a
    stream that closed cleanly without a terminal chunk reads, and such a stream is a
    whole answer. A refusal the upstream had already sent is its whole verdict, so the
    reply keeps that finish instead: the words are its explanation, not an answer to go
    on with.

    An opening still held back, because it could have been a sentinel, is shown
    first, exactly as the end of a stream shows one: the continuation that follows
    has to land after words the reader can see.
    """
    if not produced.hand_back or produced.tool_call_slots:
        return None
    raw = "".join(produced.content_buf)
    content, reasoning_content = _split_unparsed_thought(
        produced.provider, raw, "".join(produced.reasoning_buf) or None
    )
    content = without_sentinels(content)
    if not content:
        return None
    if produced.on_token_delta is not None and not produced.content_released:
        await produced.on_token_delta(raw)
    logger.warning(
        "the stream died after {} chars of its answer; handing them back as a cut reply: {}",
        len(content),
        cause[:160],
    )
    return LLMResponse(
        content=content,
        finish_reason=_finish_after(produced.finish_reason, STREAM_CUT),
        usage=produced.usage or {},
        reasoning_content=reasoning_content,
        thinking_blocks=produced.thinking_blocks,
        reasoning_ms=produced.reasoning_ms,
        call_record=produced.record,
    )


async def _cut_or_raise(exc: Exception, produced: _Produced) -> LLMResponse:
    """The exit for a stream that raised after its output reached the reader and was not to be asked again.

    Hands back the answer when there is one (:func:`_hand_back`), and otherwise raises
    ``exc`` itself, so the provider's own exception reaches the caller unchanged as it
    did through the bare ``raise`` this stands in for. A function and not a branch
    because ``stream_llm_call`` is at the complexity ceiling, and both raising exits
    need the same answer.
    """
    partial = await _hand_back(produced, f"{type(exc).__name__}: {exc}")
    if partial is None:
        raise exc
    return partial


async def _answer_to_error_delta(
    produced: _Produced,
    *,
    error_content: str | None,
    error_classification: ErrorClassification | None,
    after_output: bool,
) -> LLMResponse:
    """The reply for a stream whose terminal delta reported the failure instead of raising it.

    ``after_output``: something of the attempt had reached the reader and the caller
    did not ask for a retry. The answer is handed back when there is one; otherwise
    the verdict is spent here, the last place that knows something was rendered. A
    provider that swallows a mid-stream failure into its terminal delta reports it
    as retryable, and the caller's own ladder would ask the same question again --
    with words already on the reader's screen, the answer drawn from the top a
    second time.
    """
    if after_output:
        partial = await _hand_back(produced, (error_content or "")[:160])
        if partial is not None:
            return partial
        error_classification = _spent(error_classification, produced.provider, error_content)
        logger.warning(
            "Stream LLM error [{}] after {} chars of output; not asking again: {}",
            error_classification.category if error_classification is not None else "unclassified",
            sum(len(part) for part in produced.content_buf),
            (error_content or "")[:160],
        )
    return LLMResponse(
        content=error_content,
        finish_reason="error",
        error_classification=error_classification,
        usage=produced.usage or {},
        call_record=produced.record,
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
    hand_back_partial: bool = False,
    **stream_kwargs: Any,
) -> LLMResponse:
    """Stream an LLM response via ``provider.chat_stream`` + accumulate to LLMResponse.

    When a turn caller wires ``on_token_delta``, its loop diverts here instead
    of to ``chat_with_retry``. Each non-empty content chunk fires the callback,
    except that the opening of a reply is held back while it could still be one
    that is only a sentinel, so a reply the response returns empty is never shown;
    tool_call fragments are merged positionally; the final response object is
    shape-compatible with what ``chat()`` would have returned.

    The tool-call merge is by ``index``: ``id`` and ``function.name`` are taken
    from the first fragment carrying them, and ``function.arguments`` is the
    concatenation of the per-fragment argument strings.

    A failure that already streamed deltas is not retried -- the caller has
    rendered them, so a second attempt would duplicate its output. The rule holds
    however the failure arrives: a provider that hands one back as its terminal
    error delta instead of raising has that delta's verdict spent of its
    retryability, so the caller's own ladder does not ask again either. What the
    reader has seen is then all a failure leaves, and a caller that can ask the
    model to go on from it (``hand_back_partial``) is handed those words as a reply
    whose finish reason is ``stream_cut``, not the failure -- for a raise, a stall and
    a terminal error delta alike. Only an answer counts: thought alone, a sentinel,
    or words beside a tool call still in the making fail as before, and a caller
    that asked to ``retry_after_output`` keeps the retry of the whole call. Before the
    first delta there is nothing to duplicate, so a retryable error reconnects up
    to ``max_reconnects`` times at once, then waits out ``retry_delays`` -- the
    loop's own ladder, seconds to minutes, for a gateway that serves error pages
    for a while -- asking again after each wait. Once both are spent the exception
    propagates: a mid-turn provider error is the turn's failure (the lane emits
    TurnFailed), not a text reply about one. Whether a failure that is not handed
    back comes back as an error response (``finish_reason="error"`` with its
    classification) or raises follows from how it arrives. One that arrives
    without an exception has nothing to raise and always comes back: the
    provider's terminal error delta, its verdict spent when output had already
    reached the reader; a stream the upstream closed before its terminal chunk
    with nothing deliverable in it, a network failure for the loop to wait out on
    its own ladder -- retryable unless a thought had already reached the watcher;
    and a normal end that carries the evidence of a request the upstream never
    processed, once the reconnects are spent (``upstream_transport_failure``,
    retryable). One that is raised propagates, unless its recovery belongs to the
    caller and nothing has reached the reader yet (or the caller asked to
    ``retry_after_output``): a stall, which this function does not ask again
    because the idle cap has already waited the full timeout, and a
    ``strip_images`` verdict, whose recovery is a change to the messages that
    only the loop can make.

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
    # Whether this attempt's content has gone out through ``on_token_delta``.
    # Until it has, ``content_buf`` is the part held back.
    content_released = False

    def stop_thinking() -> int | None:
        """Close the thinking clock at the first output that is not a thought."""
        nonlocal reasoning_ms
        if think_t0 is not None and reasoning_ms is None:
            reasoning_ms = int((time.monotonic() - think_t0) * 1000)
        return reasoning_ms

    def rendered() -> bool:
        """Whether asking again would show a watcher the same words twice.

        Only what went out through a delta callback counts. A thought reaches a
        watcher solely when the caller wired ``on_reasoning_delta``, and the
        fragments of a tool call are never rendered as the reply at all -- a
        second attempt replaces them. Counting either as output made a stall
        during a long silent think, or midway through a large tool argument,
        unretryable for a turn that had put nothing on screen.
        """
        return bool(content_buf) or bool(reasoning_buf and on_reasoning_delta is not None)

    def reset_attempt() -> None:
        """Drop what the attempt that just failed left behind, before asking again.

        Every retry path goes through here. While `rendered()` was the same test as
        `any buffer is non-empty`, a path that did not clear could not be reached
        with anything in them; now that a silent think retries, a slot left behind
        merges with the next attempt's fragments into a call the model never made,
        and a thinking block keeps a signature belonging to a generation that no
        longer exists.
        """
        nonlocal final_usage, thinking_blocks, record, upstream_finish_reason
        nonlocal think_t0, reasoning_ms, content_released
        content_buf.clear()
        content_released = False
        reasoning_buf.clear()
        tool_call_slots.clear()
        final_usage = None
        thinking_blocks = None
        record = None
        upstream_finish_reason = None
        # Left standing, the clock spans the ladder's waits and reports a minute
        # of sleep as a minute of thought -- the only evidence of how long a
        # round that showed nothing actually thought for.
        think_t0 = None
        reasoning_ms = None

    final_usage: dict[str, Any] | None = None
    thinking_blocks: list[dict[str, Any]] | None = None
    # The transport facts of the stream currently open. Latest wins: the provider
    # sends them once when the stream opens, and again with the retained chunks
    # attached when the stream turns out to have delivered nothing.
    record: CallRecord | None = None
    had_error = False
    error_content: str | None = None
    error_classification: ErrorClassification | None = None
    upstream_finish_reason: str | None = None
    transport_evidence: str | None = None

    def produced() -> _Produced:
        """The attempt that is ending, as the exits that hand its answer back read it."""
        return _Produced(
            provider,
            content_buf,
            reasoning_buf,
            tool_call_slots,
            on_token_delta,
            content_released,
            final_usage,
            thinking_blocks,
            record,
            reasoning_ms,
            hand_back_partial,
            upstream_finish_reason,
        )

    for attempt in range(max_reconnects + len(retry_delays) + 1):
        attempt_t0 = time.monotonic()
        finish_made_up = False
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
                        if getattr(delta, "call_record", None) is not None:
                            record = delta.call_record
                        if delta.usage is not None:
                            final_usage = {
                                **(final_usage or {}),
                                **{k: v for k, v in delta.usage.items() if v is not None},
                            }
                        # No reconnect for it: the fallback already spent
                        # chat()'s own retries, so the stream ends here and
                        # ``had_error`` answers for the call.
                        continue
                    if delta.finish_reason:
                        if getattr(delta, "finish_synthesized", False):
                            finish_made_up = True
                        else:
                            upstream_finish_reason = _finish_after(upstream_finish_reason, delta.finish_reason)
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
                            if content_released:
                                await on_token_delta(delta.content)
                            else:
                                held = "".join(content_buf)
                                if not may_fold_to_nothing(held):
                                    content_released = True
                                    await on_token_delta(held)
                    if delta.tool_call_delta:
                        stop_thinking()
                        _merge_tool_call_fragments(
                            tool_call_slots,
                            delta.tool_call_delta,
                        )
                    if delta.usage is not None:
                        final_usage = merge_usage(final_usage, delta.usage)
                    if getattr(delta, "thinking_blocks", None):
                        thinking_blocks = delta.thinking_blocks
                    # getattr for the same reason thinking_blocks uses one: a
                    # duck-typed provider need not carry the field at all.
                    if getattr(delta, "call_record", None) is not None:
                        record = delta.call_record
            # The upstream closed the stream before its terminal chunk and nothing
            # deliverable had arrived: a reply cut mid-thought, not an empty reply
            # the model finished. Measured on 2026-09-06: nine deck-build calls
            # ended this way at 307-314 s each with 11-15k reasoning tokens and no
            # content, and the empty-reply recovery re-sent the same request
            # five more times because the cut looked like a finished reply. Handed
            # back as a retryable network error instead, so the loop's error
            # ladder owns it and the log says what happened. Content that did
            # arrive is delivered as it is, with the finish reason left unknown.
            if finish_made_up and not content_buf and not tool_call_slots and not had_error:
                elapsed = time.monotonic() - attempt_t0
                reasoning_chars = sum(len(part) for part in reasoning_buf)
                logger.warning(
                    "the stream ended without the upstream's terminal chunk after {:.0f}s "
                    "({} chars of reasoning, no content or tool call); treating it as a transport failure",
                    elapsed,
                    reasoning_chars,
                )
                stop_thinking()
                cut_verdict: ErrorClassification | None = ErrorClassification(
                    "network", retryable=True, should_fallback=True
                )
                if rendered() and not retry_after_output:
                    # The thought is already on the watcher's screen, so the
                    # caller's ladder asking again would draw a second one. The
                    # same rule the raising exits below hold, spent here because
                    # this exit hands the failure back rather than raising.
                    cut_verdict = _spent(cut_verdict, provider, None)
                    logger.warning("the cut stream had already rendered its reasoning; not asking again")
                # In the canonical error shape, so the readers of that shape (the
                # loop's failure report, the CLI's diagnosis) keep this account.
                return LLMResponse(
                    content=canonical_llm_error(
                        "network",
                        None,
                        f"the model's reply was cut off by the connection after {elapsed:.0f}s, "
                        f"before any content arrived ({reasoning_chars} chars of reasoning were lost)",
                    ),
                    finish_reason="error",
                    error_classification=cut_verdict,
                    usage=final_usage or {},
                    reasoning_ms=reasoning_ms,
                    call_record=record,
                )
            # Asked inside the attempt loop so the answer can be acted on. The
            # verdict requires that nothing was emitted, so a second attempt
            # duplicates no rendered output -- the same condition the reconnect
            # below already tests before retrying a mid-stream error. A
            # reconnect is the cheap answer and is tried while attempts remain;
            # once they are spent the verdict is handed back as a retryable
            # error response, which the loop waits out on its own ladder, or
            # answers with a fallback model, before it gives the turn up.
            transport_evidence = _transport_failed(
                upstream_finish_reason, content_buf, reasoning_buf, tool_call_slots, final_usage, messages
            )
            if attempt < max_reconnects and transport_evidence:
                logger.warning(
                    "upstream reported a failed call as a normal end (attempt {}/{}), reconnecting",
                    attempt + 1,
                    max_reconnects + 1,
                )
                reset_attempt()
                continue
            break
        except TimeoutError as exc:
            # The idle cap already waited the full timeout; reconnecting would
            # double an already-long stall, so a stall ends the call. After output
            # it is not asked again either, unless the caller asked for a retry:
            # handed back as a retryable response, the loop's own ladder asked again
            # and a person watching the stream saw the words twice, which is the rule
            # the branch below holds and this one did not. What the reader saw is
            # handed back as the answer so far, or the stall raised when it is not one.
            if rendered() and not retry_after_output:
                return await _cut_or_raise(exc, produced())
            # Classified from the live exception rather than a fresh
            # TimeoutError: a first-byte timeout says which bound it was and how
            # long it waited, and throwing that away left the loop logging an
            # error whose message was the empty string, which is how fifteen
            # minutes of silence came to be recorded as nothing at all. The words
            # that had streamed are not the content here -- a failed call's
            # content is the account of the failure, and the caller keeps its own
            # copy of what the reader already saw.
            classification = provider.classify_error(exc)
            return LLMResponse(
                content=format_llm_error(exc, classification),
                finish_reason="error",
                error_classification=classification,
                usage=final_usage or {},
                call_record=record,
            )
        except Exception as exc:
            # Every path out of here but two leaves the provider's own exception to
            # reach the caller unchanged: per N-TURNFAILED the turn must fail (the
            # lane emits TurnFailed) rather than resolve into a "Sorry" text reply.
            # The two are the error response `strip_images` asks for below, and the
            # answer the reader already saw, which a caller that can continue it is
            # handed instead.
            emitted = rendered()
            if emitted and not retry_after_output:
                return await _cut_or_raise(exc, produced())
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
                reset_attempt()
            if classification.strip_images or (classification.should_compress and not emitted):
                # The recoveries this function cannot make: the picture has to
                # come out of the messages it only reads, and so does the history
                # a too-long prompt carries. Handed back as the error response the
                # loop's shrink-and-retry acts on; raising ended a deck build on
                # the first render an endpoint refused for its size, and ended a
                # long streamed turn on its first context overflow, and waiting
                # would have shrunk neither.
                return LLMResponse(
                    content=canonical_llm_error(classification.category, None, str(exc)),
                    finish_reason="error",
                    error_classification=classification,
                    usage=final_usage or {},
                    call_record=record,
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
                reset_attempt()
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
            reset_attempt()
            await asyncio.sleep(delay)

    if had_error:
        return await _answer_to_error_delta(
            produced(),
            error_content=error_content,
            error_classification=error_classification,
            after_output=rendered() and not retry_after_output,
        )

    if transport_evidence:
        # The reconnects are spent and the verdict still stands. Reported the way
        # the non-streaming exit reports it: handed back as an empty reply it
        # would read as the model's own, when the evidence says the request was
        # never processed.
        logger.warning("upstream reported a failed call as a normal end: {}", transport_evidence)
        return LLMResponse(
            content=transport_failure_message(transport_evidence),
            finish_reason="error",
            usage=final_usage or {},
            error_classification=ErrorClassification(
                category="upstream_transport_failure",
                retryable=True,
                should_fallback=True,
            ),
            call_record=record,
        )

    tool_calls = _finalize_tool_calls(tool_call_slots)

    # Asked of the same owner the request body asks, rather than passed in: the
    # loop calls chat_stream with no ceiling, and the provider resolves its own
    # from this function. Two calls to one owner agree by construction, which is
    # why ``send_max_tokens`` exists; leaving it out is what made every one of a
    # deck run's truncation lines read ``max_tokens=None``.
    sent_max_tokens, truncated = flag_truncation(
        finish_reason=upstream_finish_reason,
        usage=final_usage,
        tool_calls=tool_calls,
        sent=stream_kwargs.get("max_tokens")
        or send_max_tokens(
            getattr(provider, "generation", None),
            getattr(provider, "wire_model_id", lambda m: m)(model or getattr(provider, "default_model", "") or ""),
        ),
    )

    # No terminal reason arrived means none may be fabricated: a reply that died
    # mid-stream has to stay distinguishable from one that finished.
    finish_reason = upstream_finish_reason or "unknown"

    # A call that emitted nothing but thought still thought for a measurable
    # time; the end of the stream is where that thought stopped.
    stop_thinking()

    content = "".join(content_buf)
    # A held opening that the end of the stream shows was not a sentinel after
    # all -- a reply of `<`, say -- is an answer, and goes out as one.
    if on_token_delta is not None and not content_released and without_sentinels(content):
        await on_token_delta(content)
    content, reasoning_content = _split_unparsed_thought(provider, content, "".join(reasoning_buf) or None)
    # The same fold as the non-streaming exit: a reply that is only a sentinel
    # reaches the loop empty on both paths.
    content = without_sentinels(content)

    # The verdict was asked inside the attempt loop, where a reconnect was still
    # possible, and a standing one was returned above. What reaches here has no
    # transport evidence, so an empty response is the model's own and the loop's
    # empty-response recovery owns it rather than being pre-empted by an error.
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=final_usage or {},
        reasoning_content=reasoning_content,
        thinking_blocks=thinking_blocks,
        truncated=truncated,
        max_tokens=sent_max_tokens,
        reasoning_ms=reasoning_ms,
        call_record=record,
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
