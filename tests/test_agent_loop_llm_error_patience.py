"""A retryable model error outlasting the provider's ladder waits, then asks again.

The provider's own ladder is seconds long, which suits a dropped connection and
not a gateway that serves error pages for a few minutes. One measured deck build
had 62 minutes and 23.8M input tokens behind it when a 40-second OpenRouter outage
came back as a single error response and the turn ended on it. The loop now waits
out a second, longer ladder -- ``RecoveryLimits.llm_error_retry_delays`` -- before
giving the turn up, and the messages it asks with are the ones it asked with before.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.agent.loop.recovery import RecoveryLimits, limits_from_defaults
from raven.agent.window import shrink
from raven.contracts.tool import Tool, ToolResult
from raven.providers.base import ErrorClassification, LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.events import NoticeKind
from raven.spine.message import ChatType, Source
from raven.spine.turn import AnswerlessTurnError, Origin, TurnRequest
from raven.utils.images import image_block, is_image_part, text_block


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _FailsThenAnswers(LLMProvider):
    """Errors `failures` times with the given verdict, then answers."""

    def __init__(self, failures: int, verdict: ErrorClassification):
        super().__init__(api_key="test")
        self.failures = failures
        self.verdict = verdict
        self.calls = 0
        self.asked_with: list[int] = []

    def get_default_model(self) -> str:
        return "stub"

    _CHAT_RETRY_DELAYS = ()  # the provider's own ladder, exhausted at once

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, **_):
        self.calls += 1
        self.asked_with.append(len(messages))
        if self.calls <= self.failures:
            return LLMResponse(
                content="Error calling LLM (unparsable_response@ppt): Unable to get json response",
                finish_reason="error",
                error_classification=self.verdict,
            )
        return LLMResponse(content="real answer", finish_reason="stop")

    async def chat_stream(self, *args, **kwargs):  # pragma: no cover - the non-stream path is under test
        raise NotImplementedError


def _agent(workspace: Path, provider: LLMProvider, delays: tuple[float, ...]) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=10, empty_recovery=RecoveryLimits(llm_error_retry_delays=delays)),
        tools=ToolWiring(restrict_to_workspace=True),
    )


async def _turn(agent: AgentLoop, notices: list[tuple[NoticeKind, str]] | None = None):
    async def on_notice(kind: NoticeKind, detail: str) -> None:
        assert notices is not None
        notices.append((kind, detail))

    return await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="build the deck",
        ),
        session_key="s1",
        on_notice=None if notices is None else on_notice,
    )


@pytest.mark.asyncio
async def test_a_retryable_error_is_waited_out_and_the_turn_finishes(workspace):
    provider = _FailsThenAnswers(2, ErrorClassification("unparsable_response", retryable=True, should_fallback=True))
    agent = _agent(workspace, provider, delays=(0.0, 0.0, 0.0))

    out = await _turn(agent)

    assert out is not None and out[0] == "real answer"
    assert provider.calls == 3
    assert len(set(provider.asked_with)) == 1, "the retried call asks with the same messages, nothing appended"


@pytest.mark.asyncio
async def test_the_ladder_is_the_budget(workspace):
    provider = _FailsThenAnswers(3, ErrorClassification("server", retryable=True, should_fallback=True))
    agent = _agent(workspace, provider, delays=(0.0, 0.0))

    with pytest.raises(AnswerlessTurnError) as failed:
        await _turn(agent)

    assert provider.calls == 3, "two waits, three calls, then the turn fails on the third error"
    assert "Error calling LLM" in str(failed.value)


@pytest.mark.asyncio
async def test_a_non_retryable_error_is_not_asked_again(workspace):
    provider = _FailsThenAnswers(1, ErrorClassification("invalid_request", retryable=False))
    agent = _agent(workspace, provider, delays=(0.0, 0.0, 0.0))

    with pytest.raises(AnswerlessTurnError) as failed:
        await _turn(agent)

    assert provider.calls == 1
    assert "Error calling LLM" in str(failed.value)


@pytest.mark.asyncio
async def test_each_wait_is_announced_with_the_error_category(workspace):
    """The wait is the longest silence a turn has, and nothing said so.

    Worst case the ladder plus a first-byte budget per rung is minutes of a page
    showing the last thing the model said, which reads as a wedged runtime. One
    notice per wait -- carrying the category and not the vendor's own body, which
    can hold a masked key and an account URL.
    """
    provider = _FailsThenAnswers(2, ErrorClassification("server", retryable=True, should_fallback=True))
    notices: list[tuple[NoticeKind, str]] = []

    out = await _turn(_agent(workspace, provider, delays=(0.0, 0.0, 0.0)), notices)

    assert out is not None and out[0] == "real answer"
    assert notices == [(NoticeKind.LLM_RETRY, "server"), (NoticeKind.LLM_RETRY, "server")]


@pytest.mark.asyncio
async def test_an_error_nobody_will_ask_again_about_is_not_announced(workspace):
    """No wait, nothing to say: the turn is about to fail and its own report is
    what the reader gets, so a "trying again" left on the status line would
    contradict it."""
    provider = _FailsThenAnswers(1, ErrorClassification("invalid_request", retryable=False))
    notices: list[tuple[NoticeKind, str]] = []

    with pytest.raises(AnswerlessTurnError):
        await _turn(_agent(workspace, provider, delays=(0.0, 0.0, 0.0)), notices)

    assert notices == []


class _WordsItsOwnFailure(LLMProvider):
    """Hands back a failure worded its own way -- the transport-failure account,
    which is not the canonical sentence -- and is never asked twice."""

    def __init__(self, detail: str) -> None:
        super().__init__(api_key="test")
        self._detail = detail

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, *args, **kwargs):
        return LLMResponse(
            content=self._detail,
            finish_reason="error",
            error_classification=ErrorClassification("upstream_transport_failure", retryable=False),
        )

    async def chat_stream(self, *args, **kwargs):  # pragma: no cover - the non-stream path is under test
        raise NotImplementedError


@pytest.mark.asyncio
async def test_a_failure_the_provider_worded_itself_is_bounded_too(workspace):
    """Not every error response carries the canonical sentence: the
    transport-failure account is the provider's own prose and is passed through
    whole. It travels into a chat reply, a session marker and a cron job record
    like any other, so the same ceiling applies to it."""
    from raven.providers.base import LLM_ERROR_DETAIL_MAX

    agent = _agent(workspace, _WordsItsOwnFailure("the upstream said it failed: " + "x" * 5000), delays=())

    with pytest.raises(AnswerlessTurnError) as failed:
        await _turn(agent)

    detail = str(failed.value)
    assert len(detail) == LLM_ERROR_DETAIL_MAX and detail.endswith("...")
    assert detail.startswith("the upstream said it failed: ")


@pytest.mark.asyncio
async def test_a_canonical_sentence_is_not_cut_a_second_time(workspace):
    """Its detail was bounded where the sentence was built, so the whole
    sentence is longer than the bound by the width of its head. Cutting it
    again here would eat the detail the head promises a reader."""
    from raven.providers.base import LLM_ERROR_DETAIL_MAX, canonical_llm_error

    sentence = canonical_llm_error("invalid_request", "ppt", "y" * 5000)
    assert len(sentence) > LLM_ERROR_DETAIL_MAX
    agent = _agent(workspace, _WordsItsOwnFailure(sentence), delays=())

    with pytest.raises(AnswerlessTurnError) as failed:
        await _turn(agent)

    assert str(failed.value) == sentence


class _StallsThenAnswers(LLMProvider):
    """Streams a word, then stalls (the idle cap's TimeoutError); answers whole next time."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, *args, **kwargs):  # pragma: no cover - the stream path is under test
        raise NotImplementedError

    async def chat_stream(self, *args, **kwargs):
        from raven.providers.base import ChatDelta

        self.calls += 1
        if self.calls == 1:
            yield ChatDelta(content="partial")
            raise TimeoutError
        yield ChatDelta(content="answer")


def _streaming_agent(workspace: Path, provider: LLMProvider, *, retry_after_output: bool) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(
            max_iterations=10,
            empty_recovery=RecoveryLimits(llm_error_retry_delays=(0.0,), llm_retry_after_output=retry_after_output),
        ),
        tools=ToolWiring(restrict_to_workspace=True),
    )


async def _streamed_turn(agent: AgentLoop, seen: list[str]):
    async def on_delta(text: str) -> None:
        seen.append(text)

    return await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="build the deck",
        ),
        session_key="s1",
        on_token_delta=on_delta,
    )


@pytest.mark.asyncio
async def test_a_stall_after_streamed_output_is_not_retried_by_the_outer_ladder_unless_asked(workspace):
    """The whole loop, not the helper alone: a stream that produced a word and then
    stalled used to come back as a retryable error response, and the outer ladder
    asked again regardless of `llm_retry_after_output` -- an interactive client
    received the output of two attempts. Off, the turn fails on the first attempt;
    on, the retry's answer is the answer and the caller saw both, as agreed."""
    seen: list[str] = []
    with pytest.raises(TimeoutError):
        await _streamed_turn(_streaming_agent(workspace, _StallsThenAnswers(), retry_after_output=False), seen)
    assert seen == ["partial"]

    provider = _StallsThenAnswers()
    seen = []
    out = await _streamed_turn(_streaming_agent(workspace, provider, retry_after_output=True), seen)

    assert out is not None and out[0] == "answer"
    assert provider.calls == 2
    assert seen == ["partial", "answer"]


class _StreamsThenReportsFailure(LLMProvider):
    """Streams a word, then reports the failure as its terminal delta; answers whole
    next time. The shape the Anthropic adapter produces: it catches the exception
    its own stream raised and hands it on as a delta."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, *args, **kwargs):  # pragma: no cover - the stream path is under test
        raise NotImplementedError

    async def chat_stream(self, *args, **kwargs):
        from raven.providers.base import ChatDelta

        self.calls += 1
        if self.calls == 1:
            yield ChatDelta(content="partial")
            yield ChatDelta(
                content="Error calling LLM (server@stub): 503 upstream connect error",
                finish_reason="error",
                error_classification=ErrorClassification("server", retryable=True, should_fallback=True),
            )
            return
        yield ChatDelta(content="answer")


@pytest.mark.asyncio
async def test_a_failure_delta_after_streamed_output_is_not_retried_unless_asked(workspace):
    """The twin of the stall above, in the shape the tree's own adapter produces:
    the failure arrives as a delta rather than as a raise, so the rule that a
    rendered stream is not asked again was never reached and the outer ladder
    asked anyway -- the watcher saw the answer start over, up to four times. Off,
    the turn fails on the first attempt and says why; on, the retry's answer is
    the answer and the caller saw both, as agreed."""
    provider = _StreamsThenReportsFailure()
    seen: list[str] = []

    with pytest.raises(AnswerlessTurnError) as failed:
        await _streamed_turn(_streaming_agent(workspace, provider, retry_after_output=False), seen)

    assert provider.calls == 1, "the reader had seen words, so the ladder did not ask again"
    assert seen == ["partial"]
    assert "503 upstream connect error" in str(failed.value)

    provider = _StreamsThenReportsFailure()
    seen = []
    out = await _streamed_turn(_streaming_agent(workspace, provider, retry_after_output=True), seen)

    assert out is not None and out[0] == "answer"
    assert provider.calls == 2
    assert seen == ["partial", "answer"]


class _ThinksThenStalls(LLMProvider):
    """Streams a long thought and a half-built tool call, then stalls; answers next time."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, *args, **kwargs):  # pragma: no cover - the stream path is under test
        raise NotImplementedError

    async def chat_stream(self, *args, **kwargs):
        from raven.providers.base import ChatDelta

        self.calls += 1
        if self.calls == 1:
            yield ChatDelta(content=None, reasoning_content="weighing the layout")
            yield ChatDelta(
                content=None,
                tool_call_delta={
                    "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "write_file", "arguments": '{"pa'}}]
                },
            )
            raise TimeoutError
        yield ChatDelta(content="answer")


@pytest.mark.asyncio
async def test_a_stall_during_a_silent_think_is_asked_again(workspace):
    """Nothing reached the caller, so asking again repeats nothing. A thought is not
    the reply unless the caller wired `on_reasoning_delta`, and the fragments of a
    tool call are never rendered as one -- counting either as output made the long
    quiet round, the one that writes a whole build script, the one round a dropped
    stream could always end."""
    provider = _ThinksThenStalls()
    seen: list[str] = []

    out = await _streamed_turn(_streaming_agent(workspace, provider, retry_after_output=False), seen)

    assert out is not None and out[0] == "answer"
    assert provider.calls == 2
    assert seen == ["answer"], "the watcher saw the reply once"
    assert "weighing" not in (out[0] or ""), "the abandoned thought is not part of the reply"


@pytest.mark.asyncio
async def test_what_the_failed_attempt_left_behind_is_dropped():
    """`stream_llm_call` directly, for the invariant the loop cannot show: every
    retry starts from empty buffers. While a retry was only possible with all three
    empty, the paths that ask again did not have to clear them; once a silent think
    retries, a slot left standing merges with the next attempt's fragments into a
    call the model never made, and a thinking block keeps the signature of a
    generation that no longer exists."""
    from raven.providers.base import ChatDelta
    from raven.providers.streaming import stream_llm_call

    class _HalfCallThenAnswers(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")
            self.calls = 0

        def get_default_model(self) -> str:
            return "stub"

        async def chat(self, *args, **kwargs):  # pragma: no cover - the stream path is under test
            raise NotImplementedError

        async def chat_stream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield ChatDelta(content=None, reasoning_content="first thought")
                yield ChatDelta(
                    content=None,
                    tool_call_delta={
                        "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "exec", "arguments": '{"comm'}}]
                    },
                    thinking_blocks=[{"type": "thinking", "thinking": "A", "signature": "sigA"}],
                )
                raise ConnectionError("upstream went away")
            yield ChatDelta(content="here is the plan")

    provider = _HalfCallThenAnswers()
    out = await stream_llm_call(
        provider,
        messages=[{"role": "user", "content": "go"}],
        tools=None,
        model="stub",
        on_token_delta=None,
        max_reconnects=1,
    )

    assert provider.calls == 2
    assert out.content == "here is the plan"
    assert not out.tool_calls, "no call the second attempt did not make"
    assert "first thought" not in (out.reasoning_content or "")
    assert not out.thinking_blocks, "no signed block from a generation that is gone"


@pytest.mark.production_timing
def test_the_ladder_comes_from_agents_defaults():
    class _Defaults:
        llm_error_retry_delays = [30, 60, 120]

    assert limits_from_defaults(_Defaults()).llm_error_retry_delays == (30.0, 60.0, 120.0)
    assert limits_from_defaults(object()).llm_error_retry_delays == (15.0, 30.0, 60.0)
    assert limits_from_defaults(object()).llm_retry_after_output is False

    class _Off:
        llm_error_retry_delays: list[float] = []

    assert limits_from_defaults(_Off()).llm_error_retry_delays == (), "an explicit [] turns the ladder off"

    class _Unattended:
        llm_retry_after_output = True

    assert limits_from_defaults(_Unattended()).llm_retry_after_output is True


def test_the_image_window_budget_is_a_setting() -> None:
    """``agents.defaults.imageWindowBudgetBytes`` rides on the limits; absent it is the
    measured 12 MB, and 0 is a legal value that means no standing pass."""

    class _Bounded:
        image_window_budget_bytes = 4_000_000

    class _ReactiveOnly:
        image_window_budget_bytes = 0

    assert limits_from_defaults(object()).image_window_budget_bytes == 12_000_000
    assert limits_from_defaults(_Bounded()).image_window_budget_bytes == 4_000_000
    assert limits_from_defaults(_ReactiveOnly()).image_window_budget_bytes == 0


def _picture(tag: str) -> dict:
    return image_block(f"data:image/png;base64,{tag}")


def _with_pictures() -> list[dict]:
    """Three image-bearing messages on both transports: a tool result carrying its
    captions and pictures interleaved, and two attached user messages (the shape a
    Chat Completions endpoint gets), each stamped with where its pictures came from."""
    return [
        {"role": "user", "content": "build it"},
        {
            "role": "tool",
            "tool_call_id": "1",
            "name": "ppt_template",
            "content": [
                text_block("Template page 1 -- the cover"),
                _picture("T1"),
                text_block("Template page 2"),
                _picture("T2"),
            ],
            "_image_sources": [
                {"tool": "ppt_template", "iteration": 1, "caption": "Template page 1 -- the cover"},
                {"tool": "ppt_template", "iteration": 1, "caption": "Template page 2"},
            ],
        },
        {"role": "assistant", "content": "building"},
        {
            "role": "user",
            "content": [_picture("B7")],
            "_attached_image": True,
            "_image_sources": [{"tool": "ppt_build", "iteration": 2, "caption": "Page 7 of 20: the claim"}],
        },
        {"role": "assistant", "content": "fixing"},
        {
            "role": "user",
            "content": [_picture("B7b"), _picture("B8")],
            "_attached_image": True,
            "_image_sources": [
                {"tool": "ppt_build", "iteration": 3, "caption": "Page 7 of 20: the claim"},
                {"tool": "ppt_build", "iteration": 3, "caption": "Page 8 of 20"},
            ],
        },
    ]


def _pictures_in(message: dict) -> int:
    content = message.get("content")
    return sum(1 for p in content if is_image_part(p)) if isinstance(content, list) else 0


def test_the_image_window_keeps_the_newest_pictures_and_says_what_the_rest_showed() -> None:
    """The picture the model has already looked at is replaced by a note that names
    the tool, the round and the page, and says how to see it again -- not a bare
    "elided" it has to guess about. The newest ``keep`` image-bearing messages are
    left byte for byte as they were, whatever their role."""
    messages = _with_pictures()
    before_newest = [dict(messages[3]), dict(messages[5])]

    changed, withdrawn = shrink.window_images(messages, 2)

    assert (changed, withdrawn) == (1, 2)
    assert [_pictures_in(m) for m in messages] == [0, 0, 0, 1, 0, 2]
    assert messages[3] == before_newest[0] and messages[5] == before_newest[1]
    notes = [p["text"] for p in messages[1]["content"] if p["type"] == "text"]
    assert notes[0] == "Template page 1 -- the cover", "the caption beside the picture stays"
    assert notes[1].startswith(
        '[image no longer in context: "Template page 1 -- the cover" from ppt_template at iteration 1.'
    )
    assert "only the 2 newest image-bearing result(s) keep their pictures" in notes[1]
    assert notes[1].endswith("To see it again, ask ppt_template for it again]")
    assert '"Template page 2" from ppt_template' in notes[3]
    assert messages[1]["name"] == "ppt_template" and messages[1]["tool_call_id"] == "1", "only the content changes"


def test_the_image_window_is_prefix_stable_between_iterations() -> None:
    """Between two calls only the message that just slid out of the window changes.
    Everything before it is the same object it was, including messages withdrawn on
    an earlier pass, so a cached prefix is invalidated from the slide-out point and
    not rewritten from the top."""
    messages = _with_pictures()
    shrink.window_images(messages, 2)
    first_pass = list(messages)

    messages.append({"role": "assistant", "content": "one more"})
    messages.append(
        {
            "role": "user",
            "content": [_picture("B9")],
            "_attached_image": True,
            "_image_sources": [{"tool": "ppt_build", "iteration": 4, "caption": "Page 9 of 20"}],
        }
    )
    changed, withdrawn = shrink.window_images(messages, 2)

    assert (changed, withdrawn) == (1, 1)
    same = [messages[i] is first_pass[i] for i in range(len(first_pass))]
    assert same == [True, True, True, False, True, True], "exactly the message that slid out is new"
    assert (
        _pictures_in(messages[3]) == 0
        and '"Page 7 of 20: the claim" from ppt_build at iteration 2' in messages[3]["content"][0]["text"]
    )
    assert shrink.window_images(messages, 2) == (0, 0), "a second pass over the same list is a no-op"


def _batch(tag: str, pictures: int, iteration: int) -> dict:
    """An attached batch of `pictures` renders, each 4 wire bytes of payload ("AAAA")."""
    return {
        "role": "user",
        "content": [_picture("AAAA") for _ in range(pictures)],
        "_attached_image": True,
        "_image_sources": [
            {"tool": "ppt_build", "iteration": iteration, "caption": f"Page {n} of 20"} for n in range(pictures)
        ],
    }


def test_the_budget_window_collapses_once_when_the_pictures_outgrow_it_and_not_before() -> None:
    """Pictures stay while they fit; the moment they do not, everything but the newest
    ``keep`` goes in one collapse. The prefix breaks on that call and on no other, so a
    deck that adds a batch per build breaks its cached prefix a handful of times
    rather than once per build -- the count is the point, hence the assertion on it."""
    messages: list[dict] = [{"role": "user", "content": "build it"}]
    breaks = []
    for arrival in range(1, 8):
        messages.append({"role": "assistant", "content": f"round {arrival}"})
        messages.append(_batch(f"b{arrival}", pictures=3, iteration=arrival))  # 12 wire bytes per batch
        before = list(messages)
        changed, withdrawn = shrink.window_images(messages, 2, budget=40, reason="budget")
        breaks.append(changed)
        untouched = [messages[i] is before[i] for i in range(len(before))]
        if changed:
            assert withdrawn == 3 * changed
            assert sum(1 for same in untouched if not same) == changed, "only the collapsed messages are new objects"
        else:
            assert all(untouched), "a call under budget rewrites nothing"

    # batches 1-3 fit (36 B); the 4th tips the total over 40 -> collapse to the newest 2;
    # 5 fits again (36 B); the 6th tips it -> collapse; 7 fits. Two breaks in seven builds.
    assert breaks == [0, 0, 0, 2, 0, 2, 0]
    live = [i for i, m in enumerate(messages) if _pictures_in(m)]
    assert len(live) == 3 and live[-1] == len(messages) - 1, "the newest batches are the ones still in view"
    note = next(p["text"] for m in messages if m.get("_attached_image") for p in m["content"] if p["type"] == "text")
    assert "outgrew their byte budget" in note and "only the 2 newest image-bearing result(s) keep theirs" in note
    assert shrink.window_images(messages, 2, budget=40, reason="budget") == (0, 0), "idempotent under budget"


def test_the_budget_weighs_inline_bytes_only_and_the_users_picture_is_outside_it() -> None:
    remote = {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}
    sketch = {"role": "user", "content": [_picture("SKETCHSKETCH")]}
    messages = [
        sketch,
        {"role": "tool", "tool_call_id": "1", "content": [remote]},
        _batch("b1", 2, 1),
        _batch("b2", 2, 2),
    ]

    # 16 wire bytes across the two batches, the remote reference weighing nothing.
    assert shrink.window_images(messages, 1, budget=16, reason="budget") == (0, 0)
    assert shrink.window_images(messages, 1, budget=15, reason="budget") == (2, 3)
    assert messages[0] is sketch, "the user's own picture is neither weighed nor withdrawn"


def test_the_budget_is_charged_the_base64_the_request_carries_not_the_bytes_it_decodes_to() -> None:
    """The budget bounds the request body, and a data URI travels encoded.

    Counted on the decoded side, 11.24 MB of pictures passed the 12 MB default on a
    request that put 16.8 MB on the wire; the gateway answered it with an empty 200
    and zero usage, five times over on a byte-identical payload. The ratio is 4/3, so
    the pin is a budget that sits between the two readings of the same picture: it has
    to collapse the window, and would not have before.
    """
    payload = "A" * 400  # 400 base64 characters, 300 bytes decoded
    messages = [
        {"role": "tool", "tool_call_id": "1", "content": [image_block(f"data:image/png;base64,{payload}")]},
        {"role": "assistant", "content": "looked"},
        {"role": "tool", "tool_call_id": "2", "content": [image_block(f"data:image/png;base64,{payload}")]},
    ]

    assert shrink.window_images(messages, 1, budget=800, reason="budget") == (0, 0), "two pictures, 800 wire bytes"
    assert shrink.window_images(messages, 1, budget=700, reason="budget") == (1, 1), (
        "600 decoded would have fit 700; 800 on the wire does not"
    )


def test_a_picture_without_provenance_is_still_accounted_for() -> None:
    """A message that arrived without a stamp (a transport the loop did not build,
    an older transcript) gets a note that counts the picture rather than naming it,
    and never a note that claims a tool it cannot know."""
    messages = [
        {"role": "tool", "tool_call_id": "1", "content": [_picture("a"), _picture("b")]},
        {"role": "user", "content": [_picture("c")], "_attached_image": True},
    ]

    assert shrink.window_images(messages, 1) == (1, 2)
    notes = [p["text"] for p in messages[0]["content"]]
    assert notes[0].startswith("[image no longer in context: picture 1 of 2.")
    assert notes[1].startswith("[image no longer in context: picture 2 of 2.")
    assert notes[0].endswith("To see it again, ask for it again]")


def test_a_picture_the_user_sent_is_not_part_of_the_window() -> None:
    """The user's own picture is the subject of the turn, not a result the model has
    finished with: however many renders follow it, it stays -- until the endpoint has
    refused the request and the window is closed with nothing else left to take out."""
    sketch = {"role": "user", "content": [text_block("make it look like this"), _picture("SKETCH")]}
    messages = [sketch, *_with_pictures()[1:]]

    assert shrink.window_images(messages, 1) == (2, 3)
    assert messages[0] is sketch and _pictures_in(messages[0]) == 1

    assert shrink.window_images(messages, 0, reason="refused") == (1, 2), "the tool pictures, not the sketch"
    assert _pictures_in(messages[0]) == 1
    assert shrink.window_images(messages, 0, reason="refused", any_role=True) == (1, 1)
    assert _pictures_in(messages[0]) == 0 and "refused this request's pictures" in messages[0]["content"][1]["text"]


def test_a_closed_window_withdraws_the_newest_picture_and_says_why() -> None:
    """Window zero is what a size refusal leaves behind: every picture comes out,
    the newest included, and the note blames the endpoint rather than a newer batch."""
    messages = _with_pictures()

    assert shrink.window_images(messages, 0, reason="refused") == (3, 5)
    assert not any(_pictures_in(m) for m in messages)
    assert "The endpoint refused this request's pictures as too large" in messages[5]["content"][1]["text"]
    assert "Page 8 of 20" in messages[5]["content"][1]["text"]

    # The standing pass with the window already closed reads differently again: the
    # picture was never shown, and the model is told none will be for this turn.
    later = [{"role": "user", "content": [_picture("z")], "_attached_image": True}]
    shrink.window_images(later, 0)
    assert "so none are kept in context now" in later[0]["content"][0]["text"]


def test_the_overflow_path_uses_the_same_notes() -> None:
    """``shrink.emergency_shrink`` keeps its tighter count and its copy-and-return contract,
    and the picture it drops gets the same kind of note, with the overflow as the reason."""
    messages = _with_pictures()
    out, elided = shrink.elide_older_images(messages)

    assert elided == 2 and out is not messages
    assert [_pictures_in(m) for m in messages] == [0, 2, 0, 1, 0, 2], "the input is left alone"
    assert [_pictures_in(m) for m in out] == [0, 0, 0, 0, 0, 2]
    assert "elided to fit the context window" in out[3]["content"][0]["text"]
    assert shrink.elide_older_images(out) == (out, 0)


def test_the_overflow_path_sheds_the_users_own_pictures_last() -> None:
    """A turn that is nothing but pasted screenshots overflows on them alone, and the
    overflow path could always reach them: they go after every result picture and
    tool body, newest kept, with the same note. While a result picture is still in
    the transcript it goes first and the user's stay."""
    pasted = [
        {"role": "user", "content": [text_block("first screenshot"), _picture("U1")]},
        {"role": "assistant", "content": "seen"},
        {"role": "user", "content": [text_block("second"), _picture("U2")]},
        {"role": "assistant", "content": "seen"},
        {"role": "user", "content": [text_block("third"), _picture("U3")]},
    ]
    out, elided = shrink.emergency_shrink(pasted)

    assert elided == 2 and out is not pasted
    assert [_pictures_in(m) for m in pasted] == [1, 0, 1, 0, 1], "the input is left alone"
    assert [_pictures_in(m) for m in out] == [0, 0, 0, 0, 1]
    assert "elided to fit the context window" in out[0]["content"][1]["text"]
    assert shrink.emergency_shrink(out) == (out, 0)

    mixed = [*_with_pictures(), {"role": "user", "content": [_picture("U9")]}]
    first, freed = shrink.emergency_shrink(mixed)
    assert freed == 2 and _pictures_in(first[-1]) == 1, "result pictures go first, the user's stays"
    again, freed_again = shrink.emergency_shrink(first)
    assert freed_again == 1 and _pictures_in(again[5]) == 0, "then the older result picture goes"
    assert _pictures_in(again[-1]) == 1, "the newest picture, the user's, is kept"
    assert shrink.emergency_shrink(again) == (again, 0)


class _PageRender(Tool):
    """A tool that answers with a caption and a picture, the way the deck tools do."""

    @property
    def name(self) -> str:
        return "page_render"

    @property
    def description(self) -> str:
        return "renders one page"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"page": {"type": "integer"}}}

    async def execute(self, page: int = 7, **_) -> ToolResult:
        caption = f"Page {page} of 20: the claim"
        return ToolResult(model_text=caption, blocks=[text_block(caption), _picture(f"P{page}" * 40)])


class _ShowsThenRefuses(LLMProvider):
    """Asks for a render, refuses the request that carries it as too large, asks for
    another render once that is settled, then answers. Records every request."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0
        self.seen: list[list[dict]] = []

    def get_default_model(self) -> str:
        return "stub"

    _CHAT_RETRY_DELAYS = ()

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, **_):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        renders = sum(1 for m in messages if m.get("role") == "tool")
        pictures = sum(_pictures_in(m) for m in messages)
        if renders == 0:
            return LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[ToolCallRequest(id="c1", name="page_render", arguments={"page": 7})],
            )
        if pictures:
            return LLMResponse(
                content="Error calling LLM (image_too_large): Downloaded image content cannot exceed 30MB",
                finish_reason="error",
                error_classification=ErrorClassification("image_too_large", strip_images=True),
            )
        if renders == 1:
            return LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[ToolCallRequest(id="c2", name="page_render", arguments={"page": 8})],
            )
        return LLMResponse(content="done", finish_reason="stop")


@pytest.mark.asyncio
async def test_a_size_refusal_closes_the_window_for_the_rest_of_the_turn(workspace):
    """The refusal is answered by withdrawing the pictures and asking again, and the
    narrower window then stands: the next render is withdrawn before it is ever sent,
    so the same refusal cannot come back a few calls later the way it did on both
    measured runs. The pictures the loop attaches carry their provenance, and neither
    that stamp nor the notes reach the session file."""
    provider = _ShowsThenRefuses()
    agent = _agent(workspace, provider, delays=(0.0,))
    agent.tools.register(_PageRender())

    out = await _turn(agent)

    assert out is not None and out[0] == "done"
    assert provider.calls == 4  # asks for page 7; refused; asked again, asks for page 8; answers
    refused = provider.seen[1]
    attached = refused[-1]
    assert attached.get("_attached_image") and _pictures_in(attached) == 1
    assert attached["_image_sources"] == [{"tool": "page_render", "iteration": 1, "caption": "Page 7 of 20: the claim"}]

    retried = provider.seen[2]
    assert not any(_pictures_in(m) for m in retried)
    note = retried[-1]["content"][0]["text"]
    assert note.startswith('[image no longer in context: "Page 7 of 20: the claim" from page_render at iteration 1.')
    assert "refused this request's pictures as too large" in note

    # Window at zero from here: the page 8 render was withdrawn before the call.
    final = provider.seen[3]
    assert not any(_pictures_in(m) for m in final)
    assert "so none are kept in context now" in final[-1]["content"][0]["text"]
    assert '"Page 8 of 20: the claim" from page_render at iteration 2' in final[-1]["content"][0]["text"]

    persisted = " ".join(p.read_text(encoding="utf-8") for p in (workspace / "sessions").rglob("*.jsonl"))
    assert persisted, "the turn was filed"
    assert "_image_sources" not in persisted and "image no longer in context" not in persisted


class _ShowsThreePages(LLMProvider):
    """Asks for three renders, one per call, then answers. Records every request."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0
        self.seen: list[list[dict]] = []

    def get_default_model(self) -> str:
        return "stub"

    _CHAT_RETRY_DELAYS = ()

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, **_):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self.calls <= 3:
            return LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(id=f"c{self.calls}", name="page_render", arguments={"page": 6 + self.calls})
                ],
            )
        return LLMResponse(content="done", finish_reason="stop")


def _agent_with_budget(workspace: Path, provider: LLMProvider, budget: int) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(
            max_iterations=10,
            empty_recovery=RecoveryLimits(llm_error_retry_delays=(), image_window_budget_bytes=budget),
        ),
        tools=ToolWiring(restrict_to_workspace=True),
    )


@pytest.mark.asyncio
async def test_the_configured_budget_collapses_the_window_and_zero_leaves_it_open(workspace):
    """The budget the loop enforces is the one on its limits. At one byte the third
    render tips the total and the oldest picture is withdrawn with the budget note; at
    zero no standing pass runs, so every picture stays until an endpoint refuses."""
    bounded = _ShowsThreePages()
    agent = _agent_with_budget(workspace, bounded, budget=1)
    agent.tools.register(_PageRender())
    out = await _turn(agent)
    assert out is not None and out[0] == "done" and bounded.calls == 4
    final = bounded.seen[3]
    bearing = [m for m in final if m.get("_attached_image") or _pictures_in(m)]
    assert [_pictures_in(m) for m in bearing] == [0, 1, 1], "the oldest collapsed, the newest two kept"
    assert "outgrew their byte budget" in bearing[0]["content"][0]["text"]

    open_window = _ShowsThreePages()
    agent = _agent_with_budget(workspace, open_window, budget=0)
    agent.tools.register(_PageRender())
    out = await _turn(agent)
    assert out is not None and out[0] == "done" and open_window.calls == 4
    assert sum(_pictures_in(m) for m in open_window.seen[3]) == 3, "no standing pass: every render is still there"
    assert not any("no longer in context" in str(m.get("content")) for m in open_window.seen[3])


class _ShowsOnce(LLMProvider):
    """Asks for one render, then answers. Records every request."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0
        self.seen: list[list[dict]] = []

    def get_default_model(self) -> str:
        return "stub"

    _CHAT_RETRY_DELAYS = ()

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, **_):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self.calls == 1:
            return LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[ToolCallRequest(id="c1", name="page_render", arguments={"page": 7})],
            )
        return LLMResponse(content="done", finish_reason="stop")


@pytest.mark.asyncio
async def test_the_tool_result_transport_stamps_the_same_provenance(workspace):
    """On a transport that carries a picture inside the ``tool`` message (the default
    for the Anthropic family), the stamp is written on that message, one entry per
    picture, the same entry the attached transport writes."""
    provider = _ShowsOnce()
    agent = _agent(workspace, provider, delays=())
    agent._image_tool_result_ok["stub"] = True
    agent.tools.register(_PageRender())

    out = await _turn(agent)

    assert out is not None and out[0] == "done" and provider.calls == 2
    carried = [m for m in provider.seen[1] if m.get("role") == "tool" and _pictures_in(m)]
    assert len(carried) == 1, "the picture rode inside the tool result"
    assert carried[0]["_image_sources"] == [
        {"tool": "page_render", "iteration": 1, "caption": "Page 7 of 20: the claim"}
    ]
    assert not any(m.get("_attached_image") for m in provider.seen[1]), "nothing was attached to a user message"


@pytest.mark.asyncio
async def test_a_note_on_the_tool_result_transport_is_filed_with_its_facts_only(workspace):
    """The tool message survives the turn on this transport, so the note it carries
    is filed: which picture, which tool, which round -- not why it left this turn or
    how to ask for it again, which a resumed session would read as current."""
    provider = _ShowsThenRefuses()
    agent = _agent(workspace, provider, delays=(0.0,))
    agent._image_tool_result_ok["stub"] = True
    agent.tools.register(_PageRender())

    out = await _turn(agent)

    assert out is not None and out[0] == "done"
    live = [m for m in provider.seen[2] if m.get("role") == "tool"][-1]["content"][1]["text"]
    assert live.startswith(
        '[image no longer in context: "Page 7 of 20: the claim" from page_render at iteration 1. The endpoint refused'
    )
    assert live.endswith("ask page_render for it again]")

    persisted = " ".join(p.read_text(encoding="utf-8") for p in (workspace / "sessions").rglob("*.jsonl"))
    assert "from page_render at iteration 1.]" in persisted and "Page 7 of 20: the claim" in persisted
    assert "refused this request" not in persisted and "To see it again" not in persisted
    assert "_image_sources" not in persisted


@pytest.mark.parametrize(
    ("reason", "keep"),
    [("refused", 2), ("context", 2), ("budget", 2), ("superseded", 0), ("superseded", 2)],
)
@pytest.mark.parametrize(
    "source",
    [
        {"caption": "Page 7 of 20. The claim, then the proof.", "tool": "page_render", "iteration": 3},
        {"caption": None, "tool": None, "iteration": None},
    ],
)
def test_every_reason_the_note_can_give_files_down_to_its_facts(reason, keep, source):
    """The composer and the filed-form parser share the reasons' opening words; this
    composes every reason the loop can pass and files it, so a reworded opening on
    either side fails here instead of filing the turn's business into the session.
    The caption carries the sentence break the parser must read past."""
    from raven.agent.window.images import filed_image_note, withdrawn_image_note

    live = withdrawn_image_note(source, index=1, total=2, reason=reason, keep=keep)
    facts = live.partition(". ")[0] if source["caption"] is None else live[: live.index(". ", live.index("iteration"))]

    assert filed_image_note(live) == f"{facts}.]"
    assert facts.startswith("[image no longer in context: ") and "To see it again" not in facts
    assert filed_image_note("plain tool text. It was elided by nobody]") == "plain tool text. It was elided by nobody]"


@pytest.mark.asyncio
async def test_an_image_refusal_with_nothing_to_withdraw_is_not_waited_out(workspace):
    """No picture in the conversation means the refusal was about something else, and
    `image_too_large` is not retryable: the turn ends at once rather than after the
    error ladder."""
    provider = _FailsThenAnswers(5, ErrorClassification("image_too_large", strip_images=True))
    agent = _agent(workspace, provider, delays=(0.0, 0.0, 0.0))

    with pytest.raises(AnswerlessTurnError) as failed:
        await _turn(agent)

    assert provider.calls == 1
    assert "Error calling LLM" in str(failed.value)


async def _watch(text: str) -> None:
    """A caller watching the reply form: what puts a spawn on the streamed path."""


class _DropsTwiceThenAnswers(LLMProvider):
    """The connection drops before the first delta twice, then the stream answers
    whole: one failure more than the single reconnect the helper grants on its own,
    so only a ladder handed down from the deployment reaches the answer."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, *args, **kwargs):  # pragma: no cover - the fallback this test must not take
        raise AssertionError("the spawn fell back to the waited-for call")

    async def chat_stream(self, *args, **kwargs):
        from raven.providers.base import ChatDelta

        self.calls += 1
        if self.calls <= 2:
            raise ConnectionError("connection reset by peer")
        yield ChatDelta(content="answer")


class _ShowsThenDrops(LLMProvider):
    """Streams a word, then the connection drops; answers whole next time."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, *args, **kwargs):  # pragma: no cover - the fallback this test must not take
        raise AssertionError("the spawn fell back to the waited-for call")

    async def chat_stream(self, *args, **kwargs):
        from raven.providers.base import ChatDelta

        self.calls += 1
        if self.calls == 1:
            yield ChatDelta(content="partial")
            raise ConnectionError("connection reset by peer")
        yield ChatDelta(content="answer")


@pytest.mark.asyncio
async def test_a_spawned_subagents_dropped_stream_is_asked_again_on_the_deployments_ladder(workspace):
    """The subagent half of the wiring: `RavenLoopBackend` hands the ladder it was built
    with to `stream_llm_call`. Left to that call's own defaults a spawn reconnects once
    and waits never, so a stream dropped twice ended the run; with the ladder handed
    over, the third call answers and its answer is the run's result."""
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

    provider = _DropsTwiceThenAnswers()
    backend = RavenLoopBackend(provider=provider, model="stub", agent_home=workspace / "home", retry_delays=(0.0,))

    out = await backend.run("build the deck", task_id="t1", workspace=workspace, executor=None, on_delta=_watch)

    assert provider.calls == 3
    assert "answer" in (out or "")


@pytest.mark.asyncio
async def test_a_spawned_subagents_rendered_stream_is_retried_only_when_the_deployment_says_so(workspace):
    """`llmRetryAfterOutput` reaches a spawn the same way. Built with it on, a stream
    that showed a word and then dropped is asked again and the retry's word is the
    answer; built without it, the default, the drop ends the run as the rule has
    always read."""
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

    seen: list[str] = []

    async def watch(text: str) -> None:
        seen.append(text)

    provider = _ShowsThenDrops()
    backend = RavenLoopBackend(
        provider=provider, model="stub", agent_home=workspace / "home", retry_delays=(0.0,), retry_after_output=True
    )
    out = await backend.run("build the deck", task_id="t1", workspace=workspace, executor=None, on_delta=watch)
    assert provider.calls == 2
    assert "answer" in (out or "")
    assert seen == ["partial", "answer"]

    provider = _ShowsThenDrops()
    backend = RavenLoopBackend(provider=provider, model="stub", agent_home=workspace / "home", retry_delays=(0.0,))
    with pytest.raises(ConnectionError):
        await backend.run("build the deck", task_id="t1", workspace=workspace, executor=None, on_delta=_watch)
    assert provider.calls == 1
