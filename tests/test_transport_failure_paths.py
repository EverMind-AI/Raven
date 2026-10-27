"""An upstream failure is not an answer, on either response path.

The upstream can mark a call failed in the response body -- `finish_reason=error`
with nothing in it -- and the client library rewrites that to `stop` from a
lookup table, silently and in place (litellm 1.97.0,
`core_helpers.py` maps `"error": "stop"`; `gpt_transformation.py` overwrites
the field and keeps no copy). Raven's retry ladder gates on
`finish_reason != "error"`, so it judged the call healthy: no retry, no
classification, no model fallback. A backend blip that one retry would have
healed became a hard failure, and the error pointed at the model.

Where the response was assembled through `Choices.__init__` the library keeps
the value it renamed, so the upstream's own word is readable and these pin that
reading; where a vendor overwrote the reason on an already-built choice nothing
was kept, and they pin the decision made from local evidence instead. Both paths
ask the same question, for the reason
`truncation.py` gives for asking its own on both: a verdict reached on one
and not the other means the same fault reads differently in the TUI than
outside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from raven.contracts.llm_provider import CallRecord, ChatDelta, GenerationSettings, LLMResponse
from raven.providers.litellm_provider import LiteLLMProvider
from raven.providers.streaming import STREAM_CUT
from raven.providers.transport_failure import flag_transport_failure

# ---------- the shape litellm hands back, with the error already laundered ----


@dataclass
class _Msg:
    content: str | None = None
    tool_calls: list[Any] | None = None
    reasoning_content: str | None = None


@dataclass
class _Choice:
    message: _Msg
    finish_reason: str = "stop"
    #: What the library records when its mapping renamed the upstream's value.
    #: Absent, not empty, on a response it did not rename -- so the default
    #: stands for "no rename happened", not for "the field was empty".
    provider_specific_fields: dict[str, Any] | None = None


@dataclass
class _Usage:
    prompt_tokens: int = 1
    completion_tokens: int = 1
    total_tokens: int = 2


@dataclass
class _Response:
    choices: list[_Choice]
    usage: _Usage = field(default_factory=_Usage)


def _laundered() -> _Response:
    """The observed response, with every recorded field on it.

    Verbatim from the trace: `content: null`, `finish_reason: "stop"`, no tool
    calls, `prompt_tokens: 1` against a prompt of about 3000 -- and
    `reasoning_content: "#"`, billed as the one completion token.

    That last field is the one that decides, and leaving it at its default is
    what made an earlier revision of this file green while the detector did not
    fire on the recorded shape at all. It is residue from a prompt that was
    never read, not thought about it, which is why the verdict does not weigh
    the reasoning field -- see `transport_failure._said_nothing`.
    """
    return _Response(choices=[_Choice(message=_Msg(reasoning_content="#"))])


def _tool_call() -> SimpleNamespace:
    """The shape `_parse_response` reads a tool call out of."""
    return SimpleNamespace(
        function=SimpleNamespace(name="read_file", arguments='{"path": "a.txt"}', provider_specific_fields=None),
        provider_specific_fields=None,
    )


#: What the incident sent: about 3000 tokens, billed as one.
SENT = 12000


def _provider() -> LiteLLMProvider:
    with (
        patch("raven.providers.litellm_provider.litellm"),
        patch("raven.providers.litellm_provider.LiteLLMProvider._setup_env"),
    ):
        return LiteLLMProvider(api_key="sk-test", provider_name="openrouter")


# ---------- the non-streaming path -------------------------------------------


def test_an_empty_stop_is_reported_as_the_failure_it_is() -> None:
    """The whole incident, at the response exit."""
    response = _provider()._parse_response(_laundered(), sent_chars=SENT)

    assert response.finish_reason == "error"


def test_the_error_says_it_was_the_upstream_and_carries_its_evidence() -> None:
    """The diagnosis is half the bug. What the caller received said the model
    had chosen to say nothing, so the agent went off repairing its own prompt
    -- a dimension unrelated to the actual fault."""
    response = _provider()._parse_response(_laundered(), sent_chars=SENT)

    text = (response.content or "").lower()
    assert "upstream" in text
    assert "model" not in text.replace("model fallback", "")
    assert response.error_classification is not None
    assert response.error_classification.retryable


def test_an_empty_reply_with_honest_usage_is_left_to_the_loop() -> None:
    """The review's finding, as a test.

    An empty response whose prompt was billed truthfully is a model that
    returned nothing -- `recovery.py` recovers that by changing the request (a
    prefill, a post-tool nudge, a continuation after a cut at the output limit)
    or by resending it. Reporting it as an error takes all of that out of
    service, because the loop breaks on an error response before `reply_kind`
    runs, and the identical request again is all this verdict can ask for.
    """
    empty = _Response(
        choices=[_Choice(message=_Msg())],
        usage=_Usage(prompt_tokens=2900, completion_tokens=1, total_tokens=2901),
    )

    assert _provider()._parse_response(empty, sent_chars=SENT).finish_reason == "stop"


def test_content_that_is_only_a_sentinel_counts_as_nothing_delivered() -> None:
    """An upstream can leak its own end-of-text marker in place of an answer.

    Nobody can read it, so it is not a reply -- and with the accounting of a
    request that was never processed beside it, it is the same fault as an
    empty one. Without the sentinel list this reads as content and walks.
    """
    leaked = _Response(
        choices=[_Choice(message=_Msg(content="<|endoftext|>"))],
        usage=_Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )

    assert _provider()._parse_response(leaked, sent_chars=SENT).finish_reason == "error"


def test_a_reply_that_is_only_a_sentinel_is_handed_to_the_loop_empty() -> None:
    """The shape the review measured on this branch: OpenRouter, native `stop`,
    real usage, and a reply of nothing but `<|im_end|>`.

    With `stop` on the allow list the verdict rightly steps aside -- and the
    loop's emptiness test reads the sentinel as visible text, so the user was
    handed `<|im_end|>` as the answer. The exit folds such a reply to empty, and
    the loop runs the recovery it has for one; the finish reason stays the
    upstream's, because nothing failed.
    """
    bare = _Response(
        choices=[
            _Choice(
                message=_Msg(content="<|im_end|>"),
                provider_specific_fields={"native_finish_reason": "stop"},
            )
        ],
        usage=_Usage(prompt_tokens=367412, completion_tokens=27, total_tokens=367439),
    )

    response = _provider()._parse_response(bare, sent_chars=SENT)

    assert response.finish_reason == "stop"
    assert response.content == ""
    assert response.error_classification is None


@pytest.mark.parametrize(
    ("content", "folded"),
    [
        (None, None),
        ("", ""),
        ("<|im_end|>", ""),
        (" <|eot_id|> ", ""),
        ("<|endoftext|><|im_end|>", ""),
        ("the answer<|endoftext|>", "the answer<|endoftext|>"),
        ("hi", "hi"),
    ],
)
def test_without_sentinels_folds_only_a_reply_that_is_nothing_else(content: str | None, folded: str | None) -> None:
    from raven.providers.transport_failure import without_sentinels

    assert without_sentinels(content) == folded


def test_the_fold_reads_the_reply_after_the_orphan_think_is_split_off() -> None:
    """A self-hosted backend that leaves `</think>` in the content: the split
    takes the thought, and what is left is only the sentinel. The fold has to
    read that remainder, not the raw content -- folded first, the thought
    beside the sentinel reads as an answer and `<|im_end|>` reaches the loop.
    """
    thought_then_sentinel = _Response(
        choices=[
            _Choice(
                message=_Msg(content="plan the reply</think>\n<|im_end|>"),
                provider_specific_fields={"native_finish_reason": "stop"},
            )
        ],
        usage=_Usage(prompt_tokens=367412, completion_tokens=27, total_tokens=367439),
    )

    with patch.object(LiteLLMProvider, "emits_unparsed_reasoning", return_value=True):
        response = _provider()._parse_response(thought_then_sentinel, sent_chars=SENT)

    assert response.finish_reason == "stop"
    assert response.reasoning_content == "plan the reply"
    assert response.content == ""


def test_a_sentinel_beside_a_real_answer_is_still_an_answer() -> None:
    """Stripping is for deciding whether anything was said, not for editing the
    reply: the content delivered is untouched."""
    trailing = _Response(
        choices=[_Choice(message=_Msg(content="the answer<|endoftext|>"))],
        usage=_Usage(prompt_tokens=1, completion_tokens=4, total_tokens=5),
    )

    parsed = _provider()._parse_response(trailing, sent_chars=SENT)

    assert parsed.finish_reason == "stop"
    assert parsed.content == "the answer<|endoftext|>"


def test_a_short_answer_with_ordinary_usage_stays_healthy() -> None:
    """The adversarial case: a one-word reply is still a reply."""
    ok = _Response(
        choices=[_Choice(message=_Msg(content="ok"))],
        usage=_Usage(prompt_tokens=2900, completion_tokens=1, total_tokens=2901),
    )

    assert _provider()._parse_response(ok).finish_reason == "stop"


def test_declining_to_call_a_tool_is_an_answer() -> None:
    """The neighbouring legitimate case this must not swallow: the model was
    offered tools, used none, and said why."""
    said = _Response(
        choices=[_Choice(message=_Msg(content="I cannot help with that."))],
        usage=_Usage(prompt_tokens=2900, completion_tokens=6, total_tokens=2906),
    )

    assert _provider()._parse_response(said).finish_reason == "stop"


def test_a_reasoning_only_reply_with_honest_usage_is_the_loops_business() -> None:
    """A model that emitted only thought is not this verdict's case -- but the
    accounting is what says so, not the reasoning field.

    The field itself is not weighed: it used to be, and a single `"#"` of
    upstream residue then vetoed the stronger evidence and defeated the
    detector on the very incident it was built for.
    """
    thought = _Response(
        choices=[_Choice(message=_Msg(reasoning_content="let me look at the schema", content=""))],
        usage=_Usage(prompt_tokens=2900, completion_tokens=40, total_tokens=2940),
    )

    assert _provider()._parse_response(thought, sent_chars=SENT).finish_reason == "stop"


def test_residue_in_the_reasoning_field_does_not_save_a_dead_request() -> None:
    """The recorded shape, one variable at a time.

    Each of these is the incident's usage and empty answer with a different
    reasoning field. All are condemned, because none of them can be thought
    about a prompt that was billed as one token.
    """
    for residue in ("#", "<|endoftext|>", "", "  ", None, "still thinking"):
        response = _Response(
            choices=[_Choice(message=_Msg(reasoning_content=residue))],
            usage=_Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

        assert _provider()._parse_response(response, sent_chars=SENT).finish_reason == "error", residue


def test_a_run_that_was_cut_off_is_still_truncation_not_this() -> None:
    """`length` belongs to the truncation verdict, which already owns it."""
    cut = _Response(choices=[_Choice(message=_Msg(content=""), finish_reason="length")])

    assert _provider()._parse_response(cut).finish_reason == "length"


# ---------- the streaming path -----------------------------------------------


class _BlipThenAnswer:
    """The upstream that was observed: one request the backend never processed,
    then the same request served properly."""

    generation = GenerationSettings()

    def __init__(self) -> None:
        self.attempts = 0

    async def chat_stream(self, *a: Any, **kw: Any):
        self.attempts += 1
        if self.attempts == 1:
            yield ChatDelta(content=None, finish_reason="stop", usage={"prompt_tokens": 1, "total_tokens": 2})
            return
        yield ChatDelta(content="the answer")
        yield ChatDelta(content=None, finish_reason="stop", usage={"prompt_tokens": 2900, "total_tokens": 2903})

    def classify_error(self, exc: Any = None, content: Any = None) -> Any:
        from raven.contracts.llm_provider import ErrorClassification

        return ErrorClassification(category="network", retryable=True)


class _EmptyStream:
    """A stream that opens, says nothing, and closes on `stop`.

    `chat_stream` is normalised by the provider, so what the assembler sees is
    `ChatDelta` -- the terminal one carries the finish reason.
    """

    generation = GenerationSettings()

    def __init__(self) -> None:
        self.attempts = 0

    async def chat_stream(self, *a: Any, **kw: Any):
        self.attempts += 1
        yield ChatDelta(content=None, finish_reason="stop", usage={"prompt_tokens": 1, "total_tokens": 2})


async def test_the_streaming_path_reconnects_instead_of_ending_the_turn() -> None:
    """The regression the review measured, pinned.

    This path has no retry ladder, and the loop breaks the turn on an error
    response before empty-recovery can run -- so returning an error here made
    a blip that `main` recovered from end the turn on the first call. The
    verdict requires that nothing was emitted, which is exactly the condition
    the reconnect below it already tests, so it reconnects instead.
    """
    from raven.providers.streaming import stream_llm_call

    stream = _BlipThenAnswer()
    response = await stream_llm_call(
        stream,
        messages=[{"role": "user", "content": "hi" * 6000}],
        tools=None,
        model="stub",
    )

    assert stream.attempts == 2, "the blip was returned instead of reconnected"
    assert response.finish_reason == "stop"
    assert response.content == "the answer"


class _SentinelStream:
    """A stream whose only token is the upstream's own end marker."""

    generation = GenerationSettings()

    async def chat_stream(self, *a: Any, **kw: Any):
        yield ChatDelta(content="<|im_end|>")
        yield ChatDelta(content=None, finish_reason="stop", usage={"prompt_tokens": 2900, "total_tokens": 2901})


async def test_a_stream_that_is_only_a_sentinel_ends_as_empty_on_this_path_too() -> None:
    """The same fold as the non-streaming exit, so the fault reads the same
    inside the TUI as outside it."""
    from raven.providers.streaming import stream_llm_call

    response = await stream_llm_call(
        _SentinelStream(),
        messages=[{"role": "user", "content": "hi" * 6000}],
        tools=None,
        model="stub",
    )

    assert response.finish_reason == "stop"
    assert response.content == ""


class _ThoughtThenSentinelStream:
    """A self-hosted backend's stream: an unmarked thought, its closing tag,
    then only the upstream's end marker."""

    generation = GenerationSettings()

    def emits_unparsed_reasoning(self) -> bool:
        return True

    async def chat_stream(self, *a: Any, **kw: Any):
        yield ChatDelta(content="plan the reply</think>\n")
        yield ChatDelta(content="<|im_end|>")
        yield ChatDelta(content=None, finish_reason="stop", usage={"prompt_tokens": 2900, "total_tokens": 2901})


async def test_the_streamed_fold_also_reads_the_reply_after_the_split() -> None:
    """The streamed exit splits the orphan thought off first too; folded before
    the split, the thought would keep the sentinel from folding."""
    from raven.providers.streaming import stream_llm_call

    response = await stream_llm_call(
        _ThoughtThenSentinelStream(),
        messages=[{"role": "user", "content": "hi" * 6000}],
        tools=None,
        model="stub",
    )

    assert response.finish_reason == "stop"
    assert response.reasoning_content == "plan the reply"
    assert response.content == ""


def test_the_unparsed_thought_is_split_only_for_a_backend_that_leaks_one() -> None:
    """The split the streamed exit makes before it folds sentinels, on its own."""
    from raven.providers.streaming import _split_unparsed_thought

    leaky = SimpleNamespace(emits_unparsed_reasoning=lambda: True)
    careful = SimpleNamespace(emits_unparsed_reasoning=lambda: False)
    duck_typed = SimpleNamespace()
    reply = "plan</think>\nanswer"

    assert _split_unparsed_thought(leaky, reply, None) == ("answer", "plan")
    # A structured thought that already arrived wins outright.
    assert _split_unparsed_thought(leaky, reply, "structured") == (reply, "structured")
    assert _split_unparsed_thought(careful, reply, None) == (reply, None)
    assert _split_unparsed_thought(duck_typed, reply, None) == (reply, None)


async def test_a_stream_that_stays_empty_is_reported_as_the_failure_it_is() -> None:
    """Once the reconnects are spent the verdict still stands. Handed back as an
    empty reply, evidence that the request was never processed read as a reply
    that happened to be empty."""
    from raven.providers.streaming import stream_llm_call

    stream = _EmptyStream()
    response = await stream_llm_call(
        stream,
        messages=[{"role": "user", "content": "hi" * 6000}],
        tools=None,
        model="stub",
    )

    assert stream.attempts == 2, "the failure was reported before the reconnect was spent"
    assert response.finish_reason == "error"
    assert response.error_classification is not None
    assert response.error_classification.category == "upstream_transport_failure"
    assert response.error_classification.retryable is True
    assert response.error_classification.should_fallback is True
    assert "upstream" in (response.content or "").lower()


async def test_both_exits_report_the_failure_the_same_way() -> None:
    """The same fault must read the same whichever exit reported it: one path's
    classification drifting from the other's is a failure the loop retries on
    one side and gives up on the other."""
    from raven.providers.streaming import stream_llm_call

    plain = _provider()._parse_response(_laundered(), sent_chars=SENT)
    streamed = await stream_llm_call(
        _EmptyStream(), messages=[{"role": "user", "content": "hi" * 6000}], tools=None, model="stub"
    )

    assert plain.finish_reason == streamed.finish_reason == "error"
    assert plain.error_classification == streamed.error_classification
    assert (plain.content or "").split(":")[0] == (streamed.content or "").split(":")[0]


class _DeclinedThenStop:
    """A stream that declines, then closes on the ``stop`` every stream closes on."""

    generation = GenerationSettings()

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict

    async def chat_stream(self, *a: Any, **kw: Any):
        yield ChatDelta(content="I can't help with that", finish_reason=self.verdict)
        yield ChatDelta(content=None, finish_reason="stop", usage={"prompt_tokens": 2900, "total_tokens": 2950})


@pytest.mark.parametrize("verdict", ["refusal", "content_filter"])
async def test_a_stop_after_a_refusal_does_not_overwrite_it(verdict: str) -> None:
    """The ``stop`` that closes a stream only says the stream is over. Read as the
    model's own finish it turned a refusal into a reply that happened to be short."""
    from raven.providers.streaming import stream_llm_call

    response = await stream_llm_call(
        _DeclinedThenStop(verdict),
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="stub",
    )

    assert response.finish_reason == verdict
    assert response.content == "I can't help with that"


# ---------- what the watcher sees while it streams ----------------------------


class _ScriptedStream:
    """A stream that sends `contents` as content deltas, then ends as told.

    `emitted` is what the watcher was handed; `shown_before` records how much of
    it had gone out by the time each delta after the first was sent, which is
    what a watcher's wait for the first token is made of.
    """

    generation = GenerationSettings()

    def __init__(
        self,
        *contents: str,
        finish: str | None = "stop",
        error_delta: bool = False,
        raises: Exception | None = None,
    ) -> None:
        self.contents = contents
        self.finish = finish
        self.error_delta = error_delta
        self.raises = raises
        self.emitted: list[str] = []
        self.shown_before: list[int] = []

    async def chat_stream(self, *a: Any, **kw: Any):
        for index, content in enumerate(self.contents):
            if index:
                self.shown_before.append(len("".join(self.emitted)))
            yield ChatDelta(content=content)
        if self.raises is not None:
            raise self.raises
        if self.error_delta:
            from raven.contracts.llm_provider import ErrorClassification

            yield ChatDelta(
                content="the upstream failed",
                finish_reason="error",
                error_classification=ErrorClassification(category="network", retryable=False),
            )
            return
        if self.finish is not None:
            yield ChatDelta(
                content=None, finish_reason=self.finish, usage={"prompt_tokens": 2900, "total_tokens": 2950}
            )

    def classify_error(self, exc: Any = None, content: Any = None) -> Any:
        from raven.contracts.llm_provider import ErrorClassification

        return ErrorClassification(category="network", retryable=True)


async def _watched(stream: Any, **kw: Any) -> LLMResponse:
    from raven.providers.streaming import stream_llm_call

    async def watch(text: str) -> None:
        stream.emitted.append(text)

    return await stream_llm_call(
        stream,
        messages=[{"role": "user", "content": "hi" * 6000}],
        tools=None,
        model="stub",
        on_token_delta=watch,
        **kw,
    )


async def test_a_reply_that_is_only_a_sentinel_is_never_shown_to_the_watcher() -> None:
    """The review's reproduction: the response folded to empty while the
    callback had already been handed `<|im_end|>`."""
    stream = _ScriptedStream("<|im_end|>")

    response = await _watched(stream)

    assert stream.emitted == []
    assert response.content == ""


@pytest.mark.parametrize(
    "contents",
    [
        ("<|im_", "end|>"),
        ("<", "|", "im", "_e", "nd|>"),
        ("\n", "<|eot_", "id|>", " "),
        ("<|endoftext|><|im_", "end|>"),
    ],
)
async def test_a_sentinel_split_across_deltas_is_held_until_it_completes(contents: tuple[str, ...]) -> None:
    """`<|im_` on its own is text; only the next delta says it was a sentinel."""
    stream = _ScriptedStream(*contents)

    response = await _watched(stream)

    assert stream.emitted == []
    assert response.content == ""


@pytest.mark.parametrize(
    ("contents", "shown_before"),
    [
        (("Hello", " world"), [5]),
        (("<", "div>hello</div>"), [0]),
        (("\n", " ", "Hi"), [0, 0]),
    ],
)
async def test_an_ordinary_reply_reaches_the_watcher_whole_and_on_time(
    contents: tuple[str, ...], shown_before: list[int]
) -> None:
    """A reply that does not begin like a sentinel goes out on its first delta;
    one that opens with `<` or whitespace waits only for the character that
    tells it apart, and then goes out exactly as it came."""
    stream = _ScriptedStream(*contents)

    response = await _watched(stream)

    assert "".join(stream.emitted) == response.content == "".join(contents)
    assert "" not in stream.emitted
    assert stream.shown_before == shown_before


async def test_a_reply_that_only_looked_like_a_sentinel_goes_out_at_the_end() -> None:
    stream = _ScriptedStream("<|im")

    response = await _watched(stream)

    assert stream.emitted == ["<|im"]
    assert response.content == "<|im"


@pytest.mark.parametrize(
    "contents",
    [("<|im_end|>", "the answer"), ("the answer", "<|im_end|>"), ("<|im_", "end|>", "the answer")],
)
async def test_a_sentinel_beside_a_real_answer_is_shown_as_it_is_returned(contents: tuple[str, ...]) -> None:
    stream = _ScriptedStream(*contents)

    response = await _watched(stream)

    assert "".join(stream.emitted) == response.content == "".join(contents)


@pytest.mark.parametrize(
    ("stream", "content"),
    [
        (_ScriptedStream("<|im_end|>", finish="length"), ""),
        (_ScriptedStream("<|im_", "end|>", finish=None), ""),
        (_ScriptedStream("<|im_end|>", error_delta=True), "the upstream failed"),
    ],
    ids=["truncated", "closed-early", "error-delta"],
)
async def test_a_failed_stream_does_not_release_a_held_sentinel(stream: _ScriptedStream, content: str) -> None:
    response = await _watched(stream)

    assert stream.emitted == []
    assert response.content == content


async def test_a_stream_that_raises_after_a_sentinel_has_shown_nothing() -> None:
    stream = _ScriptedStream("<|im_", raises=RuntimeError("connection reset"))

    with pytest.raises(RuntimeError):
        await _watched(stream)

    assert stream.emitted == []


class _AnswerCutThenSentinel(_ScriptedStream):
    """A first attempt that streams words and drops, then a second that is only
    the end marker."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def chat_stream(self, *a: Any, **kw: Any):
        self.attempts += 1
        if self.attempts == 1:
            yield ChatDelta(content="partial")
            raise RuntimeError("connection reset")
        yield ChatDelta(content="<|im_end|>")
        yield ChatDelta(content=None, finish_reason="stop", usage={"prompt_tokens": 2900, "total_tokens": 2950})


async def test_a_retried_attempt_holds_its_own_opening_again() -> None:
    """What the first attempt released does not release the second's."""
    stream = _AnswerCutThenSentinel()

    response = await _watched(stream, retry_after_output=True)

    assert stream.attempts == 2
    assert stream.emitted == ["partial"]
    assert response.content == ""


@pytest.mark.parametrize(
    "contents",
    [("<|im_end|>",), ("<|im_", "end|>"), ("<", "div>"), ("<|im_end|>", "the answer"), ("Hello",)],
)
async def test_without_a_watcher_the_reply_is_unchanged(contents: tuple[str, ...]) -> None:
    """Spawn and the DAG stream with no callback; holding back is only about
    what a watcher is shown."""
    from raven.providers.streaming import stream_llm_call

    watched = await _watched(_ScriptedStream(*contents))
    unwatched = await stream_llm_call(
        _ScriptedStream(*contents),
        messages=[{"role": "user", "content": "hi" * 6000}],
        tools=None,
        model="stub",
    )

    assert unwatched.content == watched.content
    assert unwatched.finish_reason == watched.finish_reason


# ---------- a stream that dies after the reader has seen its text -------------


@pytest.mark.parametrize("failure", [ConnectionError("connection reset"), TimeoutError()], ids=["raised", "stalled"])
async def test_a_stream_cut_after_text_hands_back_the_partial(failure: Exception) -> None:
    """What the reader has seen of a cut answer is the answer so far. It comes back as
    a reply finished as cut, for the loop to ask the model to go on from, instead of
    failing a turn whose reader already holds half of it."""
    stream = _ScriptedStream("Hello ", finish=None, raises=failure)

    response = await _watched(stream, hand_back_partial=True)

    assert response.content == "Hello "
    assert response.finish_reason == STREAM_CUT
    assert response.error_classification is None
    assert stream.emitted == ["Hello "]


async def test_a_terminal_error_delta_after_text_hands_back_the_partial() -> None:
    """The Anthropic adapter does not raise: it turns a mid-stream failure into its
    last delta, so a hand-back that only covered the raising exits would miss the
    adapter this failure is most often seen on."""
    stream = _ScriptedStream("Hello ", error_delta=True)

    response = await _watched(stream, hand_back_partial=True)

    assert (response.content, response.finish_reason) == ("Hello ", STREAM_CUT)
    assert response.error_classification is None
    assert stream.emitted == ["Hello "]


class _ThoughtThenWordsThenCut:
    """A stream that thought, said "Hello ", and lost the connection."""

    generation = GenerationSettings()

    def __init__(self) -> None:
        self.record = CallRecord(http_status=200)
        self.emitted: list[str] = []

    async def chat_stream(self, *a: Any, **kw: Any):
        yield ChatDelta(content=None, reasoning_content="weighing it", call_record=self.record)
        yield ChatDelta(
            content=None,
            usage={"prompt_tokens": 12},
            thinking_blocks=[{"type": "thinking", "thinking": "weighing it", "signature": "sig"}],
        )
        yield ChatDelta(content="Hello ")
        raise ConnectionError("connection reset")


async def test_what_the_cut_call_produced_rides_with_its_partial() -> None:
    """The call was paid for and its thought is the model's own: the loop bills the
    usage, keeps the record, and shows how long the model thought."""
    stream = _ThoughtThenWordsThenCut()

    response = await _watched(stream, hand_back_partial=True)

    assert response.content == "Hello "
    assert response.reasoning_content == "weighing it"
    assert response.thinking_blocks == [{"type": "thinking", "thinking": "weighing it", "signature": "sig"}]
    assert response.usage.get("prompt_tokens") == 12
    assert response.call_record is stream.record
    assert response.reasoning_ms is not None


class _ThoughtThenCut:
    """A stream whose reader saw only the model's thought before the connection went."""

    generation = GenerationSettings()

    async def chat_stream(self, *a: Any, **kw: Any):
        yield ChatDelta(content=None, reasoning_content="weighing it")
        raise ConnectionError("connection reset")


async def test_a_stream_cut_before_any_text_still_fails() -> None:
    """A thought the reader saw is not an answer to continue: the failure stands as it
    was, so the same words are not drawn twice."""
    from raven.providers.streaming import stream_llm_call

    async def watch_thought(text: str) -> None:
        return None

    with pytest.raises(ConnectionError):
        await stream_llm_call(
            _ThoughtThenCut(),
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            model="stub",
            on_reasoning_delta=watch_thought,
            hand_back_partial=True,
        )


class _WordsThenHalfACall:
    """A stream that said a few words and was part-way through a tool call when it dropped."""

    generation = GenerationSettings()

    def __init__(self) -> None:
        self.emitted: list[str] = []

    async def chat_stream(self, *a: Any, **kw: Any):
        yield ChatDelta(content="Let me look")
        yield ChatDelta(
            content=None,
            tool_call_delta={
                "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": '{"pa'}}]
            },
        )
        raise ConnectionError("connection reset")


async def test_a_tool_call_in_progress_is_not_an_answer_to_hand_back() -> None:
    """The words came before a call the model was still writing; continuing them as an
    answer would drop the call."""
    with pytest.raises(ConnectionError):
        await _watched(_WordsThenHalfACall(), hand_back_partial=True)


class _DropsOnceThenAnswers:
    """The first attempt shows "Hello " and drops; the second answers whole."""

    generation = GenerationSettings()

    def __init__(self) -> None:
        self.attempts = 0

    async def chat_stream(self, *a: Any, **kw: Any):
        self.attempts += 1
        if self.attempts == 1:
            yield ChatDelta(content="Hello ")
            raise ConnectionError("connection reset")
        yield ChatDelta(content="Hello world.")
        yield ChatDelta(content=None, finish_reason="stop")

    def classify_error(self, exc: Any = None, content: Any = None) -> Any:
        from raven.contracts.llm_provider import ErrorClassification

        return ErrorClassification(category="network", retryable=True)


async def test_retry_after_output_keeps_the_whole_call_retry() -> None:
    """An unattended caller that accepts seeing its output twice asked for the whole
    call to be made again; the partial is not handed back in its place."""
    stream = _DropsOnceThenAnswers()
    shown: list[str] = []

    async def watch(text: str) -> None:
        shown.append(text)

    from raven.providers.streaming import stream_llm_call

    response = await stream_llm_call(
        stream,
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="stub",
        on_token_delta=watch,
        retry_after_output=True,
        hand_back_partial=True,
    )

    assert stream.attempts == 2
    assert (response.content, response.finish_reason) == ("Hello world.", "stop")
    assert shown == ["Hello ", "Hello world."]


async def test_a_caller_that_did_not_ask_for_the_partial_still_gets_the_failure() -> None:
    """Handing the partial back is for the one call that can ask the model to go on from
    it: the loop's answering call. The others that stream through here -- a sub-agent
    node, the exhaustion wrap-up -- would take half an answer for the whole of it, so a
    stream that dropped is still the failure it was for them."""
    with pytest.raises(ConnectionError):
        await _watched(_ScriptedStream("Hello ", finish=None, raises=ConnectionError("connection reset")))

    response = await _watched(_ScriptedStream("Hello ", error_delta=True))

    assert response.finish_reason == "error"
    assert response.content == "the upstream failed"
    assert response.error_classification is not None
    assert response.error_classification.retryable is False


async def test_a_cut_opening_that_was_held_back_is_shown_when_it_is_handed_back() -> None:
    """`<` is held until the next delta says whether it opens a sentinel. The stream
    dying there leaves it an answer, and the reader is shown it before the continuation
    that follows, exactly as an opening the end of a stream releases."""
    stream = _ScriptedStream("<", finish=None, raises=ConnectionError("connection reset"))

    response = await _watched(stream, hand_back_partial=True)

    assert (response.content, response.finish_reason) == ("<", STREAM_CUT)
    assert stream.emitted == ["<"]


async def test_a_cut_that_only_sent_a_sentinel_has_no_answer_to_hand_back() -> None:
    """A sentinel stands for nothing having been said, so there is nothing to continue
    and nothing the reader was shown: the failure stands, raised or reported."""
    raised = _ScriptedStream("<|im_end|>", finish=None, raises=ConnectionError("connection reset"))
    with pytest.raises(ConnectionError):
        await _watched(raised, hand_back_partial=True)

    reported = _ScriptedStream("<|im_end|>", error_delta=True)
    response = await _watched(reported, hand_back_partial=True)

    assert response.finish_reason == "error"
    assert response.content == "the upstream failed"
    assert raised.emitted == reported.emitted == []


# ---------- what the verdict is worth: the ladder acts on it ------------------


class _BlipsThenAnswers:
    """The upstream that was actually observed: the same request is routed to
    a backend that cannot serve it, then to one that can."""

    generation = GenerationSettings()

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **kw: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LiteLLMProvider._parse_response(self, _laundered(), sent_chars=SENT)  # type: ignore[arg-type]
        return LLMResponse(content="the answer", finish_reason="stop")


async def test_the_call_is_retried_and_the_user_never_sees_the_blip() -> None:
    """The point of the whole change. In the control experiment the same
    request succeeded on the next backend; Raven never retried because it could
    not see a failure."""
    provider = _provider()
    inner = _BlipsThenAnswers()
    calls = {"n": 0}

    async def chat(**kw: Any) -> LLMResponse:
        calls["n"] += 1
        return await inner.chat(**kw)

    provider.chat = chat  # type: ignore[method-assign]
    provider._CHAT_RETRY_DELAYS = (0.0,)  # type: ignore[misc]

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])

    assert calls["n"] == 2, "the laundered failure did not reach the retry ladder"
    assert response.content == "the answer"


def test_a_laundered_failure_that_still_answered_is_delivered() -> None:
    """The boundary of what this can see, fixed so it cannot drift.

    In the control experiment six responses carried `finish_reason=error` and a
    complete tool call. The client library overwrites that field in place and
    keeps no copy, so locally those are indistinguishable from a healthy
    answer -- and this deliberately delivers them. The invariant is that a
    failure must be *visible*, not that a usable answer be thrown away.
    """
    answered = _Response(
        choices=[
            _Choice(
                message=_Msg(content="", tool_calls=[_tool_call()]),
                finish_reason="tool_calls",
            )
        ],
        usage=_Usage(prompt_tokens=2900, completion_tokens=12, total_tokens=2912),
    )

    response = _provider()._parse_response(answered)

    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1


def test_the_upstreams_own_word_is_read_where_the_library_kept_it() -> None:
    """The case the accounting evidence cannot reach.

    A prompt billed truthfully and an empty reply is a model that returned
    nothing, which is why the accounting verdict refuses it. But an upstream
    that said `finish_reason=error` said something after all -- it reported a
    failure, and the library filed the original beside the value it rewrote.
    Read there, the verdict needs no corroboration and no threshold.
    """
    laundered_but_billed_honestly = _Response(
        choices=[
            _Choice(
                message=_Msg(),
                provider_specific_fields={"native_finish_reason": "error"},
            )
        ],
        usage=_Usage(prompt_tokens=2900, completion_tokens=1, total_tokens=2901),
    )

    response = _provider()._parse_response(laundered_but_billed_honestly, sent_chars=SENT)

    assert response.finish_reason == "error"
    assert "error" in (response.content or "")
    assert response.error_classification is not None
    assert response.error_classification.retryable


def test_a_renamed_normal_end_is_not_a_failure() -> None:
    """`end_turn` is what Anthropic calls a normal end, and the library renames
    every one of them. Reading any rename as a failure would report a fault on
    every healthy Anthropic turn that happened to say nothing."""
    renamed = _Response(
        choices=[
            _Choice(
                message=_Msg(),
                provider_specific_fields={"native_finish_reason": "end_turn"},
            )
        ],
        usage=_Usage(prompt_tokens=2900, completion_tokens=1, total_tokens=2901),
    )

    assert _provider()._parse_response(renamed, sent_chars=SENT).finish_reason == "stop"


def test_a_rename_nobody_classified_is_read_as_a_failure() -> None:
    """The direction the allow list buys.

    `network_error` reaches `stop` through the same table as `error`, and no
    list of failure spellings written before it appeared would have held it.
    An unrecognised rename has to land on the side that keeps the failure
    visible, because the alternative is the fault this module exists for.
    """
    unknown = _Response(
        choices=[
            _Choice(
                message=_Msg(),
                provider_specific_fields={"native_finish_reason": "network_error"},
            )
        ],
        usage=_Usage(prompt_tokens=2900, completion_tokens=1, total_tokens=2901),
    )

    assert _provider()._parse_response(unknown, sent_chars=SENT).finish_reason == "error"


def test_a_cache_hit_bills_a_fraction_of_the_prompt_and_is_not_a_failure() -> None:
    """The trap in reading `prompt_tokens` alone.

    A cached prompt is legitimately billed at a sliver of its size, so the
    accounting test adds the cache counters back before comparing. Without
    that, every cached call on a long conversation reads as a request that was
    never processed.
    """
    cached = _Response(
        choices=[_Choice(message=_Msg(content="carrying on"))],
        usage=_Usage(prompt_tokens=12, completion_tokens=3, total_tokens=15),
    )
    cached.usage.prompt_tokens_details = SimpleNamespace(cached_tokens=2900)

    response = _provider()._parse_response(cached, sent_chars=SENT)

    assert response.finish_reason == "stop"


def test_honest_accounting_is_not_a_transport_failure() -> None:
    """The evidence is the verdict, not a decoration on it. Without it the
    claim in the message -- "a transport failure, not a refusal to answer" --
    would be false for the commonest empty response there is."""
    from raven.providers.transport_failure import flag_transport_failure

    assert (
        flag_transport_failure(
            finish_reason="stop",
            content=None,
            reasoning=None,
            tool_calls=[],
            usage={"prompt_tokens": 2900},
            sent_chars=SENT,
        )
        is None
    )


async def test_one_blip_is_healed_on_either_path() -> None:
    """The constraint `truncation` states for its own question, held at the
    level that matters: the user sees a working answer either way.

    What differs below it is which mechanism heals it -- the retry ladder on
    one path, the stream's own reconnect on the other -- because those are the
    recoveries each path actually has.
    """
    from raven.providers.streaming import stream_llm_call

    streamed = _BlipThenAnswer()
    from_stream = await stream_llm_call(
        streamed, messages=[{"role": "user", "content": "hi" * 6000}], tools=None, model="stub"
    )
    assert from_stream.content == "the answer"

    verdict = _provider()._parse_response(_laundered(), sent_chars=SENT)
    assert verdict.finish_reason == "error"
    assert verdict.error_classification is not None
    assert verdict.error_classification.retryable


def test_a_cached_prompt_is_not_a_dead_request() -> None:
    """The cached counters are added back before the accounting is judged.

    A cache hit legitimately bills a sliver of the prompt. Now that the
    accounting *is* the verdict, missing this would condemn every cached call
    that came back empty -- and send its reader after a transport fault that is
    not there.
    """
    from raven.providers.transport_failure import flag_transport_failure

    assert (
        flag_transport_failure(
            finish_reason="stop",
            content=None,
            reasoning=None,
            tool_calls=[],
            usage={"prompt_tokens": 12, "cache_read_input_tokens": 2900},
            sent_chars=SENT,
        )
        is None
    )


def test_an_upstream_that_said_nothing_about_how_it_ended_is_not_accused() -> None:
    """The verdict disbelieves a claim; it does not invent one.

    `_parse_response` defaults a missing finish reason to `unknown` on the way
    out, which says only that nothing was said. Read back as a claim it would
    condemn every provider that omits the field -- a case nobody reported, and
    one the streaming side settles the same way (see
    `test_llm_call_stream_empty_stream_yields_empty_content`).
    """
    no_finish = _Response(choices=[_Choice(message=_Msg(), finish_reason=None)])  # type: ignore[arg-type]

    parsed = _provider()._parse_response(no_finish)

    assert parsed.finish_reason == "unknown"
    assert parsed.error_classification is None


# ---------- against the library itself, not a double -------------------------


def test_the_client_library_really_does_launder_the_failure() -> None:
    """The premise. If a future litellm stops rewriting `error`, the verdict
    above becomes redundant rather than wrong -- but we should find that out
    from a red test, not by wondering why an ancient workaround is still here.

    Both branches reach the same place. The version this repo locks has no
    lowercase `error` in its table, so it falls through to the unmapped default
    and logs a warning; later versions carry the entry and rewrite silently.
    Either way the caller is handed `stop` and the original value is gone.
    """
    from litellm.litellm_core_utils.core_helpers import map_finish_reason

    assert map_finish_reason("error") == "stop"


def test_the_library_files_the_original_beside_the_value_it_rewrote() -> None:
    """The other half of the premise, on the real transformation.

    Asserted through `_transform_choices` rather than the constructor, because
    that function builds the choice and then assigns the mapped reason over the
    top -- if that assignment ever grew to clear the recorded copy as well, a
    constructor-level test would stay green while the verdict went blind.
    """
    from litellm.llms.openai.chat.gpt_transformation import OpenAIGPTConfig

    from raven.providers.transport_failure import native_finish_reason

    failed, healthy = (
        OpenAIGPTConfig()._transform_choices(
            [{"index": 0, "finish_reason": reason, "message": {"role": "assistant", "content": None}}]
        )[0]
        for reason in ("error", "stop")
    )

    assert failed.finish_reason == "stop"
    assert native_finish_reason(failed) == "error"
    # Nothing was renamed, so there is no copy and no attribute holding one --
    # the reader has to survive that, since it is the ordinary case.
    assert native_finish_reason(healthy) is None


def test_a_rename_never_seen_before_says_so_without_being_judged_differently() -> None:
    """The one case the allow list can get wrong, made visible.

    A normal end spelled in a way nothing has mapped is recorded as a rename, is
    absent from the allow list, and is reported as a failure -- taking a turn
    that the loop's own empty-response recovery should have had. The verdict
    cannot tell that apart from a new failure dialect and does not try: both
    are refused, for the reason the allow list exists. What it can do is say
    which one it is looking at, so the case has a signal at all rather than
    only a retry nobody asked about.
    """
    known = flag_transport_failure(
        finish_reason="stop", content=None, tool_calls=[], native_finish_reason="network_error"
    )
    unknown = flag_transport_failure(finish_reason="stop", content=None, tool_calls=[], native_finish_reason="ALL_DONE")

    # Same verdict for both -- the wording is the only thing that differs.
    assert known and unknown
    assert "not seen before" not in known
    assert "not seen before" in unknown


def test_every_spelling_of_stop_the_library_knows_is_classified() -> None:
    """The allow list's safety net.

    An unrecognised rename is read as a failure, which is the safe direction
    for a dialect we have never seen -- and the wrong one for a *normal* end
    the library learns to rename later. This walks its table so that arrives as
    a red test with a name to classify, rather than as healthy turns being
    reported as upstream failures.
    """
    from litellm.litellm_core_utils.core_helpers import _FINISH_REASON_MAP

    from raven.providers.transport_failure import _NORMAL_STOP_ALIASES, _OBSERVED_FAILURE_REASONS

    known_failures = _OBSERVED_FAILURE_REASONS

    renamed_to_stop = {name for name, mapped in _FINISH_REASON_MAP.items() if mapped == "stop" and name != "stop"}
    unclassified = renamed_to_stop - _NORMAL_STOP_ALIASES - known_failures

    assert not unclassified, (
        f"litellm renames {sorted(unclassified)} to 'stop' and this repo has not said which they are. "
        "A normal end belongs in _NORMAL_STOP_ALIASES; a failure needs no entry, but add it to "
        "known_failures here so the next addition is the only thing this test reports."
    )


def test_the_upstreams_own_stop_is_a_normal_end() -> None:
    """The shape OpenRouter handed back on 2026-10-21, from the session record.

    A reply of one space, `finish_reason: "stop"` -- and `native_finish_reason:
    "stop"` beside it, because that gateway names its backend's reason on every
    choice, rename or not. Read as "a value renamed to stop that is not a known
    normal end", a plain stop became an upstream failure: twelve retries over
    five minutes, then the verdict's own sentence delivered as the answer. A
    model that said nothing is the loop's case (prefill, nudge, retry), and it
    is only reachable when this verdict stays out of the way.
    """
    blank = _Response(
        choices=[
            _Choice(
                message=_Msg(content=" "),
                provider_specific_fields={"native_finish_reason": "stop"},
            )
        ],
        usage=_Usage(prompt_tokens=367412, completion_tokens=27, total_tokens=367439),
    )

    response = _provider()._parse_response(blank, sent_chars=SENT)

    assert response.finish_reason == "stop"
    assert response.error_classification is None
    assert "failed call" not in (response.content or "")


def test_a_native_stop_still_faces_the_accounting_check() -> None:
    """Admitting `stop` to the allow list says the upstream ended normally; it
    does not say the request was processed. A prompt of thousands of
    characters billed as one token with nothing delivered is the incident's
    dead-request shape whatever the finish reason reads, so the verdict is
    still asked -- a native `stop` must not return early past it.
    """
    evidence = flag_transport_failure(
        finish_reason="stop",
        content="",
        reasoning=None,
        tool_calls=[],
        native_finish_reason="stop",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        sent_chars=SENT,
    )

    assert evidence and "billed as 1 tokens" in evidence


def test_openrouter_names_its_backends_reason_on_every_choice() -> None:
    """The premise, on the real conversion.

    `Choices.__init__` records a copy only when it renamed something, which is
    what the reader relies on for most vendors. OpenRouter's body carries
    `native_finish_reason` itself, and the dict conversion passes every
    non-standard choice field through -- so a plain `stop` reaches the verdict
    under that name. If the library ever stopped passing it through, this goes
    red before the allow list's `stop` entry turns into dead weight.
    """
    from litellm.litellm_core_utils.llm_response_utils.convert_dict_to_response import (
        convert_to_model_response_object,
    )
    from litellm.types.utils import ModelResponse

    from raven.providers.transport_failure import native_finish_reason

    body = {
        "id": "gen-1792555321-59XScE7pyZzOA2G0vlxy",
        "object": "chat.completion",
        "created": 1792555321,
        "model": "z-ai/glm-5.3-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "native_finish_reason": "stop",
                "message": {"role": "assistant", "content": " "},
            }
        ],
        "usage": {"prompt_tokens": 367412, "completion_tokens": 27, "total_tokens": 367439},
    }

    converted = convert_to_model_response_object(response_object=body, model_response_object=ModelResponse())
    choice = converted.choices[0]

    assert choice.finish_reason == "stop"
    assert native_finish_reason(choice) == "stop"
    assert flag_transport_failure(finish_reason="stop", content=" ", tool_calls=[], native_finish_reason="stop") is None


async def test_a_real_model_response_is_judged_and_retried() -> None:
    """End to end on the library's own type, through the real `chat`.

    The doubles above pin the decision; this pins that the decision is wired to
    the shape production actually receives. A double that has drifted from
    `ModelResponse` would let every test here pass while the fault survived.
    """
    from unittest.mock import patch

    from litellm.types.utils import Choices, Message, ModelResponse, Usage

    laundered = ModelResponse(
        choices=[Choices(finish_reason="stop", index=0, message=Message(content=None, role="assistant"))],
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )
    answered = ModelResponse(
        choices=[Choices(finish_reason="stop", index=0, message=Message(content="the answer", role="assistant"))],
        usage=Usage(prompt_tokens=2900, completion_tokens=3, total_tokens=2903),
    )
    served: list[ModelResponse] = [laundered, answered]

    async def acompletion(**kwargs: Any) -> ModelResponse:
        return served.pop(0)

    provider = _provider()
    provider._CHAT_RETRY_DELAYS = (0.0,)  # type: ignore[misc]

    with patch("raven.providers.litellm_provider.acompletion", acompletion):
        response = await provider.chat_with_retry(
            messages=[{"role": "user", "content": "x" * 12000}], tools=None, model="m"
        )

    assert not served, "the blip was delivered instead of retried"
    assert response.finish_reason == "stop"
    assert response.content == "the answer"


# ---------- the measurement both paths weigh the accounting against ----------


def _vision_messages() -> list[dict[str, Any]]:
    """What a message looks like after a tool result returned a picture.

    The same shape `main.py` appends for pending images and `helpers.py` builds
    for an attachment: a block list whose image part carries a data URI.
    """
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is in this picture"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 1_400_000}},
            ],
        }
    ]


def test_a_picture_is_not_counted_as_prompt_text() -> None:
    """`len(str(content))` reprs the block list and counts the data URI into the
    total -- four orders of magnitude out on a vision turn, which inflated the
    threshold until truthful usage read as absurd."""
    from raven.providers.transport_failure import prompt_chars

    assert prompt_chars(_vision_messages()) == len("what is in this picture")


async def test_a_vision_turn_with_honest_usage_is_not_reconnected() -> None:
    """The regression the shared measurement closes.

    Images are attached after a tool result, which is exactly where models
    return nothing -- so the two halves of the false verdict coincided by
    construction: an inflated threshold and an empty response arriving
    together, on a call where the upstream did nothing wrong. Reconnecting sent
    the picture twice and logged a transport failure for it.
    """
    from raven.providers.streaming import stream_llm_call

    class _NothingAboutThePicture:
        generation = GenerationSettings()

        def __init__(self) -> None:
            self.attempts = 0

        async def chat_stream(self, *a: Any, **kw: Any):
            self.attempts += 1
            # Truthful accounting for a megabyte of png.
            yield ChatDelta(content=None, finish_reason="stop", usage={"prompt_tokens": 1800, "total_tokens": 1801})

    provider = _NothingAboutThePicture()
    response = await stream_llm_call(provider, messages=_vision_messages(), tools=None, model="stub")

    assert provider.attempts == 1, "the picture was sent twice for a call that did not fail"
    assert response.finish_reason == "stop"


# ---------- what a zero in the usage block means -----------------------------


def test_a_zero_prompt_beside_a_real_count_is_the_strongest_evidence() -> None:
    """A gateway that answers a request it never processed with a zeroed prompt
    is the incident shape, and reading zero as a missing value let it walk."""
    from raven.providers.transport_failure import flag_transport_failure

    evidence = flag_transport_failure(
        finish_reason="stop",
        content=None,
        reasoning=None,
        tool_calls=[],
        usage={"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1},
        sent_chars=SENT,
    )

    assert evidence and "billed as 0 tokens" in evidence


def test_a_usage_block_of_nothing_but_zeros_is_not_evidence() -> None:
    """That is a provider not reporting usage. Read as evidence, every empty
    response it ever returns would be called a transport failure -- and the
    loop's own recovery for an empty reply would be unreachable behind it."""
    from raven.providers.transport_failure import flag_transport_failure

    assert (
        flag_transport_failure(
            finish_reason="stop",
            content=None,
            reasoning=None,
            tool_calls=[],
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            sent_chars=SENT,
        )
        is None
    )
