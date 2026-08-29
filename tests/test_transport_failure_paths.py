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

from raven.providers.base import GenerationSettings, LLMResponse, StreamDelta
from raven.providers.litellm_provider import LiteLLMProvider
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


def test_silence_with_honest_usage_is_left_to_the_loop() -> None:
    """The review's finding, as a test.

    An empty response whose prompt was billed truthfully is a model that said
    nothing -- `recovery.py` recovers that by changing the request (a prefill,
    a post-tool nudge) or retrying it. Reporting it as an error takes all of
    that out of service, because the loop breaks on an error response before
    `classify_empty_response` runs. Retrying the identical request, the only
    thing this verdict can ask for, is also the one thing that does not help.
    """
    silent = _Response(
        choices=[_Choice(message=_Msg())],
        usage=_Usage(prompt_tokens=2900, completion_tokens=1, total_tokens=2901),
    )

    assert _provider()._parse_response(silent, sent_chars=SENT).finish_reason == "stop"


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
            yield StreamDelta(content=None, finish_reason="stop", usage={"prompt_tokens": 1, "total_tokens": 2})
            return
        yield StreamDelta(content="the answer")
        yield StreamDelta(content=None, finish_reason="stop", usage={"prompt_tokens": 2900, "total_tokens": 2903})

    def classify_error(self, exc: Any = None, content: Any = None) -> Any:
        from raven.providers.base import ErrorClassification

        return ErrorClassification(category="network", retryable=True)


class _EmptyStream:
    """A stream that opens, says nothing, and closes on `stop`.

    `chat_stream` is normalised by the provider, so what the assembler sees is
    `StreamDelta` -- the terminal one carries the finish reason.
    """

    generation = GenerationSettings()

    async def chat_stream(self, *a: Any, **kw: Any):
        yield StreamDelta(content=None, finish_reason="stop", usage={"prompt_tokens": 1, "total_tokens": 2})


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


async def test_a_stream_that_stays_empty_ends_as_empty_not_as_an_error() -> None:
    """Once the reconnects are spent, the response is what it is. An error
    here would pre-empt the loop's own empty-response recovery."""
    from raven.providers.streaming import stream_llm_call

    response = await stream_llm_call(
        _EmptyStream(),
        messages=[{"role": "user", "content": "hi" * 6000}],
        tools=None,
        model="stub",
    )

    assert response.finish_reason == "stop"
    assert response.content == ""


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

    A prompt billed truthfully and an empty reply is a silent model, which is
    why the accounting verdict refuses it. But an upstream that said
    `finish_reason=error` did not stay silent -- it reported a failure, and the
    library filed the original beside the value it rewrote. Read there, the
    verdict needs no corroboration and no threshold.
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

    `_parse_response` defaults a missing finish reason to `stop` on the way
    out. Reading that default back would condemn every provider that omits the
    field -- a case nobody reported, and one the streaming side already settles
    differently (see `test_llm_call_stream_empty_stream_yields_empty_content`).
    """
    silent = _Response(choices=[_Choice(message=_Msg(), finish_reason=None)])  # type: ignore[arg-type]

    assert _provider()._parse_response(silent).finish_reason == "stop"


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
    that the loop's own silence recovery should have had. The verdict cannot tell
    that apart from a new failure dialect and does not try: both are refused, for
    the reason the allow list exists. What it can do is say which one it is
    looking at, so the case has a signal at all rather than only a retry nobody
    asked about.
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

    Images are attached after a tool result, which is exactly where models go
    silent -- so the two halves of the false verdict coincided by construction:
    an inflated threshold and an empty response arriving together, on a call
    where the upstream did nothing wrong. Reconnecting sent the picture twice
    and logged a transport failure for it.
    """
    from raven.providers.streaming import stream_llm_call

    class _SilentAboutThePicture:
        generation = GenerationSettings()

        def __init__(self) -> None:
            self.attempts = 0

        async def chat_stream(self, *a: Any, **kw: Any):
            self.attempts += 1
            # Truthful accounting for a megabyte of png.
            yield StreamDelta(content=None, finish_reason="stop", usage={"prompt_tokens": 1800, "total_tokens": 1801})

    provider = _SilentAboutThePicture()
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
    loop's own recovery for a silent model would be unreachable behind it."""
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
