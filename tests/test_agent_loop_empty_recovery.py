"""Empty / thinking-only response recovery.

A turn that ends with no visible text is usually a weak-model dud, not a real
"done". The loop recovers the turn (bounded per turn) before it fails, and the
synthetic scaffolding never reaches persisted history.

The decision lives in ``raven.agent.loop.recovery`` as pure functions
(``reply_kind``, ``decide``) and is unit-tested in isolation; the loop tests
cover the side effects (what each request carries, which model and effort it
asks for, what a failure says). Most of them run the real ``LiteLLMProvider``
over a scripted ``acompletion``, so the facts the decision reads -- the finish,
the usage, the reasoning -- are made by the adapter and not by the test.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from raven.agent.hook import AgentHook
from raven.agent.loop import AgentLoop, recovery, turn_path
from raven.agent.loop._shared import _MAX_ITER_STATIC_FALLBACK
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
from raven.agent.loop.recovery import (
    EMPTY_REPLY_FAILURE,
    MAX_CONTINUATIONS,
    NETWORK_CUT_CONTINUATION,
    OUTPUT_LIMIT_CONTINUATION,
    PARTIAL_STREAM_NOTE,
    POST_TOOL_NUDGE,
    REASONING_EXCERPT_MAX,
    REFUSAL_EXPLANATION,
    REFUSAL_FAILURE,
    REFUSAL_NO_EXPLANATION,
    THINKING_ONLY_FAILURE,
    TRUNCATED_FAILURE,
    EmptyAttempt,
    RecoveryAction,
    RecoveryLimits,
    RecoveryState,
    ReplyKind,
    SilenceGate,
    decide,
    deterministic_empty,
    empty_retry_delay,
    has_inline_thinking,
    has_thinking,
    limits_from_defaults,
    next_step_down,
    reasoning_excerpt,
    reply_kind,
    resend_budget,
    strip_think_blocks,
)
from raven.agent.window import shrink
from raven.config.schema import CompactionConfig
from raven.contracts.llm_provider import REASONING_EFFORT_LADDER
from raven.providers import rates
from raven.providers.base import (
    ChatDelta,
    ErrorClassification,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    send_max_tokens,
)
from raven.providers.litellm_provider import LiteLLMProvider
from raven.providers.streaming import STREAM_CUT
from raven.spine.events import NoticeKind
from raven.spine.message import ChatType, Source
from raven.spine.turn import AnswerlessTurnError, Origin, TurnRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _turn(text: str = "hi", origin: Origin = Origin.USER) -> TurnRequest:
    return TurnRequest(
        origin=origin,
        source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text=text,
    )


async def _run_answerless(agent: AgentLoop, text: str = "hi", **kwargs) -> str:
    """Run a turn that ends with no answer, and return the failure it reports.

    A turn the model never answered fails rather than returning a reply, so every
    test below whose provider stays empty to the end asks for its work through
    here: the assertions about what the ladder sent are unchanged, and the exit
    they are reached through is held in one place.
    """
    with pytest.raises(AnswerlessTurnError) as failed:
        await agent._process_message(_turn(text), session_key="s1", **kwargs)
    return str(failed.value)


def _make_agent(
    workspace: Path,
    provider: LLMProvider,
    limits: RecoveryLimits | None = None,
    *,
    max_iterations: int = 10,
    hooks=(),
) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=max_iterations, empty_recovery=limits),
        host=HostWiring(hooks=list(hooks)),
        tools=ToolWiring(restrict_to_workspace=True),
    )


# --------------------------------------------------------------------------- #
# unit: thinking detection                                                     #
# --------------------------------------------------------------------------- #


def test_has_inline_thinking():
    assert has_inline_thinking("<think>...</think>") is True
    assert has_inline_thinking("<THINKING>x") is True
    assert has_inline_thinking("plain text") is False
    assert has_inline_thinking(None) is False
    assert has_inline_thinking("") is False


def test_has_thinking():
    assert has_thinking(LLMResponse(content=None, reasoning_content="hmm")) is True
    assert has_thinking(LLMResponse(content=None, thinking_blocks=[{"x": 1}])) is True
    assert has_thinking(LLMResponse(content="<think>...</think>")) is True
    assert has_thinking(LLMResponse(content="real answer")) is False
    assert has_thinking(LLMResponse(content=None)) is False


def test_limits_from_defaults_maps_fields():
    class _D:
        empty_recovery_enabled = False
        post_tool_empty_max_nudges = 5
        thinking_prefill_max_retries = 6
        empty_content_max_retries = 7

    limits = limits_from_defaults(_D())
    assert limits == RecoveryLimits(
        enabled=False,
        post_tool_empty_max_nudges=5,
        thinking_prefill_max_retries=6,
        empty_content_max_retries=7,
    )


def test_limits_from_defaults_uses_defaults_for_missing_attrs():
    assert limits_from_defaults(object()) == RecoveryLimits()


# --------------------------------------------------------------------------- #
# unit: reply kind and the recovery decision                                   #
# --------------------------------------------------------------------------- #


def _kind(content, finish="stop", *, origin=Origin.USER, streamed=False, prev_had_tool_calls=False, **fields):
    """``reply_kind`` asked the way the loop asks it, of the text left once think blocks are out."""
    response = LLMResponse(content=content or None, finish_reason=finish, **fields)
    visible = (strip_think_blocks(content) or None) if content else None
    return reply_kind(response, visible, origin=origin, prev_had_tool_calls=prev_had_tool_calls, streamed=streamed)


@pytest.mark.parametrize(
    "content,finish,extra,origin,streamed,kind",
    [
        ("[SILENT]", "stop", {}, Origin.USER, False, ReplyKind.SILENT),
        ("[SILENT] nothing new", "stop", {}, Origin.USER, False, ReplyKind.TEXT),
        ("[SILENT] nothing new", "stop", {}, Origin.CRON, False, ReplyKind.SILENT),
        ("", "content_filter", {}, Origin.USER, False, ReplyKind.REFUSAL),
        ("I can't", "refusal", {}, Origin.USER, False, ReplyKind.REFUSAL),
        ("partial", STREAM_CUT, {}, Origin.USER, True, ReplyKind.NETWORK_CUT),
        # Only a stream that died is cut: a clean close without a terminal chunk, and a whole body that
        # names no finish, both read ``unknown`` and are whole answers.
        ("partial", "unknown", {}, Origin.USER, True, ReplyKind.TEXT),
        ("partial", "unknown", {}, Origin.USER, False, ReplyKind.TEXT),
        ("partial", STREAM_CUT, {}, Origin.USER, False, ReplyKind.TEXT),
        ("", "stop", {"reasoning_content": "hmm"}, Origin.USER, False, ReplyKind.THINKING_ONLY),
        ("", "stop", {"usage": {"reasoning_tokens": 5}}, Origin.USER, False, ReplyKind.THINKING_ONLY),
        ("", "length", {"truncated": True}, Origin.USER, False, ReplyKind.TRUNCATED),
        ("", "stop", {}, Origin.USER, False, ReplyKind.EMPTY),
        # A refusal outranks the text that came with it: the text is the model's explanation.
        ("I can't help with that", "content_filter", {}, Origin.CRON, True, ReplyKind.REFUSAL),
        # A cut stream needs something to have streamed.
        ("", "unknown", {}, Origin.USER, True, ReplyKind.EMPTY),
        ("", STREAM_CUT, {}, Origin.USER, True, ReplyKind.EMPTY),
        ("<think>hmm</think>", "stop", {}, Origin.USER, False, ReplyKind.THINKING_ONLY),
        (
            "",
            "stop",
            {"thinking_blocks": [{"type": "redacted_thinking", "data": "x"}]},
            Origin.USER,
            False,
            ReplyKind.THINKING_ONLY,
        ),
        # Thinking says why a cut reply is empty, so it is named before the cut.
        ("", "length", {"truncated": True, "reasoning_content": "hmm"}, Origin.USER, False, ReplyKind.THINKING_ONLY),
        # A count the providers' reader does not accept is no evidence of thinking.
        ("", "stop", {"usage": {"reasoning_tokens": "n/a"}}, Origin.USER, False, ReplyKind.EMPTY),
        ("", "stop", {"usage": {"reasoning_tokens": True}}, Origin.USER, False, ReplyKind.EMPTY),
    ],
)
def test_reply_kind(content, finish, extra, origin, streamed, kind):
    assert _kind(content, finish, origin=origin, streamed=streamed, **extra) is kind


def test_an_empty_reply_after_tools_is_its_own_kind():
    assert _kind("", prev_had_tool_calls=True) is ReplyKind.POST_TOOL_EMPTY
    assert _kind("", prev_had_tool_calls=True, reasoning_content="hmm") is ReplyKind.THINKING_ONLY
    assert _kind("", "length", prev_had_tool_calls=True, truncated=True) is ReplyKind.TRUNCATED
    assert _kind("done", prev_had_tool_calls=True) is ReplyKind.TEXT


def test_blank_visible_text_is_no_text():
    response = LLMResponse(content="  \n ")
    assert (
        reply_kind(response, "  \n ", origin=Origin.USER, prev_had_tool_calls=False, streamed=True) is ReplyKind.EMPTY
    )


def _state(**fields) -> RecoveryState:
    return RecoveryState(rungs_used=set(), **fields)


_ON = RecoveryLimits()
_OFF = RecoveryLimits(enabled=False)
_ZERO_OUTPUT = EmptyAttempt(model="m", finish_reason="stop", usage_present=True, zero_output=True)
_PARTIAL = ["The answer is "]
_K = ReplyKind
_A = RecoveryAction
# Every other move open, so a wrong answer cannot hide behind a closed one.
_ROOM = {"fallback_left": True, "next_rung": "high"}


def _row(case, kind, action, *, state=None, limits=_ON, **asked):
    """One table row; the facts the loop supplies default to a prefill-taking provider with nothing left to try."""
    asked = {"prefill_supported": True, "next_rung": None, "fallback_left": False, "resend_budget": 3, **asked}
    return pytest.param(kind, state or _state(), limits, asked, action, id=case)


_DECIDE_TABLE = [
    # Recovery off: a marker is still silence and a refusal still fails; every other reply completes the turn as it stands.
    _row("off-silent", _K.SILENT, _A.SILENT, limits=_OFF, **_ROOM),
    _row("off-refusal", _K.REFUSAL, _A.FAIL, limits=_OFF, **_ROOM),
    *(
        _row(f"off-{kind.name}", kind, _A.COMPLETE, limits=_OFF, **_ROOM)
        for kind in (_K.TEXT, _K.NETWORK_CUT, _K.THINKING_ONLY, _K.TRUNCATED, _K.POST_TOOL_EMPTY, _K.EMPTY)
    ),
    _row("text", _K.TEXT, _A.COMPLETE),
    _row("silent", _K.SILENT, _A.SILENT),
    # A continuation that came back with nothing delivers what had already streamed.
    *(
        _row(f"parts-{kind.name}", kind, _A.COMPLETE, state=_state(parts=list(_PARTIAL)), **_ROOM)
        for kind in (_K.EMPTY, _K.POST_TOOL_EMPTY, _K.THINKING_ONLY, _K.TRUNCATED)
    ),
    _row("parts-NETWORK_CUT", _K.NETWORK_CUT, _A.CONTINUE, state=_state(parts=list(_PARTIAL), continuations=1)),
    _row("parts-REFUSAL", _K.REFUSAL, _A.FALLBACK, state=_state(parts=list(_PARTIAL)), fallback_left=True),
    _row("refusal-falls-back", _K.REFUSAL, _A.FALLBACK, fallback_left=True),
    _row(
        "refusal-falls-back-once",
        _K.REFUSAL,
        _A.FAIL,
        state=_state(refusal_fell_back=True, fallbacks_used=1),
        fallback_left=True,
    ),
    _row("refusal-without-fallback", _K.REFUSAL, _A.FAIL),
    # What a refusal has spent is its own flag, not the count of fallbacks the turn used for other reasons.
    _row("refusal-after-another-fallback", _K.REFUSAL, _A.FALLBACK, state=_state(fallbacks_used=1), fallback_left=True),
    _row("cut-continues", _K.NETWORK_CUT, _A.CONTINUE, state=_state(continuations=MAX_CONTINUATIONS - 1)),
    _row("cut-spent", _K.NETWORK_CUT, _A.COMPLETE, state=_state(continuations=MAX_CONTINUATIONS), **_ROOM),
    _row("truncated-continues", _K.TRUNCATED, _A.CONTINUE),
    _row("truncated-spent", _K.TRUNCATED, _A.FAIL, state=_state(continuations=MAX_CONTINUATIONS), **_ROOM),
    _row("thinking-prefills", _K.THINKING_ONLY, _A.PREFILL, next_rung="high"),
    _row("thinking-prefills-twice", _K.THINKING_ONLY, _A.PREFILL, state=_state(prefills=1), next_rung="high"),
    _row("thinking-steps-down", _K.THINKING_ONLY, _A.STEP_DOWN, state=_state(prefills=2), **_ROOM),
    _row("thinking-no-prefill-steps-down", _K.THINKING_ONLY, _A.STEP_DOWN, prefill_supported=False, next_rung="high"),
    _row(
        "thinking-no-prefill-budget-steps-down",
        _K.THINKING_ONLY,
        _A.STEP_DOWN,
        limits=RecoveryLimits(thinking_prefill_max_retries=0),
        next_rung="high",
    ),
    _row("thinking-falls-back", _K.THINKING_ONLY, _A.FALLBACK, state=_state(prefills=2), fallback_left=True),
    _row("thinking-fails", _K.THINKING_ONLY, _A.FAIL, state=_state(prefills=2)),
    # A fallback model is tried once: no prefill, no step-down, no resend on it.
    _row("thinking-on-fallback-moves-on", _K.THINKING_ONLY, _A.FALLBACK, state=_state(fallbacks_used=1), **_ROOM),
    _row(
        "thinking-on-last-fallback-fails", _K.THINKING_ONLY, _A.FAIL, state=_state(fallbacks_used=1), next_rung="high"
    ),
    _row("post-tool-nudges", _K.POST_TOOL_EMPTY, _A.NUDGE),
    _row("post-tool-nudges-on-fallback", _K.POST_TOOL_EMPTY, _A.NUDGE, state=_state(fallbacks_used=1), **_ROOM),
    _row("post-tool-then-resends", _K.POST_TOOL_EMPTY, _A.RETRY, state=_state(nudges=1)),
    _row("post-tool-then-falls-back", _K.POST_TOOL_EMPTY, _A.FALLBACK, state=_state(nudges=1, resends=3), **_ROOM),
    _row("post-tool-then-fails", _K.POST_TOOL_EMPTY, _A.FAIL, state=_state(nudges=1, resends=3)),
    _row("post-tool-then-moves-on", _K.POST_TOOL_EMPTY, _A.FALLBACK, state=_state(nudges=1, fallbacks_used=1), **_ROOM),
    _row(
        "post-tool-no-nudge-budget",
        _K.POST_TOOL_EMPTY,
        _A.RETRY,
        limits=RecoveryLimits(post_tool_empty_max_nudges=0),
    ),
    _row("empty-resends", _K.EMPTY, _A.RETRY),
    _row("empty-resends-a-third-time", _K.EMPTY, _A.RETRY, state=_state(resends=2)),
    _row("empty-falls-back", _K.EMPTY, _A.FALLBACK, state=_state(resends=3), fallback_left=True),
    _row("empty-fails", _K.EMPTY, _A.FAIL, state=_state(resends=3)),
    _row("empty-expensive-resent-once", _K.EMPTY, _A.FAIL, state=_state(resends=1), resend_budget=1),
    _row("empty-no-resend-budget", _K.EMPTY, _A.FALLBACK, resend_budget=0, fallback_left=True),
    # Two zero-output empties of one signature: the resends are skipped, the fallback is not.
    _row(
        "empty-deterministic-skips-resends",
        _K.EMPTY,
        _A.FALLBACK,
        state=_state(resends=1, empty_streak=[_ZERO_OUTPUT, _ZERO_OUTPUT]),
        fallback_left=True,
    ),
    _row(
        "empty-deterministic-fails",
        _K.EMPTY,
        _A.FAIL,
        state=_state(resends=1, empty_streak=[_ZERO_OUTPUT, _ZERO_OUTPUT]),
    ),
    _row("empty-one-zero-output-is-not-deterministic", _K.EMPTY, _A.RETRY, state=_state(empty_streak=[_ZERO_OUTPUT])),
    _row("empty-on-fallback-moves-on", _K.EMPTY, _A.FALLBACK, state=_state(fallbacks_used=1), fallback_left=True),
    _row("empty-on-last-fallback-fails", _K.EMPTY, _A.FAIL, state=_state(fallbacks_used=1)),
]


@pytest.mark.parametrize("kind,state,limits,asked,action", _DECIDE_TABLE)
def test_decide_table(kind, state, limits, asked, action):
    assert decide(kind, state, limits, **asked) is action


@pytest.mark.parametrize(
    "current,used,rung",
    [
        (None, (), "high"),
        (None, ("high",), "low"),
        ("max", (), "high"),
        ("xhigh", (), "high"),
        ("high", (), "low"),
        ("medium", (), "low"),
        ("low", (), None),
        ("minimal", (), None),
        ("bogus", (), None),
        ("high", ("low",), None),
        (None, ("high", "low"), None),
        ("max", ("high",), "low"),
        ("HIGH", (), "low"),
        (" Max ", (), "high"),
    ],
)
def test_next_step_down(current, used, rung):
    assert next_step_down(current, used=used) == rung


def test_a_rung_the_wire_cannot_tell_apart_is_skipped():
    assert next_step_down(None, lambda e: {"effort": "high"} if e in (None, "high") else {"effort": e}) == "low"
    assert next_step_down("max", lambda e: {}) is None


def test_the_step_down_never_leaves_high_and_low():
    wires = (None, lambda effort: {"effort": effort}, lambda effort: {}, lambda effort: {"effort": "low"})
    for current in (*REASONING_EFFORT_LADDER, None, "bogus", "", "HIGH", " Max "):
        for used in ((), ("high",), ("low",), ("high", "low")):
            for wire in wires:
                rung = next_step_down(current, wire, used=used)
                assert rung in {"high", "low", None}, (current, used, rung)
                assert rung not in used, (current, used, rung)


@pytest.mark.production_timing
def test_empty_retry_delay_is_hermes_jittered_backoff():
    assert empty_retry_delay(1, rand=lambda: 0.0) == 5.0 and empty_retry_delay(1, rand=lambda: 1.0) == 7.5
    assert empty_retry_delay(2, rand=lambda: 0.0) == 10.0 and empty_retry_delay(5, rand=lambda: 0.0) == 60.0
    assert empty_retry_delay(9, rand=lambda: 1.0) == 90.0
    assert 60.0 <= empty_retry_delay(10_000) <= 90.0
    assert empty_retry_delay(0, rand=lambda: 0.0) == 5.0


def test_the_backoff_reads_its_bounds_when_called(monkeypatch):
    """The suite shrinks the bounds to a millisecond, which works only while they are read per call."""
    monkeypatch.setattr(recovery, "EMPTY_RETRY_BASE_DELAY_S", 1.0)
    monkeypatch.setattr(recovery, "EMPTY_RETRY_MAX_DELAY_S", 3.0)
    assert [empty_retry_delay(n, rand=lambda: 0.0) for n in (1, 2, 3)] == [1.0, 2.0, 3.0]


def _attempt(model="m", finish="stop", **usage) -> EmptyAttempt:
    usage = {"prompt_tokens": 50_000, "completion_tokens": 0, **usage}
    return EmptyAttempt.of(LLMResponse(content=None, finish_reason=finish, usage=usage), model)


def test_two_zero_output_empties_of_one_signature_are_deterministic():
    zero = _attempt()
    assert zero == EmptyAttempt(model="m", finish_reason="stop", usage_present=True, zero_output=True)
    assert deterministic_empty([zero, _attempt()]) is True
    assert deterministic_empty([zero, _attempt(), _attempt()]) is True
    assert deterministic_empty([]) is False
    assert deterministic_empty([zero]) is False
    # Usage that is no evidence fails open: missing, no prompt counted, no completion reported.
    assert deterministic_empty([zero, EmptyAttempt.of(LLMResponse(content=None), "m")]) is False
    assert deterministic_empty([zero, _attempt(prompt_tokens=0)]) is False
    assert deterministic_empty([zero, _attempt(completion_tokens=None)]) is False
    assert deterministic_empty([zero, _attempt(finish="unknown")]) is False
    assert deterministic_empty([zero, _attempt(model="other")]) is False
    assert deterministic_empty([zero, _attempt(completion_tokens=3)]) is False
    assert deterministic_empty([zero, _attempt(reasoning_tokens=12)]) is False
    # Zero output claimed without usage behind it is no evidence either.
    unproven = EmptyAttempt(model="m", finish_reason="stop", usage_present=False, zero_output=True)
    assert deterministic_empty([zero, unproven]) is False


def test_an_empty_attempt_reads_usage_the_way_the_providers_report_it():
    assert _attempt(completion_tokens="n/a").usage_present is False
    assert _attempt(prompt_tokens=True).usage_present is False
    assert _attempt(prompt_tokens=50_000.0).usage_present is False
    assert _attempt(reasoning_tokens="n/a").zero_output is True
    # An adapter that counts the cache apart (Anthropic) still counts a prompt read wholly from it.
    cached = _attempt(prompt_tokens=0, cache_read_input_tokens=50_000, prompt_tokens_include_cache=False)
    assert cached.usage_present is True and cached.zero_output is True


def test_an_expensive_empty_gets_one_resend():
    def budget(cost=None, **limits):
        usage = {} if cost is None else {"cost_usd": cost}
        return resend_budget(LLMResponse(content=None, usage=usage), RecoveryLimits(**limits))

    assert budget(0.30) == 1
    assert budget(0.25) == 1
    assert budget(0.10) == 3
    assert budget() == 3
    assert budget(0.10, empty_retry_cost_threshold_usd=0.05) == 1
    assert budget(0.30, empty_content_max_retries=0) == 0
    assert budget("0.30") == 3
    assert budget(True) == 3


def test_the_excerpt_keeps_the_first_500_characters():
    excerpt = reasoning_excerpt(LLMResponse(content=None, reasoning_content="x" * 900))
    assert len(excerpt) == REASONING_EXCERPT_MAX + 3 == 503
    assert excerpt == "x" * 500 + "..."
    assert reasoning_excerpt(LLMResponse(content=None, reasoning_content="y" * 500)) == "y" * 500
    assert reasoning_excerpt(LLMResponse(content=None)) == ""
    assert reasoning_excerpt(LLMResponse(content="the answer")) == ""


def test_the_excerpt_reads_the_reasoning_field_then_thinking_blocks_then_inline_tags():
    blocks = [
        {"type": "thinking", "thinking": "from ", "signature": "s"},
        {"type": "redacted_thinking", "data": "zz"},
        {"type": "thinking", "thinking": "blocks"},
    ]
    both = LLMResponse(content="<think>inline</think>", reasoning_content="field", thinking_blocks=blocks)
    assert reasoning_excerpt(both) == "field"
    assert reasoning_excerpt(LLMResponse(content="<think>inline</think>", thinking_blocks=blocks)) == "from blocks"
    inline = LLMResponse(content="<think> inline </think>answer<mm:thinking>more</mm:thinking>")
    assert reasoning_excerpt(inline) == "inline\n\nmore"
    assert reasoning_excerpt(LLMResponse(content="</think>", reasoning_content="  ")) == ""
    assert reasoning_excerpt(LLMResponse(content=None, reasoning_content="\n  hmm \n")) == "hmm"
    assert reasoning_excerpt(LLMResponse(content=None, reasoning_content="  ", thinking_blocks=blocks)) == "from blocks"


def test_the_wording_is_copied_verbatim():
    assert MAX_CONTINUATIONS == 3
    assert REASONING_EXCERPT_MAX == 500
    assert (
        POST_TOOL_NUDGE
        == "You just executed tool calls but returned an empty response. Please process the tool results above and continue with the task."
    )
    assert (
        OUTPUT_LIMIT_CONTINUATION
        == "[System: Your previous response was truncated by the output length limit. Continue exactly where you left off. Do not restart or repeat prior text. Finish the answer directly.]"
    )
    assert (
        NETWORK_CUT_CONTINUATION
        == "[System: The previous response was cut off by a network error mid-stream. Continue exactly where you left off. Do not restart or repeat prior text. Finish the answer directly.]"
    )
    assert (
        EMPTY_REPLY_FAILURE
        == "No reply: the model returned empty content after retries and any fallback models. Try `continue`, switch the model, or check the tool output above."
    )
    assert (
        THINKING_ONLY_FAILURE
        == "The model produced only internal reasoning and no final answer, despite retries. Its last reasoning, which may contain the answer:"
    )
    assert (
        TRUNCATED_FAILURE
        == "Processing stopped: the response remained truncated after {n} continuation attempts. Try again."
    )
    assert (
        TRUNCATED_FAILURE.format(n=MAX_CONTINUATIONS)
        == "Processing stopped: the response remained truncated after 3 continuation attempts. Try again."
    )
    assert REFUSAL_FAILURE == "The model declined to respond to this request (a safety refusal, not a Raven failure)."
    assert REFUSAL_EXPLANATION == " The model's explanation: {text}"
    assert REFUSAL_EXPLANATION.format(text="I can't.") == " The model's explanation: I can't."
    assert REFUSAL_NO_EXPLANATION == " The model returned no explanation."
    assert (
        PARTIAL_STREAM_NOTE
        == "No reply: streaming stopped early and only a partial response was recovered. Send `continue` to resume."
    )


# --------------------------------------------------------------------------- #
# loop: each reply kind, through the real adapter                              #
# --------------------------------------------------------------------------- #

_MODEL = "stub"


def _reply(
    content=None,
    *,
    finish="stop",
    reasoning=None,
    prompt=50_000,
    completion=1,
    reasoning_tokens=0,
    cost=None,
    tool_calls=None,
    refusal=None,
):
    """A ``ModelResponse`` as the library builds one.

    The prompt is billed at a size the request cannot be mistaken for a failed
    call by: ``transport_failure`` reads an empty reply whose prompt was billed
    as a handful of tokens as an upstream failure, and the loop would then never
    see the reply this module is about.
    """
    from litellm.types.utils import Choices, CompletionTokensDetailsWrapper, Message, ModelResponse, Usage

    details = CompletionTokensDetailsWrapper(reasoning_tokens=reasoning_tokens) if reasoning_tokens else None
    usage = Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        completion_tokens_details=details,
    )
    if cost is not None:
        usage.cost = cost
    message = Message(content=content, role="assistant", reasoning_content=reasoning, tool_calls=tool_calls)
    if refusal is not None:
        message.refusal = refusal
    return ModelResponse(choices=[Choices(finish_reason=finish, index=0, message=message)], usage=usage)


def _tool_reply(name="list_dir", arguments='{"path": "."}'):
    from litellm.types.utils import ChatCompletionMessageToolCall, Function

    call = ChatCompletionMessageToolCall(id="c1", type="function", function=Function(name=name, arguments=arguments))
    return _reply(finish="tool_calls", tool_calls=[call])


def _served(monkeypatch, replies) -> list[dict]:
    """Answer the adapter's calls from ``replies`` in order.

    Returns the list that gains each call's keyword arguments as it is made, so
    a test reads off what every request carried: the model, the effort, the
    ceiling and the messages. A call past the script fails on the spot.
    """
    queue = list(replies)
    calls: list[dict] = []

    async def acompletion(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        assert queue, f"call {len(calls)} had no scripted reply"
        return queue.pop(0)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", acompletion)
    return calls


def _litellm_agent(
    workspace: Path,
    *,
    limits: RecoveryLimits | None = None,
    max_tokens: int | None = None,
    max_iterations: int = 10,
    hooks=(),
) -> AgentLoop:
    provider = LiteLLMProvider(api_key=None, default_model=_MODEL)
    if max_tokens is not None:
        provider.generation = dataclasses.replace(provider.generation, max_tokens=max_tokens)
    return _make_agent(workspace, provider, limits, max_iterations=max_iterations, hooks=hooks)


async def _fail(agent: AgentLoop, text: str = "hi", origin: Origin = Origin.USER) -> AnswerlessTurnError:
    """The failure a turn ends in, for a test that reads more than its text."""
    with pytest.raises(AnswerlessTurnError) as failed:
        await agent._process_message(_turn(text, origin), session_key="s1")
    return failed.value


def _efforts(calls: list[dict]) -> list[str | None]:
    return [call.get("reasoning_effort") for call in calls]


def _recorded_sleeps(monkeypatch, timeline: list | None = None) -> list[float]:
    """Make the loop's waits instant and recorded: only ``turn_path``'s ``asyncio.sleep`` is replaced.

    A ``timeline``, when given, also gets ``("sleep", seconds)`` in the order the waits happen
    among whatever else it collects.
    """
    slept: list[float] = []

    class _Asyncio:
        """``asyncio`` as the loop module sees it, with only the sleep recorded."""

        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay, *_args, **_kwargs):
            slept.append(delay)
            if timeline is not None:
                timeline.append(("sleep", delay))

    monkeypatch.setattr(turn_path, "asyncio", _Asyncio())
    return slept


_ORIGINS = pytest.mark.parametrize("origin", [Origin.USER, Origin.CRON])


@pytest.mark.asyncio
@_ORIGINS
async def test_a_plain_empty_reply_is_resent_unchanged_then_answers(workspace, monkeypatch, origin):
    calls = _served(monkeypatch, [_reply(""), _reply(""), _reply("the answer")])
    agent = _litellm_agent(workspace)

    out = await agent._process_message(_turn(origin=origin), session_key="s1")

    assert out is not None and out[0] == "the answer"
    assert len(calls) == 3
    # The whole request, not the parts a test thought to compare: messages, model, effort, ceiling.
    assert calls[1] == calls[0] and calls[2] == calls[0]


@pytest.mark.asyncio
@_ORIGINS
async def test_a_plain_empty_reply_fails_after_three_resends_with_hermes_wording(workspace, monkeypatch, origin):
    calls = _served(monkeypatch, [_reply("")] * 4)
    agent = _litellm_agent(workspace)

    failed = await _fail(agent, origin=origin)

    assert len(calls) == 4, "the call and the three resends the budget allows"
    assert str(failed) == EMPTY_REPLY_FAILURE
    assert failed.no_output is True
    assert all(call == calls[0] for call in calls)


@pytest.mark.asyncio
async def test_two_zero_output_empties_stop_the_resends(workspace, monkeypatch):
    """Hermes's guard: two replies that each prove nothing was generated say a third would be the same."""
    calls = _served(monkeypatch, [_reply("", completion=0)] * 4)

    failed = await _fail(_litellm_agent(workspace))

    assert len(calls) == 2
    assert str(failed) == EMPTY_REPLY_FAILURE


@pytest.mark.asyncio
async def test_one_zero_output_empty_does_not_stop_the_resends(workspace, monkeypatch):
    calls = _served(monkeypatch, [_reply("", completion=0), _reply("", completion=1), _reply("", completion=0)] * 2)

    await _fail(_litellm_agent(workspace))

    assert len(calls) == 4


@pytest.mark.asyncio
async def test_an_expensive_empty_is_resent_once(workspace, monkeypatch):
    calls = _served(monkeypatch, [_reply("", cost=0.30)] * 4)

    failed = await _fail(_litellm_agent(workspace))

    assert len(calls) == 2
    assert str(failed) == EMPTY_REPLY_FAILURE


@pytest.mark.asyncio
async def test_resends_back_off_between_attempts(workspace, monkeypatch):
    calls = _served(monkeypatch, [_reply("")] * 4)
    monkeypatch.setattr(turn_path, "empty_retry_delay", lambda n, **_: float(n))
    slept = _recorded_sleeps(monkeypatch)

    await _fail(_litellm_agent(workspace))

    assert len(calls) == 4
    assert slept == [1.0, 2.0, 3.0]


@pytest.mark.asyncio
async def test_an_empty_reply_after_tools_is_nudged_once_with_hermes_words(workspace, monkeypatch):
    calls = _served(monkeypatch, [_tool_reply(), _reply(""), _reply("done")])
    agent = _litellm_agent(workspace)

    out = await agent._process_message(_turn(), session_key="s1")

    assert out is not None and out[0] == "done"
    assert len(calls) == 3
    placeholder, nudge = calls[2]["messages"][-2:]
    assert (placeholder["role"], placeholder["content"]) == ("assistant", "(empty)")
    assert (nudge["role"], nudge["content"]) == ("user", POST_TOOL_NUDGE)
    assert nudge["content"].startswith("You just executed tool calls but returned an empty response.")
    # Scaffolding: neither of the two reaches the stored session.
    stored = agent.sessions.get_or_create("s1").messages
    assert not any(m.get("content") in (POST_TOOL_NUDGE, "(empty)") or m.get("_recovery_synthetic") for m in stored)


@pytest.mark.asyncio
async def test_an_empty_reply_after_the_nudge_takes_the_resend_path(workspace, monkeypatch):
    calls = _served(monkeypatch, [_tool_reply(), _reply(""), _reply(""), _reply("done")])

    out = await _litellm_agent(workspace)._process_message(_turn(), session_key="s1")

    assert out is not None and out[0] == "done"
    assert len(calls) == 4
    assert calls[3] == calls[2], "the nudge is spent: the next empty reply is resent as it stood"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turn_effort,expected",
    [
        (None, [None, None, None, "high", "low"]),
        ("max", ["max", "max", "max", "high", "low"]),
        ("medium", ["medium", "medium", "medium", "low"]),
    ],
)
async def test_a_thinking_only_reply_is_prefilled_twice_then_stepped_down(
    workspace, monkeypatch, turn_effort, expected
):
    thought = "let me think about the whole design at length. " * 25
    calls = _served(monkeypatch, [_reply("", reasoning=thought)] * 5)
    agent = _litellm_agent(workspace)
    if turn_effort:
        agent.set_session_policy("s1", reasoning_effort=turn_effort)

    failed = await _fail(agent)

    assert _efforts(calls) == expected
    assert len(calls) == len(expected)
    assert failed.no_output is False
    assert len(str(failed)) <= 200
    assert str(failed).startswith(THINKING_ONLY_FAILURE.partition(" Its last")[0])
    assert failed.full_detail is not None
    assert failed.full_detail.startswith(THINKING_ONLY_FAILURE)
    assert failed.full_detail.endswith(thought.strip()[:500] + "...")


@pytest.mark.asyncio
async def test_a_step_down_request_does_not_end_on_the_prefill(workspace, monkeypatch):
    calls = _served(monkeypatch, [_reply("", reasoning="hmm, let me think")] * 5)

    await _fail(_litellm_agent(workspace))

    tails = [call["messages"][-1]["role"] for call in calls]
    assert tails == ["user", "assistant", "assistant", "user", "user"]
    assert calls[3]["messages"] == calls[0]["messages"], "the step-down asks the original question again"


@pytest.mark.asyncio
async def test_a_thinking_only_failure_without_reasoning_text_ends_on_its_first_sentence(workspace, monkeypatch):
    """Usage that counts reasoning tokens and a reply that carries no text of it: nothing to quote."""
    _served(monkeypatch, [_reply("", reasoning_tokens=800)] * 5)

    failed = await _fail(_litellm_agent(workspace))

    assert str(failed) == "The model produced only internal reasoning and no final answer, despite retries."
    assert failed.full_detail is None


@pytest.mark.asyncio
async def test_a_step_down_answer_is_not_cut_like_a_continuation_of_the_thought_it_dropped(workspace, monkeypatch):
    """A prefill after reasoning cut at the ceiling makes the answer that follows it open on the rest
    of that thought, so its head is cut. The step-down drops the prefills, and an answer to the
    original question has no such head."""
    cut_thought = _reply("", reasoning="deciding how to lay out the file", finish="length")
    calls = _served(monkeypatch, [cut_thought] * 3 + [_reply(" lowercase start. Then the rest.")])

    out = await _litellm_agent(workspace)._process_message(_turn(), session_key="s1")

    assert out is not None and out[0] == "lowercase start. Then the rest."
    assert _efforts(calls) == [None, None, None, "high"]


@pytest.mark.asyncio
async def test_the_rungs_a_turn_has_used_are_kept_in_the_set_it_is_given(workspace, monkeypatch):
    """The set is the turn's record of what thinking-only replies have been stepped down to: filled
    by a run, honoured by the next run it is handed to, and never shared by runs that were given none."""
    thinking = _reply("", reasoning="hmm, let me think")
    calls = _served(monkeypatch, [thinking] * 5 + [thinking] * 3 + [thinking] * 5)
    agent = _litellm_agent(workspace)
    used: set[str] = set()

    await agent._run_agent_loop(_seed(), rungs_used=used)
    assert used == {"high", "low"} and _efforts(calls) == [None, None, None, "high", "low"]

    # The same turn run again: both rungs are spent, so after its prefills there is nothing to step down to.
    await agent._run_agent_loop(_seed(), rungs_used=used)
    assert _efforts(calls[5:]) == [None, None, None]

    # A run given no set keeps its own, so nothing leaks from the one before.
    await agent._run_agent_loop(_seed())
    assert _efforts(calls[8:]) == [None, None, None, "high", "low"]


@pytest.mark.asyncio
async def test_a_step_down_that_comes_back_plain_empty_is_resent_at_that_effort(workspace, monkeypatch):
    """An unchanged resend repeats the request the empty call carried, effort included."""
    thinking = _reply("", reasoning="hmm, let me think")
    calls = _served(monkeypatch, [thinking, thinking, thinking, _reply(""), _reply("the answer")])

    out = await _litellm_agent(workspace)._process_message(_turn(), session_key="s1")

    assert out is not None and out[0] == "the answer"
    assert _efforts(calls) == [None, None, None, "high", "high"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script,turn_effort",
    [
        pytest.param(lambda: [_reply("")] * 4, None, id="empty_spent"),
        pytest.param(lambda: [_reply("", reasoning="hmm, let me think")] * 5, None, id="thinking_only_unstated"),
        pytest.param(lambda: [_reply("", reasoning="hmm, let me think")] * 3, "low", id="thinking_only_low"),
        pytest.param(lambda: [_reply("", reasoning="hmm, let me think")] * 5, "max", id="thinking_only_max"),
        pytest.param(lambda: [_reply("", finish="length")] * 4, None, id="cut_spent"),
    ],
)
async def test_no_request_asks_minimal(workspace, monkeypatch, script, turn_effort):
    """Codex refuses ``minimal`` with an HTTP 400, so no move recovery makes may ask for it.

    Each script runs a turn to the end of what recovery has to try, and every request it made is read.
    A session that pins ``minimal`` itself still sends it -- that is the user's choice, and none of
    these does.
    """
    replies = script()
    calls = _served(monkeypatch, replies)
    agent = _litellm_agent(workspace)
    if turn_effort:
        agent.set_session_policy("s1", reasoning_effort=turn_effort)

    await _fail(agent)

    assert len(calls) == len(replies), "the turn went through every move recovery had"
    assert "minimal" not in _efforts(calls)


@pytest.mark.asyncio
async def test_no_request_asks_minimal_when_a_head_is_summarized(monkeypatch):
    """The other request whose effort the runtime picks rather than the session: the head summary."""
    calls = _served(monkeypatch, [_reply("Compacted handoff brief. " * 60)])
    head = [{"role": "system", "content": "sys prompt"}, {"role": "user", "content": "the task statement"}]
    for i in range(6):
        call = {"id": f"s{i}", "type": "function", "function": {"name": "no_such_tool", "arguments": "{}"}}
        head.append({"role": "assistant", "content": "", "tool_calls": [call]})
        head.append({"role": "tool", "tool_call_id": f"s{i}", "content": f"seed evidence {i} " * 40})

    _messages, verdict = await shrink.summarize_head(
        head,
        provider=LiteLLMProvider(api_key=None, default_model=_MODEL),
        model=_MODEL,
        window=16_000,
        ceiling=4096,
        cfg=CompactionConfig(enabled=True, reserved_tokens=15_100, preserve_recent_tokens=300),
    )

    assert verdict == "changed" and len(calls) == 1, "a summary was asked for"
    assert "minimal" not in _efforts(calls)


@pytest.mark.asyncio
async def test_a_cut_reply_with_nothing_in_it_is_continued_with_more_room(workspace, monkeypatch):
    monkeypatch.setattr(rates, "resolve_max_output_tokens", lambda *_a, **_k: 32_768)
    calls = _served(monkeypatch, [_reply("", finish="length")] * 4)
    agent = _litellm_agent(workspace, max_tokens=4096)
    agent.set_session_policy("s1", reasoning_effort="medium")

    failed = await _fail(agent)

    gen, wire = agent.provider.generation, agent.provider.wire_model_id(_MODEL)
    assert [call["max_tokens"] for call in calls] == [4096] + [
        send_max_tokens(gen, wire, pinned=4096 * 2**k) for k in (1, 2, 3)
    ]
    assert [call["max_tokens"] for call in calls] == [4096, 8192, 16384, 32768]
    assert [call["messages"][-1]["content"] for call in calls[1:]] == [OUTPUT_LIMIT_CONTINUATION] * 3
    assert _efforts(calls) == ["medium"] * 4, "there was no reasoning to blame, so the effort is left alone"
    assert str(failed) == TRUNCATED_FAILURE.format(n=3)
    assert failed.no_output is False


@pytest.mark.asyncio
async def test_a_cut_reply_that_thought_is_stepped_down_not_continued(workspace, monkeypatch):
    """Reasoning behind a cut is the reason for it, so the thinking is what shrinks."""
    calls = _served(monkeypatch, [_reply("", reasoning="thinking, thinking", finish="length")] * 5)

    failed = await _fail(_litellm_agent(workspace))

    assert not any(OUTPUT_LIMIT_CONTINUATION in str(call["messages"]) for call in calls)
    assert _efforts(calls) == [None, None, None, "high", "low"]
    assert str(failed).startswith("The model produced only internal reasoning")


@pytest.mark.asyncio
async def test_a_continuation_never_follows_a_tool_message(workspace, monkeypatch):
    """A bare tool->user pair is a 400 on most APIs, which is why a synthetic assistant sits between them.

    Reached after a tool call with the nudge switched off, so the cut reply is the first thing
    after the tool result and nothing has been appended since.
    """
    calls = _served(monkeypatch, [_tool_reply()] + [_reply("", finish="length")] * 4)
    agent = _litellm_agent(workspace, limits=RecoveryLimits(post_tool_empty_max_nudges=0))

    await _fail(agent, "use the tool")

    checked = 0
    for call in calls:
        roles = [m["role"] for m in call["messages"]]
        for i, message in enumerate(call["messages"]):
            if message.get("content") == OUTPUT_LIMIT_CONTINUATION:
                assert roles[i - 1] == "assistant", roles[max(0, i - 3) : i + 1]
                checked += 1
    assert checked


@pytest.mark.asyncio
async def test_with_recovery_off_an_empty_reply_fails_at_once(workspace, monkeypatch):
    calls = _served(monkeypatch, [_reply(""), _reply("never asked")])

    failed = await _fail(_litellm_agent(workspace, limits=RecoveryLimits(enabled=False)))

    assert len(calls) == 1
    assert str(failed) == turn_path._NO_CONTENT_UNRECOVERED
    assert failed.no_output is True


@pytest.mark.asyncio
async def test_an_empty_reply_on_a_model_reached_by_a_refusal_is_not_resent(workspace, monkeypatch):
    """A fallback model gets one try, however it was reached: the resends are the first model's."""
    calls = _served(monkeypatch, [_reply("no", finish="content_filter")] + [_reply("")] * 3)
    agent = _litellm_agent(workspace)

    _final, _used, _messages, outcome = await agent._run_agent_loop(_seed(), fallback_models=["fb"])

    assert _models(calls) == [_MODEL, "fb"]
    assert outcome.error == EMPTY_REPLY_FAILURE


@pytest.mark.asyncio
async def test_a_switch_to_a_fallback_is_not_billed_an_iteration(workspace, monkeypatch):
    calls = _served(monkeypatch, [_reply("")] * 4 + [_reply("fb's answer")])
    agent = _litellm_agent(workspace, max_iterations=1)

    final, _used, _messages, outcome = await agent._run_agent_loop(_seed(), fallback_models=["fb"])

    assert final == "fb's answer"
    assert outcome.status == "completed"
    assert _models(calls) == [_MODEL] * 4 + ["fb"]


@pytest.mark.asyncio
async def test_a_fallback_after_prefills_is_not_asked_on_an_assistant_row(workspace, monkeypatch):
    """Two prefills, then replies with nothing in them until the resends are spent. The model the
    turn switches to is asked the original question, not the thought the first one left."""
    thinking = _reply("", reasoning="hmm, let me think")
    calls = _served(monkeypatch, [thinking, thinking] + [_reply("")] * 4 + [_reply("fb's answer")])
    agent = _litellm_agent(workspace)

    final, _used, _messages, _outcome = await agent._run_agent_loop(_seed(), fallback_models=["fb"])

    assert final == "fb's answer"
    assert _models(calls) == [_MODEL] * 6 + ["fb"]
    assert calls[5]["messages"][-1]["role"] == "assistant", "the resends before it did end on the prefill"
    assert calls[6]["messages"] == calls[0]["messages"]


@pytest.mark.asyncio
async def test_the_nudge_is_spent_once_a_turn(workspace, monkeypatch):
    """A second step that ends empty after tools is past the one nudge: it is resent as it stood."""
    calls = _served(monkeypatch, [_tool_reply(), _reply(""), _tool_reply(), _reply(""), _reply("done")])

    final, _used, _messages, _outcome = await _litellm_agent(workspace)._run_agent_loop(_seed())

    assert final == "done"
    assert len(calls) == 5
    assert calls[4] == calls[3]


@pytest.mark.asyncio
async def test_a_reply_that_thought_ends_the_run_of_zero_output_empties(workspace, monkeypatch):
    """Zero output twice running is the guard; a reply with thinking in it between the two is not zero output."""
    calls = _served(
        monkeypatch,
        [
            _reply("", completion=0),
            _reply("", reasoning="hmm, let me think"),
            _reply("", completion=0),
            _reply("the answer"),
        ],
    )

    final, _used, _messages, _outcome = await _litellm_agent(workspace)._run_agent_loop(_seed())

    assert final == "the answer"
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_with_recovery_off_the_loop_says_the_model_gave_nothing_back(workspace, monkeypatch):
    _served(monkeypatch, [_reply("")])

    final, _used, _messages, outcome = await _litellm_agent(
        workspace, limits=RecoveryLimits(enabled=False)
    )._run_agent_loop(_seed())

    assert final is None
    assert outcome.no_output is True and outcome.error is None


@pytest.mark.asyncio
async def test_a_rung_the_wire_cannot_tell_apart_is_not_asked_for(workspace):
    """On a wire where an unstated effort is already ``high``, asking for ``high`` is the same request again."""

    class _Collapsed(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")
            self.efforts: list[str | None] = []

        def reasoning_wire_keys(self, model, effort):
            return {"effort": "high" if effort in (None, "high") else effort}

        async def chat(self, messages, tools=None, model=None, reasoning_effort=None, **_kw):
            self.efforts.append(reasoning_effort)
            return LLMResponse(content="", reasoning_content="hmm", finish_reason="stop")

        def get_default_model(self) -> str:
            return "stub"

    provider = _Collapsed()

    await _make_agent(workspace, provider)._run_agent_loop(_seed())

    assert provider.efforts == [None, None, None, "low"]


@pytest.mark.asyncio
async def test_the_wrap_up_after_a_switch_goes_to_the_model_the_turn_is_on(workspace, monkeypatch):
    calls = _served(monkeypatch, [_reply("")] * 4 + [_tool_reply(), _tool_reply(), _reply("where it got to")])
    agent = _litellm_agent(workspace, max_iterations=2)

    final, _used, _messages, outcome = await agent._run_agent_loop(_seed(), fallback_models=["fb"])

    assert outcome.status == "interrupted" and final == "where it got to"
    assert _models(calls) == [_MODEL] * 4 + ["fb"] * 3


def _wire_message_tool(agent: AgentLoop) -> list[str]:
    """Point the loop's message tool at a recorder, on this turn's own target, so a send reaches it and counts as the reply."""
    sent: list[str] = []

    async def send(content: str, media: list[str]) -> None:
        sent.append(content)

    message = agent.tools.get("message")
    message.set_context("test", "c1")
    message.set_send_callback(send)
    return sent


_SEND = '{"content": "here is your answer"}'


@pytest.mark.asyncio
async def test_an_empty_reply_after_the_message_tool_sent_ends_the_turn_answered(workspace, monkeypatch):
    """The reader has the answer. The nudge is instant and still asked; the backed-off resends are not."""
    calls = _served(monkeypatch, [_tool_reply("message", _SEND), _reply(""), _reply(""), _reply("never asked")])
    slept = _recorded_sleeps(monkeypatch)
    agent = _litellm_agent(workspace)
    sent = _wire_message_tool(agent)

    out = await agent._process_message(_turn(), session_key="s1")

    assert sent == ["here is your answer"]
    assert out is None, "the message tool answered, so the turn has no reply of its own to send"
    assert len(calls) == 3, "the send, the empty reply, and the reply the nudge asked for"
    assert calls[2]["messages"][-1]["content"] == POST_TOOL_NUDGE
    assert slept == []


@pytest.mark.asyncio
async def test_an_answered_turn_that_ends_on_the_last_iteration_is_not_wrapped_up(workspace, monkeypatch):
    """The wrap-up is for a turn that ran out of budget. This one ended because the reader already has
    its answer, and a summary asked for now would be a second reply and a call nobody needed."""
    calls = _served(
        monkeypatch, [_tool_reply("message", _SEND), _reply(""), _reply(""), _reply("a wrap-up nobody asked for")]
    )
    agent = _litellm_agent(workspace, max_iterations=3)
    _wire_message_tool(agent)

    final, _used, _messages, outcome = await agent._run_agent_loop(_seed())

    assert len(calls) == 3, "the send, the empty reply, and the nudged one: the third iteration is the cap"
    assert final is None and outcome.status == "completed"


@pytest.mark.asyncio
async def test_the_same_script_without_the_send_takes_the_resend_ladder(workspace, monkeypatch):
    """The control: the message tool was never wired, so it sent nothing, and the empty replies are the turn's to recover."""
    calls = _served(monkeypatch, [_tool_reply("message", _SEND)] + [_reply("")] * 5 + [_reply("never asked")])
    slept = _recorded_sleeps(monkeypatch)

    failed = await _fail(_litellm_agent(workspace))

    assert str(failed) == EMPTY_REPLY_FAILURE
    assert len(calls) == 6, "the call that sent nothing, the nudge, and three resends"
    assert len(slept) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script,fallbacks,asked",
    [
        pytest.param(lambda: [_reply("", reasoning="hmm, let me think")] * 5, [], 4, id="step_down"),
        pytest.param(lambda: [_reply("no", finish="content_filter"), _reply("fb's answer")], ["fb"], 2, id="fallback"),
        pytest.param(lambda: [_reply("", finish="length")] * 6, [], 5, id="failure"),
    ],
)
async def test_the_paid_moves_and_the_failure_give_way_once_the_message_tool_sent(
    workspace, monkeypatch, script, fallbacks, asked
):
    """A step-down and a fallback are re-asks behind a backoff, and a failure is a verdict on a turn the
    reader already has an answer to. The instant moves before them still run: two prefills, three continuations."""
    calls = _served(monkeypatch, [_tool_reply("message", _SEND), *script()])
    slept = _recorded_sleeps(monkeypatch)
    agent = _litellm_agent(workspace)
    _wire_message_tool(agent)

    _final, _used, _messages, outcome = await agent._run_agent_loop(_seed(), fallback_models=fallbacks)

    assert outcome.status == "completed" and outcome.error is None
    assert len(calls) == asked
    assert _models(calls) == [_MODEL] * asked and _efforts(calls) == [None] * asked
    assert slept == []


@pytest.mark.asyncio
async def test_a_failure_on_the_last_iteration_is_not_wrapped_up(workspace, monkeypatch):
    """Two prefills and two step-downs take the three iterations there are. The turn that
    fails on the third has nothing to wrap up: a wrap-up would answer for a model that never did."""
    calls = _served(monkeypatch, [_reply("", reasoning="hmm, let me think")] * 6)
    agent = _litellm_agent(workspace, max_iterations=3)

    final, _used, _messages, outcome = await agent._run_agent_loop([{"role": "user", "content": "hi"}])

    assert len(calls) == 5
    assert outcome.status == "error"
    assert final == outcome.error and final.startswith("The model produced only internal reasoning")


@pytest.mark.asyncio
async def test_a_tool_call_ends_the_run_of_zero_output_empties(workspace, monkeypatch):
    """The guard reads consecutive replies. One that did something in between is a different
    run, and the empty reply after it is the first of its own."""
    calls = _served(
        monkeypatch,
        [_reply("", completion=0), _tool_reply(), _reply("", completion=0), _reply("the answer")],
    )
    agent = _litellm_agent(workspace, limits=RecoveryLimits(post_tool_empty_max_nudges=0))

    final, _used, _messages, _outcome = await agent._run_agent_loop(_seed())

    assert final == "the answer"
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_a_resend_on_the_last_iteration_is_not_wrapped_up(workspace, monkeypatch):
    """A resend is the same step asked again: it must not use up the iteration that would deliver its answer.

    The wrap-up is told apart by what it is not: a turn that ends ``interrupted``, asking
    with the tools withheld and one more instruction than the call it follows.
    """
    calls = _served(monkeypatch, [_reply(""), _reply("ok")])
    agent = _litellm_agent(workspace, max_iterations=1)

    final, _used, _messages, outcome = await agent._run_agent_loop([{"role": "user", "content": "hi"}])

    assert final == "ok"
    assert outcome.status == "completed"
    assert len(calls) == 2 and calls[1] == calls[0]


def _seed(text: str = "hi") -> list[dict]:
    return [{"role": "user", "content": text}]


def _models(calls: list[dict]) -> list[str]:
    return [call["model"] for call in calls]


class _Stream:
    """A stand-in for LiteLLM's stream wrapper over scripted chunks.

    ``received_finish_reason`` is what the wrapper kept of the finish the upstream
    sent: None for a stream that closed without one, which is how the provider
    tells a ``stop`` the library made up from a real one.
    """

    def __init__(self, chunks, received_finish_reason=None):
        self._chunks = iter(chunks)
        self.received_finish_reason = received_finish_reason

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self):
        return None


def _chunk(content=None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)], usage=None)


def _finished_stream(*texts: str) -> _Stream:
    """A stream that ends the way the upstream meant it to: the text, then its own ``stop``."""
    return _Stream([*(_chunk(text) for text in texts), _chunk(finish_reason="stop")], received_finish_reason="stop")


class _Sink:
    """The turn's delta and notice outlets, collecting what reaches them."""

    def __init__(self, timeline: list | None = None) -> None:
        self.text: list[str] = []
        self.notices: list[tuple[object, str]] = []
        self._timeline = timeline

    async def delta(self, text: str) -> None:
        self.text.append(text)

    async def notice(self, kind, detail: str) -> None:
        self.notices.append((kind, detail))
        if self._timeline is not None:
            self._timeline.append(("notice", detail))


@pytest.mark.asyncio
async def test_a_resend_says_when_it_waits(workspace, monkeypatch):
    """One notice per backoff, each ahead of its wait: the status is for the seconds it names."""
    _served(monkeypatch, [_reply(""), _reply(""), _reply("the answer")])
    timeline: list = []
    _recorded_sleeps(monkeypatch, timeline)
    sink = _Sink(timeline)

    final, _used, _messages, _outcome = await _litellm_agent(workspace)._run_agent_loop(_seed(), on_notice=sink.notice)

    assert final == "the answer"
    assert sink.notices == [(NoticeKind.LLM_RETRY, "empty_reply")] * 2
    assert [step[0] for step in timeline] == ["notice", "sleep", "notice", "sleep"]


@pytest.mark.asyncio
async def test_a_step_down_says_when_it_waits(workspace, monkeypatch):
    """The prefills before it are instant and announce nothing; the step-down is the wait."""
    _served(monkeypatch, [_reply("", reasoning="hmm, let me think")] * 3 + [_reply("the answer")])
    timeline: list = []
    _recorded_sleeps(monkeypatch, timeline)
    sink = _Sink(timeline)

    final, _used, _messages, _outcome = await _litellm_agent(workspace)._run_agent_loop(_seed(), on_notice=sink.notice)

    assert final == "the answer"
    assert sink.notices == [(NoticeKind.LLM_RETRY, "thinking_only")]
    assert [step[0] for step in timeline] == ["notice", "sleep"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script,fallbacks",
    [
        pytest.param(lambda: [_reply("the answer")], [], id="answers_at_once"),
        pytest.param(lambda: [_tool_reply(), _reply(""), _reply("the answer")], [], id="nudged"),
        pytest.param(lambda: [_reply("", reasoning="hmm, let me think"), _reply("the answer")], [], id="prefilled"),
        pytest.param(lambda: [_reply("no", finish="content_filter"), _reply("the answer")], ["fb"], id="fell_back"),
        pytest.param(lambda: [_reply("", finish="length"), _reply("the answer")], [], id="continued"),
    ],
)
async def test_a_turn_that_does_not_wait_announces_nothing(workspace, monkeypatch, script, fallbacks):
    _served(monkeypatch, script())
    sink = _Sink()

    final, _used, _messages, _outcome = await _litellm_agent(workspace)._run_agent_loop(
        _seed(), fallback_models=fallbacks, on_notice=sink.notice
    )

    assert final == "the answer"
    assert sink.notices == []


@pytest.mark.asyncio
async def test_a_wedged_notice_outlet_does_not_park_a_resend(workspace, monkeypatch):
    """The notice is a status hint, staged like the ladder's: an outlet that never returns costs the hint, not the turn."""
    _served(monkeypatch, [_reply(""), _reply("the answer")])
    agent = _litellm_agent(workspace)
    agent.provider.generation = dataclasses.replace(agent.provider.generation, first_byte_timeout=0.05)

    async def wedged(kind, detail: str) -> None:
        await asyncio.sleep(30)

    final, _used, _messages, _outcome = await asyncio.wait_for(
        agent._run_agent_loop(_seed(), on_notice=wedged), timeout=10
    )

    assert final == "the answer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(lambda: _reply("I can't help with that", finish="content_filter"), id="content_filter"),
        pytest.param(lambda: _reply(None, refusal="I can't help with that"), id="refusal_field"),
    ],
)
async def test_a_refusal_is_not_retried_and_fails_with_the_explanation(workspace, monkeypatch, reply):
    calls = _served(monkeypatch, [reply(), _reply("never asked")])

    failed = await _fail(_litellm_agent(workspace))

    assert len(calls) == 1
    assert str(failed) == REFUSAL_FAILURE + " The model's explanation: I can't help with that"
    assert failed.no_output is False


@pytest.mark.asyncio
async def test_a_refusal_that_explains_nothing_says_so(workspace, monkeypatch):
    _served(monkeypatch, [_reply("", finish="content_filter")])

    failed = await _fail(_litellm_agent(workspace))

    assert str(failed) == REFUSAL_FAILURE + REFUSAL_NO_EXPLANATION


@pytest.mark.asyncio
async def test_a_refusal_switches_to_one_fallback_model(workspace, monkeypatch):
    calls = _served(monkeypatch, [_reply("I can't help with that", finish="content_filter"), _reply("fb's answer")])
    agent = _litellm_agent(workspace)

    final, _used, _messages, outcome = await agent._run_agent_loop(_seed(), fallback_models=["fb"])

    assert final == "fb's answer"
    assert outcome.status == "completed"
    assert _models(calls) == [_MODEL, "fb"]


@pytest.mark.asyncio
async def test_a_refusal_falls_back_only_once(workspace, monkeypatch):
    """The second refusal ends the turn with a model still unused: a refusal is the upstream's
    verdict on the request, and asking every model in turn is shopping for a yes."""
    calls = _served(
        monkeypatch,
        [_reply("no", finish="content_filter"), _reply("still no", finish="content_filter"), _reply("fb2's answer")],
    )
    agent = _litellm_agent(workspace)

    _final, _used, _messages, outcome = await agent._run_agent_loop(_seed(), fallback_models=["fb1", "fb2"])

    assert _models(calls) == [_MODEL, "fb1"]
    assert outcome.status == "error"
    assert outcome.error == REFUSAL_FAILURE + " The model's explanation: still no"


@pytest.mark.asyncio
async def test_a_refusal_after_another_fallback_still_gets_its_one_switch(workspace, monkeypatch):
    """The refusal's one switch is its own: a model reached because an empty reply spent the
    resends has not used it."""
    calls = _served(
        monkeypatch,
        [_reply("")] * 4 + [_reply("no", finish="content_filter"), _reply("fb2's answer")],
    )
    agent = _litellm_agent(workspace)

    final, _used, _messages, _outcome = await agent._run_agent_loop(_seed(), fallback_models=["fb1", "fb2"])

    assert final == "fb2's answer"
    assert _models(calls) == [_MODEL] * 4 + ["fb1", "fb2"]


@pytest.mark.asyncio
async def test_an_empty_reply_walks_each_fallback_model_once(workspace, monkeypatch):
    calls = _served(monkeypatch, [_reply("")] * 5 + [_reply("fb2's answer")])
    agent = _litellm_agent(workspace)

    final, _used, _messages, outcome = await agent._run_agent_loop(_seed(), fallback_models=["fb1", "fb2"])

    assert final == "fb2's answer"
    assert outcome.status == "completed"
    assert _models(calls) == [_MODEL] * 4 + ["fb1", "fb2"]


@pytest.mark.asyncio
async def test_an_empty_fallback_model_ends_the_turn_when_none_is_left(workspace, monkeypatch):
    calls = _served(monkeypatch, [_reply("")] * 5)
    agent = _litellm_agent(workspace)

    _final, _used, _messages, outcome = await agent._run_agent_loop(_seed(), fallback_models=["fb"])

    assert _models(calls) == [_MODEL] * 4 + ["fb"]
    assert outcome.error == EMPTY_REPLY_FAILURE
    assert outcome.no_output is True


@pytest.mark.asyncio
async def test_a_thinking_only_reply_falls_back_after_the_step_downs(workspace, monkeypatch):
    thinking = _reply("", reasoning="hmm, let me think")
    calls = _served(monkeypatch, [thinking] * 5 + [_reply("fb's answer")])
    agent = _litellm_agent(workspace)

    final, _used, _messages, _outcome = await agent._run_agent_loop(_seed(), fallback_models=["fb"])

    assert final == "fb's answer"
    assert _models(calls) == [_MODEL] * 5 + ["fb"]
    # The fallback is asked as the turn asked its first model: no prefill left on it, no stepped-down effort.
    assert _efforts(calls) == [None, None, None, "high", "low", None]
    assert calls[5]["messages"] == calls[0]["messages"]


@pytest.mark.asyncio
async def test_the_fallback_chain_handed_to_the_provider_shrinks_as_models_are_spent():
    """Handed the whole list again after a switch, the provider would try the model the loop just left."""
    seen: list[tuple[str, list[str]]] = []

    class _Provider:
        async def chat_with_retry(self, messages, tools=None, model=None, fallback_models=None, **_kw):
            seen.append((model, list(fallback_models or [])))
            return LLMResponse(content="" if len(seen) < 6 else "done", finish_reason="stop")

        def get_default_model(self):
            return "primary"

    with tempfile.TemporaryDirectory() as td:
        agent = AgentLoop(
            provider=_Provider(),
            workspace=Path(td),
            model="primary",
            policy=TurnPolicy(max_iterations=10),
            tools=ToolWiring(restrict_to_workspace=True),
        )
        await agent._run_agent_loop(_seed(), fallback_models=["fb1", "fb2"])

    assert seen == [("primary", ["fb1", "fb2"])] * 4 + [("fb1", ["fb2"]), ("fb2", [])]


@pytest.mark.asyncio
async def test_the_rest_of_the_turn_stays_on_the_fallback_model(workspace, monkeypatch):
    """A switch is for the turn, not for the one call: the next step does not go back to the model that failed."""
    calls = _served(monkeypatch, [_reply("")] * 4 + [_tool_reply(), _reply("done")])
    agent = _litellm_agent(workspace)

    final, _used, _messages, _outcome = await agent._run_agent_loop(_seed(), fallback_models=["fb"])

    assert final == "done"
    assert _models(calls) == [_MODEL] * 4 + ["fb", "fb"]


@pytest.mark.asyncio
async def test_an_upstream_error_on_a_stream_tries_the_fallback_first(workspace, monkeypatch):
    """The stream path walks no chain of its own, so the loop switches: with a model left to try, the
    wait ladder is for when there is none."""
    calls = _served(monkeypatch, [_Stream([_chunk(finish_reason="stop")]), _finished_stream("fb's answer")])
    agent = _litellm_agent(workspace)
    sink = _Sink()

    final, _used, _messages, outcome = await agent._run_agent_loop(
        _seed(), fallback_models=["fb"], on_token_delta=sink.delta, on_notice=sink.notice
    )

    assert final == "fb's answer"
    assert outcome.status == "completed"
    assert _models(calls) == [_MODEL, "fb"]
    assert sink.notices == [], "no wait was announced: the fallback went first"


@pytest.mark.asyncio
async def test_a_switch_after_a_streamed_error_is_not_billed_an_iteration(workspace, monkeypatch):
    """The failed call did no work, so the fallback gets the iteration the turn had."""
    _served(monkeypatch, [_Stream([_chunk(finish_reason="stop")]), _finished_stream("fb's answer")])
    agent = _litellm_agent(workspace, max_iterations=1)

    final, _used, _messages, outcome = await agent._run_agent_loop(
        _seed(), fallback_models=["fb"], on_token_delta=_Sink().delta
    )

    assert final == "fb's answer"
    assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_an_upstream_error_on_a_stream_waits_when_no_fallback_is_left(workspace, monkeypatch):
    calls = _served(monkeypatch, [_Stream([_chunk(finish_reason="stop")]), _finished_stream("the answer")])
    agent = _litellm_agent(workspace)
    sink = _Sink()

    final, _used, _messages, _outcome = await agent._run_agent_loop(
        _seed(), on_token_delta=sink.delta, on_notice=sink.notice
    )

    assert final == "the answer"
    assert _models(calls) == [_MODEL, _MODEL]
    assert [detail for _kind, detail in sink.notices] == ["network"], "the ladder's one wait, announced"


class _PrefilledCallErrors(LLMProvider):
    """Thinks without answering, then fails the prefilled call with an error a fallback model can answer.

    ``stub`` takes a prefill and ``fb`` refuses one, as an Anthropic model with thinking on does.
    """

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.requests: list[tuple[str | None, list[dict]]] = []

    def supports_assistant_prefill(self, model: str | None = None) -> bool:
        return model != "fb"

    async def chat(self, *args, **kwargs):  # pragma: no cover - the streaming path is under test
        raise AssertionError("the streaming path is the one under test")

    async def chat_stream(self, messages, tools=None, model=None, **_kw):
        self.requests.append((model, [dict(m) for m in messages]))
        if len(self.requests) == 1:
            yield ChatDelta(content=None, reasoning_content="let me think about this at length")
            yield ChatDelta(content=None, finish_reason="stop", usage={"prompt_tokens": 100, "completion_tokens": 50})
            return
        if model == "stub":
            yield ChatDelta(
                content="Error calling LLM (server@stub): overloaded",
                finish_reason="error",
                error_classification=ErrorClassification("server", retryable=True, should_fallback=True),
            )
            return
        yield ChatDelta(content="fb's answer")
        yield ChatDelta(content=None, finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_switch_after_a_streamed_error_asks_the_fallback_the_original_question(workspace, monkeypatch):
    """The prefill is the thought of the model being left. A streamed error switches models in the loop
    as an empty reply's fallback does, and the model it moves to is asked what the turn first asked:
    a request ending on the prefill is one a prefill-refusing model rejects."""
    provider = _PrefilledCallErrors()
    _recorded_sleeps(monkeypatch)

    final, _used, _messages, outcome = await _make_agent(workspace, provider)._run_agent_loop(
        _seed(), fallback_models=["fb"], on_token_delta=_Sink().delta
    )

    assert final == "fb's answer" and outcome.status == "completed"
    asked = [(model, messages[-1]["role"]) for model, messages in provider.requests]
    assert asked == [("stub", "user"), ("stub", "assistant"), ("fb", "user")]
    assert provider.requests[2][1] == provider.requests[0][1]


@pytest.mark.asyncio
async def test_an_upstream_error_on_a_plain_call_is_left_to_the_providers_chain():
    """A non-streamed call already walks ``[model, *fallbacks]`` inside the provider, so the loop
    does not switch models for an error it has not seen the end of."""
    seen: list[tuple[str, list[str]]] = []
    answers = [
        LLMResponse(
            content="Error calling LLM (network): boom",
            finish_reason="error",
            error_classification=ErrorClassification("network", retryable=True, should_fallback=True),
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ]

    class _Provider:
        async def chat_with_retry(self, messages, tools=None, model=None, fallback_models=None, **_kw):
            seen.append((model, list(fallback_models or [])))
            return answers.pop(0)

        def get_default_model(self):
            return "primary"

    with tempfile.TemporaryDirectory() as td:
        agent = AgentLoop(
            provider=_Provider(),
            workspace=Path(td),
            model="primary",
            policy=TurnPolicy(max_iterations=10, empty_recovery=RecoveryLimits(llm_error_retry_delays=(0.0,))),
            tools=ToolWiring(restrict_to_workspace=True),
        )
        final, _used, _messages, _outcome = await agent._run_agent_loop(_seed(), fallback_models=["fb"])

    assert final == "done"
    assert seen == [("primary", ["fb"]), ("primary", ["fb"])]


def _unterminated_stream(text: str) -> _Stream:
    """A stream that closed cleanly after ``text`` without the upstream's terminal chunk.

    The wrapper makes up a ``stop`` for it and keeps no finish of its own, which is all a backend
    that never sends a terminal chunk ever leaves.
    """
    return _Stream([_chunk(text)], received_finish_reason=None)


class _DyingStream(_Stream):
    """A stream that said ``text`` and then broke, as a dropped connection does: the wrapper raises."""

    def __init__(self, text: str) -> None:
        super().__init__([_chunk(text)])

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise ConnectionError("connection reset by peer") from None


def _tool_stream(name: str = "list_dir", arguments: str = '{"path": "."}') -> _Stream:
    call = SimpleNamespace(index=0, id="c1", function=SimpleNamespace(name=name, arguments=arguments))
    delta = SimpleNamespace(content=None, tool_calls=[call], reasoning_content=None)
    calling = SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None)
    return _Stream([calling, _chunk(finish_reason="tool_calls")], received_finish_reason="tool_calls")


class _Holds(AgentHook):
    """A hook that can send a response back, which is what makes the loop hold a reply's deltas."""

    rolls_back_iterations = True

    @property
    def name(self) -> str:
        return "Holds"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first,second",
    [
        pytest.param("The answer is ", "42.", id="space_at_the_cut"),
        pytest.param("The answer is", " 42.", id="space_at_the_head_of_the_continuation"),
    ],
)
async def test_a_stream_that_died_mid_answer_is_continued_and_stitched(workspace, monkeypatch, first, second):
    """The space between two words survives the join, whichever side of the cut it fell on."""
    calls = _served(monkeypatch, [_DyingStream(first), _finished_stream(second)])
    agent = _litellm_agent(workspace)
    sink = _Sink()

    out = await agent._process_message(_turn(), session_key="s1", on_token_delta=sink.delta)

    assert out is not None and out[0] == "The answer is 42."
    assert len(calls) == 2
    partial, ask = calls[1]["messages"][-2:]
    assert (partial["role"], partial["content"]) == ("assistant", first.strip())
    assert (ask["role"], ask["content"]) == ("user", NETWORK_CUT_CONTINUATION)
    # The reader saw each word once, and the session keeps the stitched answer and none of the scaffolding.
    assert "".join(sink.text) == "The answer is 42."
    stored = agent.sessions.get_or_create("s1").messages
    assert [m["content"] for m in stored if m["role"] == "assistant"] == ["The answer is 42."]
    assert not any(m.get("_recovery_synthetic") or m.get("content") == NETWORK_CUT_CONTINUATION for m in stored)


@pytest.mark.asyncio
async def test_a_continuation_that_comes_back_empty_delivers_what_streamed(workspace, monkeypatch):
    """The continuation answers with a finish and no words: the model is saying it had nothing to
    add, so what streamed is the whole answer and nothing is made of the cut."""
    calls = _served(monkeypatch, [_DyingStream("The answer is 42."), _Stream([_chunk(finish_reason="stop")], "stop")])
    agent = _litellm_agent(workspace)
    sink = _Sink()

    final, _used, _messages, outcome = await agent._run_agent_loop(_seed(), on_token_delta=sink.delta)

    assert final == "The answer is 42."
    assert outcome.status == "completed" and outcome.error is None
    assert len(calls) == 2
    assert "".join(sink.text) == "The answer is 42."


@pytest.mark.asyncio
async def test_a_stream_that_closed_without_a_terminal_chunk_is_delivered_as_it_stands(workspace, monkeypatch):
    """A backend that never sends a terminal chunk has the library make up the ``stop`` of every
    streamed reply. A clean close is not a cut: the words are the answer, with no continuation and no note."""
    calls = _served(monkeypatch, [_unterminated_stream("The answer is 42."), _unterminated_stream(" nobody asked.")])
    agent = _litellm_agent(workspace)
    sink = _Sink()

    final, _used, messages, outcome = await agent._run_agent_loop(
        _seed(), on_token_delta=sink.delta, on_notice=sink.notice
    )

    assert len(calls) == 1
    assert final == "The answer is 42."
    assert outcome.status == "completed" and outcome.error is None
    assert "".join(sink.text) == "The answer is 42."
    assert sink.notices == []
    assert (messages[-1]["role"], messages[-1]["content"]) == ("assistant", "The answer is 42.")


@pytest.mark.asyncio
async def test_a_continuation_that_closes_without_a_terminal_chunk_is_not_cut_again(workspace, monkeypatch):
    """On the same backend a continuation that says more and closes cleanly is finished: it is not
    asked to go on a second time, so the answer is not repeated and no call is wasted."""
    calls = _served(
        monkeypatch,
        [_DyingStream("The answer is 42."), _unterminated_stream(" That is all."), _unterminated_stream(" More.")],
    )
    sink = _Sink()

    final, _used, _messages, outcome = await _litellm_agent(workspace)._run_agent_loop(
        _seed(), on_token_delta=sink.delta
    )

    assert len(calls) == 2
    assert final == "The answer is 42. That is all."
    assert outcome.status == "completed" and outcome.error is None
    assert "".join(sink.text) == final


def _unfinished_empty_stream() -> _Stream:
    """A stream that closed with nothing in it and no terminal chunk: the library's made-up ``stop`` is all there is."""
    return _Stream([_chunk(finish_reason="stop")], received_finish_reason=None)


@pytest.mark.asyncio
async def test_a_continuation_cut_before_any_new_text_delivers_what_streamed_with_the_note(workspace, monkeypatch):
    """The provider reads a stream that closed with nothing in it as the connection cutting the reply
    off before any content arrived. For the call that was to finish a cut answer that is a second cut,
    and the reader already has the answer so far: the turn ends there with the note, with no wait for
    a retry that would ask for more of it and no second model."""
    calls = _served(monkeypatch, [_DyingStream("The answer is 42."), *[_unfinished_empty_stream() for _ in range(6)]])
    slept = _recorded_sleeps(monkeypatch)
    agent = _litellm_agent(workspace)
    sink = _Sink()

    final, _used, messages, outcome = await agent._run_agent_loop(
        _seed(), fallback_models=["fb"], on_token_delta=sink.delta, on_notice=sink.notice
    )

    note = "\n\n" + PARTIAL_STREAM_NOTE
    assert _models(calls) == [_MODEL, _MODEL], "the cut call and its continuation"
    assert slept == [] and sink.notices == [], "nothing waited, so nothing was announced"
    assert final == "The answer is 42." + note
    assert outcome.status == "interrupted" and outcome.error is None and outcome.no_output is False
    assert "".join(sink.text) == final
    assert (messages[-1]["role"], messages[-1]["content"]) == ("assistant", final)


@pytest.mark.asyncio
async def test_a_turn_whose_continuation_was_cut_replies_instead_of_failing(workspace, monkeypatch):
    """The same script through the turn's own exit: a reply to deliver, no failure to report, and the
    row the session keeps is the answer with its note."""
    _served(monkeypatch, [_DyingStream("The answer is 42."), *[_unfinished_empty_stream() for _ in range(6)]])
    _recorded_sleeps(monkeypatch)
    agent = _litellm_agent(workspace)
    sink = _Sink()

    out = await agent._process_message(_turn(), session_key="s1", on_token_delta=sink.delta)

    reply = "The answer is 42.\n\n" + PARTIAL_STREAM_NOTE
    assert out == (reply, [])
    stored = agent.sessions.get_or_create("s1").messages
    assert [(m["role"], m["content"]) for m in stored] == [("user", "hi"), ("assistant", reply)]
    assert not any(m.get("turn_ended") for m in stored), "no failure marker was filed"


class _ContinuationErrors(LLMProvider):
    """Streams its words and dies; the continuation it is then asked for fails outright, with the
    kind of error a wait or another model could mend."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.models: list[str | None] = []

    async def chat(self, *args, **kwargs):  # pragma: no cover - the streaming path is under test
        raise AssertionError("the streaming path is the one under test")

    async def chat_stream(self, messages, tools=None, model=None, **_kw):
        self.models.append(model)
        if len(self.models) == 1:
            yield ChatDelta(content="The answer is 42.")
            raise ConnectionError("connection reset by peer")
        yield ChatDelta(
            content="Error calling LLM (network@stub): connection reset",
            finish_reason="error",
            error_classification=ErrorClassification("network", retryable=True, should_fallback=True),
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_continuation_that_errors_outright_delivers_what_streamed_with_the_note(workspace, monkeypatch):
    provider = _ContinuationErrors()
    agent = _make_agent(workspace, provider)
    slept = _recorded_sleeps(monkeypatch)
    sink = _Sink()

    final, _used, _messages, outcome = await agent._run_agent_loop(
        _seed(), fallback_models=["fb"], on_token_delta=sink.delta, on_notice=sink.notice
    )

    note = "\n\n" + PARTIAL_STREAM_NOTE
    assert provider.models == ["stub", "stub"], "the error was neither waited out nor answered with another model"
    assert slept == [] and sink.notices == []
    assert final == "The answer is 42." + note
    assert outcome.status == "interrupted" and outcome.error is None
    assert "".join(sink.text) == final, "the note reached the reader once, after the words"


@pytest.mark.asyncio
async def test_an_error_before_any_cut_still_waits_and_fails_as_it_did(workspace, monkeypatch):
    """The control: with no cut answer behind it, the same error is the turn's to wait out."""
    _served(monkeypatch, [_unfinished_empty_stream() for _ in range(5)])
    slept = _recorded_sleeps(monkeypatch)

    _final, _used, _messages, outcome = await _litellm_agent(workspace)._run_agent_loop(
        _seed(), on_token_delta=_Sink().delta
    )

    assert outcome.status == "error"
    assert len(slept) == 3


@pytest.mark.asyncio
async def test_three_cut_continuations_deliver_the_partial_with_the_note(workspace, monkeypatch):
    calls = _served(monkeypatch, [_DyingStream(text) for text in ("The ", "answer ", "is ", "42")])
    agent = _litellm_agent(workspace)
    sink = _Sink()

    final, _used, messages, outcome = await agent._run_agent_loop(_seed(), on_token_delta=sink.delta)

    note = "\n\n" + PARTIAL_STREAM_NOTE
    assert len(calls) == 4, "the call and the three continuations"
    assert final == "The answer is 42" + note
    assert outcome.status == "interrupted" and outcome.error is None
    # Said on the stream too: the closing text is not sent again once anything has streamed.
    assert "".join(sink.text) == final
    assert (messages[-1]["role"], messages[-1]["content"]) == ("assistant", final)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "streams,max_iterations",
    [
        pytest.param(("The answer is 4",), 1, id="cut_on_the_only_iteration"),
        pytest.param(("The answer ", "is 4"), 2, id="continuation_cut_on_the_last_iteration"),
    ],
)
async def test_a_stream_cut_on_the_last_iteration_ends_on_what_streamed_with_the_note(
    workspace, monkeypatch, streams, max_iterations
):
    """A continuation is a step, and the turn's last iteration leaves none for it. Handed to the
    exhaustion wrap-up instead, the cut answer had a summary streamed straight onto it and only the
    summary filed. It ends as a spent continuation budget does: what streamed, then the note, as an
    ``interrupted`` turn, with no wrap-up asked for."""
    calls = _served(monkeypatch, [*(_DyingStream(text) for text in streams), _finished_stream("Wrap-up: here.")])
    sink = _Sink()

    final, _used, messages, outcome = await _litellm_agent(workspace, max_iterations=max_iterations)._run_agent_loop(
        _seed(), on_token_delta=sink.delta
    )

    note = "\n\n" + PARTIAL_STREAM_NOTE
    assert len(calls) == len(streams), "no wrap-up was asked for"
    assert final == "The answer is 4" + note
    assert outcome.status == "interrupted" and outcome.error is None
    assert "".join(sink.text) == final
    assert (messages[-1]["role"], messages[-1]["content"]) == ("assistant", final)


@pytest.mark.asyncio
async def test_a_cut_answer_is_called_partial_when_recovery_is_switched_off(workspace, monkeypatch):
    """With recovery off nothing asks the model to go on, which is the exhausted case with its
    continuations spent at zero: the reader gets what streamed and the note says it is not the whole
    of it. Delivered as an ordinary turn, a cut answer would pass for a finished one."""
    calls = _served(monkeypatch, [_DyingStream("Hello "), _finished_stream("world.")])
    agent = _litellm_agent(workspace, limits=RecoveryLimits(enabled=False))
    sink = _Sink()

    final, _used, messages, outcome = await agent._run_agent_loop(_seed(), on_token_delta=sink.delta)

    note = "\n\n" + PARTIAL_STREAM_NOTE
    assert len(calls) == 1, "nothing asked the model to go on"
    assert final == "Hello" + note, "trimmed the way the exhausted path trims it, then the note"
    assert outcome.status == "interrupted" and outcome.error is None
    assert sink.text == ["Hello ", note], "the reader saw the words once and the note once"
    assert (messages[-1]["role"], messages[-1]["content"]) == ("assistant", final)


@pytest.mark.asyncio
async def test_a_whole_body_that_lacks_a_finish_is_delivered_as_it_stands(workspace):
    """Only a stream can be cut: a plain call hands back the whole body, finish or none."""

    class _NoFinish(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")
            self.calls = 0

        async def chat(self, messages, tools=None, model=None, **_kw):
            self.calls += 1
            return LLMResponse(content="the whole thing", finish_reason="unknown")

        def get_default_model(self) -> str:
            return "stub"

    provider = _NoFinish()

    final, _used, _messages, outcome = await _make_agent(workspace, provider)._run_agent_loop(_seed())

    assert final == "the whole thing" and outcome.status == "completed"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_a_cut_answer_that_goes_on_to_call_a_tool_leaves_its_partial_out_of_the_final_reply(
    workspace, monkeypatch
):
    """The partial belongs to the step that was cut; once the model acts instead of finishing it, it is not the start of the answer."""
    calls = _served(
        monkeypatch, [_DyingStream("Let me look. "), _tool_stream(), _finished_stream("There is one file.")]
    )

    final, _used, _messages, outcome = await _litellm_agent(workspace)._run_agent_loop(
        _seed(), on_token_delta=_Sink().delta
    )

    assert len(calls) == 3
    assert final == "There is one file."
    assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_a_held_reply_is_released_before_its_continuation_and_the_note_follows_it(workspace, monkeypatch):
    """With a hook that can send a reply back, a reply's deltas are held until it is kept. The
    partial is the answer so far, so it goes out before the call that continues it -- and the note
    after everything, once."""
    _served(monkeypatch, [_DyingStream(text) for text in ("The ", "answer ", "is ", "42")])
    agent = _litellm_agent(workspace, hooks=[_Holds()])
    sink = _Sink()

    final, _used, _messages, outcome = await agent._run_agent_loop(_seed(), on_token_delta=sink.delta)

    assert outcome.status == "interrupted"
    assert "".join(sink.text) == "The answer is 42" + "\n\n" + PARTIAL_STREAM_NOTE == final


class _WrapUpDiesAfterWords(LLMProvider):
    """Calls a tool while tools are offered; the wrap-up that follows streams a few words and dies.

    ``death`` is raised after the words, or None for a stream that reports its failure as the
    terminal delta, which is what the Anthropic adapter does with one.
    """

    def __init__(self, death: Exception | None) -> None:
        super().__init__(api_key="test")
        self.death = death
        self.calls = 0

    async def chat(self, *args, **kwargs):  # pragma: no cover - the streaming path is under test
        raise AssertionError("the streaming path is the one under test")

    async def chat_stream(self, messages, tools=None, **_kw):
        self.calls += 1
        if tools:
            call = {"index": 0, "id": "t1", "function": {"name": "no_such_tool", "arguments": "{}"}}
            yield ChatDelta(content=None, tool_call_delta={"tool_calls": [call]})
            yield ChatDelta(content=None, finish_reason="tool_calls")
            return
        yield ChatDelta(content="Summary so far: I looked at")
        if self.death is not None:
            raise self.death
        yield ChatDelta(
            content="Error calling LLM (network@stub): connection reset",
            finish_reason="error",
            error_classification=ErrorClassification("network", retryable=True, should_fallback=True),
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "death",
    [ConnectionError("connection reset by peer"), TimeoutError(), None],
    ids=["raised", "stalled", "error-delta"],
)
async def test_a_wrap_up_cut_after_its_words_ends_on_the_static_line(workspace, death):
    """The wrap-up is one call with no continuation behind it, so its words are the reply or
    nothing. A stream that died after showing some of them came back as the answer so far, and
    the wrap-up delivered the fragment as the turn's final word and saved it: the recovery of
    the answering call, in a place that has none. It fails as it always did, and the turn ends
    on the static line."""
    provider = _WrapUpDiesAfterWords(death)
    agent = _make_agent(workspace, provider, max_iterations=1)
    sink = _Sink()

    final, _used, messages, outcome = await agent._run_agent_loop(_seed(), on_token_delta=sink.delta)

    fallback = _MAX_ITER_STATIC_FALLBACK.format(n=1)
    assert provider.calls == 2, "the step that spent the budget and the wrap-up: nothing asked the model to go on"
    assert outcome.status == "interrupted"
    assert final == fallback
    assert (messages[-1]["role"], messages[-1]["content"]) == ("assistant", fallback)
    assert sink.text[-1] == fallback, "the static line reaches a reader whose stream already showed the cut words"


# --------------------------------------------------------------------------- #
# loop: plain empty -> retry -> recover                                        #
# --------------------------------------------------------------------------- #


class _EmptyThenAnswerProvider(LLMProvider):
    def __init__(self, empties: int = 2):
        super().__init__(api_key="test")
        self._empties = empties
        self.calls = 0

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        if self.calls <= self._empties:
            return LLMResponse(content="", finish_reason="stop")
        return LLMResponse(content="real answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_empty_then_recovers_and_does_not_persist_scaffolding(workspace):
    provider = _EmptyThenAnswerProvider(empties=2)
    agent = _make_agent(workspace, provider)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert out is not None
    assert out[0] == "real answer"  # recovered, not the canned dud reply
    assert provider.calls == 3  # 2 empty retries + the recovered call
    # synthetic scaffolding must not be persisted into session history
    session = agent.sessions.get_or_create("s1")
    for m in session.messages:
        assert not m.get("_recovery_synthetic")


# --------------------------------------------------------------------------- #
# loop: thinking-only -> prefill -> recover                                    #
# --------------------------------------------------------------------------- #


class _ThinkingThenAnswerProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0
        self.saw_reasoning_replay = False

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="", reasoning_content="let me think", finish_reason="stop")
        # the prefill re-feeds the prior reasoning as a synthetic assistant turn
        if any(m.get("_recovery_synthetic") for m in messages):
            self.saw_reasoning_replay = True
        return LLMResponse(content="final answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_thinking_only_recovers_via_prefill(workspace):
    provider = _ThinkingThenAnswerProvider()
    agent = _make_agent(workspace, provider)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert out is not None
    assert out[0] == "final answer"
    assert provider.saw_reasoning_replay is True
    session = agent.sessions.get_or_create("s1")
    for m in session.messages:
        assert not m.get("_recovery_synthetic")


# --------------------------------------------------------------------------- #
# loop: reasoning cut at the ceiling -> prefill -> the continuation's head      #
# --------------------------------------------------------------------------- #


def test_a_mid_sentence_head_is_cut_and_an_answer_opening_is_kept():
    from raven.agent.loop.recovery import cut_reasoning_head

    junk = " Lägg in an token. Let me start researching.\n\n## Answer\nfinal"
    assert cut_reasoning_head(junk) == "Let me start researching.\n\n## Answer\nfinal"
    assert cut_reasoning_head("so the answer is\n## Answer\nfinal") == "## Answer\nfinal"
    # Openings that read as content are left whole: a heading, a list, a sentence.
    for whole in ("## Answer\nfinal", "- first\n- second", "Yes. It does.", "1. step one\n2. step two"):
        assert cut_reasoning_head(whole) == whole
    # A fragment with nothing after it, or one longer than a fragment could be, stays.
    assert cut_reasoning_head(" trailing thought.") == " trailing thought."
    assert cut_reasoning_head(" " + "x" * 300 + ". rest") == " " + "x" * 300 + ". rest"
    assert cut_reasoning_head("") == "" and cut_reasoning_head(None) is None
    # CJK has no case, so a continuation opening directly on it is left whole; the
    # CJK stops end a fragment the gate did admit (leading whitespace, or a lowercase
    # Latin word running into CJK text) instead of letting it run to a Latin period.
    han_answer = "\u597d\u7684\u3002\u6211\u5f00\u59cb\u7814\u7a76\u3002"
    assert cut_reasoning_head(han_answer) == han_answer
    assert cut_reasoning_head(" " + han_answer) == han_answer[3:]
    assert cut_reasoning_head("so \u7b54\u6848\u662f\u3002\n## \u7b54\u6848") == "## \u7b54\u6848"


@pytest.mark.asyncio
async def test_the_gate_streams_what_the_stored_content_keeps():
    """The reader must see exactly what the session keeps: deltas are held until
    the head rule can be decided, then released with the fragment removed."""
    from raven.agent.loop.recovery import ContinuationGate, cut_reasoning_head

    async def run(deltas: list[str]) -> str:
        seen: list[str] = []

        async def deliver(text: str) -> None:
            seen.append(text)

        gate = ContinuationGate(deliver)
        for d in deltas:
            await gate(d)
        await gate.finish()
        return "".join(seen)

    deltas = [" Lägg in", " an token.", " Let me", " start.\n\n## Answer\nfinal"]
    assert await run(deltas) == cut_reasoning_head("".join(deltas)) == "Let me start.\n\n## Answer\nfinal"
    # Nothing is released before the rule can be decided.
    seen: list[str] = []

    async def deliver(text: str) -> None:
        seen.append(text)

    gate = ContinuationGate(deliver)
    await gate(" Lägg in")
    await gate(" an token.")
    assert seen == [], "a boundary with nothing after it is not yet decidable"
    await gate(" Let")
    assert seen == ["Let"]
    await gate(" me")
    assert seen == ["Let", " me"]
    # A continuation that never reaches a boundary is delivered whole at the end.
    assert await run([" a fragment with no end"]) == " a fragment with no end"
    assert await run(["## Answer\n", "final"]) == "## Answer\nfinal"


class _CutThinkingThenAnswerProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="", reasoning_content="let me think about the tok", finish_reason="length")
        return LLMResponse(
            content=" Lägg in an token. Let me start researching.\n\n## Answer\nfinal", finish_reason="stop"
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_continuation_after_cut_reasoning_loses_its_mid_thought_head(workspace):
    """2026-09-08: a 131072-token reasoning run hit the ceiling, the prefill fed
    it back, and the continuation's first clause reached the reader as the head
    of the answer."""
    provider = _CutThinkingThenAnswerProvider()
    agent = _make_agent(workspace, provider)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert out is not None
    assert out[0] == "Let me start researching.\n\n## Answer\nfinal"
    session = agent.sessions.get_or_create("s1")
    stored = [m for m in session.messages if m.get("role") == "assistant"]
    assert stored and stored[-1]["content"] == "Let me start researching.\n\n## Answer\nfinal"


@pytest.mark.asyncio
async def test_a_continuation_after_complete_reasoning_is_left_whole(workspace):
    """Reasoning that ended on its own (``stop``) was not cut, so the continuation
    opens where the model meant it to; the head rule is for the ceiling case."""

    class _Whole(_CutThinkingThenAnswerProvider):
        async def chat(
            self,
            messages,
            tools=None,
            model=None,
            max_tokens=4096,
            temperature=0.7,
            reasoning_effort=None,
            tool_choice=None,
        ):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content="", reasoning_content="let me think", finish_reason="stop")
            return LLMResponse(content=" lowercase start. Then the rest.", finish_reason="stop")

    agent = _make_agent(workspace, _Whole())
    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )
    assert out is not None and out[0] == "lowercase start. Then the rest."


# --------------------------------------------------------------------------- #
# loop: persistently empty is bounded then falls back                          #
# --------------------------------------------------------------------------- #


class _AlwaysEmptyProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0
        self.efforts: list[str | None] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        self.efforts.append(reasoning_effort)
        return LLMResponse(content="", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_an_exhausted_recovery_ends_the_turn_with_error_status(workspace):
    """The status the caller reads, which used to say ``completed``.

    ``LoopOutcome.status`` is the loop's own word on whether the turn produced
    anything; the neighbouring provider-error exit sets ``"error"`` and this one
    used to leave the initialised ``"completed"`` in place.
    """
    provider = _AlwaysEmptyProvider()
    agent = _make_agent(workspace, provider, limits=RecoveryLimits())

    _final, _used, messages, outcome = await agent._run_agent_loop([{"role": "user", "content": "hi"}])

    assert provider.calls == 4, "one call and the three resends the budget allows"
    assert outcome.status == "error"
    # And the words the caller fails the turn on, beside the status: without them
    # the caller reads an errored turn as one that simply had nothing to say.
    assert outcome.error == EMPTY_REPLY_FAILURE
    assert outcome.no_output is True
    # Like the provider-error exit beside it, the failure text is not persisted
    # into history: an error reply in the transcript poisons the next request.
    assert not any(m.get("role") == "assistant" for m in messages)


def test_the_anthropic_wire_tells_the_step_down_rungs_apart():
    """A step-down only pays for itself if the request it sends differs from the one that came back.

    On the Anthropic Messages wire ``minimal`` and ``low`` serialize to one request, which is why a
    step-down asks the provider what a rung sends. It lands only on ``high`` and ``low``, and those
    are two requests there, neither of them the unstated one a turn starts from.
    """
    from raven.agent.loop.turn_path import _reasoning_wire_keys
    from raven.providers.anthropic_messages_provider import AnthropicMessagesProvider

    for model in ("claude-opus-4-5", "claude-opus-5"):
        provider = AnthropicMessagesProvider(api_key="k", default_model=model)
        wire = _reasoning_wire_keys(provider, model)
        assert wire is not None
        assert len({wire(None), wire("high"), wire("low")}) == 3, f"{model}: every rung sent is its own request"
        assert next_step_down(None, wire) == "high"
        assert next_step_down("high", wire) == "low"
        assert next_step_down("low", wire) is None


def test_a_wire_that_sends_the_label_verbatim_skips_no_rung():
    """The chat and responses wires send the effort as the caller named it, so
    the labels stand on their own and nothing is skipped."""
    wire = LLMProvider.reasoning_wire_keys
    shape = lambda effort: repr(wire(None, "m", effort))  # noqa: E731 - one expression, read once

    assert shape("high") != shape("low")
    assert next_step_down(None, shape) == "high"
    assert next_step_down("high", shape) == "low"


def test_a_provider_that_cannot_answer_leaves_the_descent_on_the_labels():
    """A test double or an older adapter: no method, no shape, previous behaviour."""
    from raven.agent.loop.turn_path import _reasoning_wire_keys

    assert _reasoning_wire_keys(SimpleNamespace(), "m") is None
    assert next_step_down("max", None) == "high"

    def raises(_effort):
        raise RuntimeError("no")

    broken = _reasoning_wire_keys(SimpleNamespace(reasoning_wire_keys=lambda *a: raises(a)), "m")
    assert broken is not None
    assert next_step_down("max", broken) == "high", "an unanswerable question is not a same request"


@pytest.mark.asyncio
async def test_the_plain_empty_retry_leaves_the_effort_alone(workspace):
    """A retry that changes the effort is a guess, and the guesses were the harm.

    The ladder this replaced lowered the effort on every plain empty reply, one
    rung per retry down to ``minimal``, which Codex refuses with an HTTP 400. A
    plain empty reply carries no reasoning to blame, so nothing in it says the
    effort is at fault: the request goes back as it was.
    """
    provider = _AlwaysEmptyProvider()
    agent = _make_agent(workspace, provider, limits=RecoveryLimits())

    await _run_answerless(agent)

    assert provider.efforts == [None, None, None, None]


@pytest.mark.asyncio
async def test_recovery_disabled_fails_the_turn_without_asking_again(workspace):
    """Switching the recovery off buys fewer calls, not a manufactured answer.

    The turn still has nothing to say, and the canned "no response to give" line
    it used to return is what let a dead turn be filed as a finished one. So this
    exit fails too -- with a sentence of its own, because there are no budgets
    here to count.
    """
    provider = _AlwaysEmptyProvider()
    agent = _make_agent(workspace, provider, limits=RecoveryLimits(enabled=False))

    failure = await _run_answerless(agent)

    assert provider.calls == 1  # no retries when disabled
    assert failure == "The model returned no content, and this turn's empty-response recovery is switched off."


# ---------------------------------------------------------------------------
# Tag debris counts as empty, so recovery still runs
# ---------------------------------------------------------------------------


def test_lone_closing_think_tag_reads_as_empty() -> None:
    """A cut-off inline reasoning block leaves an opener-less closing tag.

    The paired-block substitution finds nothing to remove, so an
    eleven-character string reaches the recovery check looking like a real
    answer -- recovery is skipped and the tag is what the user sees.
    """
    for debris in ("</mm:think>", "</think>", "</thinking>", "</mm:think></mm:think>"):
        assert AgentLoop._strip_think(debris) is None, debris


def test_paired_namespaced_block_reads_as_empty() -> None:
    """A complete but empty namespaced block is debris too."""
    assert AgentLoop._strip_think("<mm:think></mm:think>") is None


def test_text_mentioning_a_tag_is_left_alone() -> None:
    """Debris detection must not eat an answer that talks about tags.

    The check asks whether anything survives removing the tags, and does not
    rewrite the text it returns.
    """
    said = "The model emits </think> at the end of its reasoning."
    assert AgentLoop._strip_think(said) == said


def test_real_content_after_a_think_block_survives() -> None:
    """The existing paired-block behaviour is unchanged."""
    assert AgentLoop._strip_think("<think>reasoning</think>the answer") == "the answer"


# ---------------------------------------------------------------------------
# Inline-thinking detection: namespaced and orphan spellings
# ---------------------------------------------------------------------------


def test_inline_thinking_detects_namespaced_and_orphan_tags() -> None:
    """The scan decides whether a turn produced reasoning at all.

    An opener-only pattern misses both shapes a truncated turn actually
    produces: a vendor-namespaced tag, and a closing tag whose opener the
    backend swallowed. Missing them classifies a reasoning-only turn as a real
    answer, which is exactly the recovery this module exists to trigger.
    """
    from raven.agent.loop.recovery import has_inline_thinking

    assert has_inline_thinking("<mm:think>weighing options</mm:think>")
    assert has_inline_thinking("weighing options</think>")
    assert has_inline_thinking("<think>weighing options</think>")


def test_inline_thinking_ignores_prose_about_thinking() -> None:
    """No tag, no detection -- the word alone must not trigger recovery."""
    from raven.agent.loop.recovery import has_inline_thinking

    assert not has_inline_thinking("I was thinking about the reasoning behind it")
    assert not has_inline_thinking("")
    assert not has_inline_thinking(None)


def test_paired_namespaced_block_is_stripped_not_shown() -> None:
    """A complete block must not reach the user because of its prefix.

    Debris detection and the orphan split both match tags by shape, so a
    literal-only pattern here was the one place a vendor spelling still got
    through -- and it got through in the worst form: a whole reasoning block
    rendered as if it were the answer.
    """
    assert AgentLoop._strip_think("<mm:think>weighing options</mm:think>the answer") == "the answer"
    assert AgentLoop._strip_think("<thinking>weighing</thinking>the answer") == "the answer"


def test_a_mismatched_pair_is_not_treated_as_a_block() -> None:
    """Deleting between two unrelated tags would take real content with it."""
    text = "<think>weighing</thinking>the answer"

    assert AgentLoop._strip_think(text) == text


def test_one_end_namespaced_is_still_one_block() -> None:
    """A backend that stamps only one end still wrote a single block."""
    assert AgentLoop._strip_think("<mm:think>weighing</think>the answer") == "the answer"


# --------------------------------------------------------------------------- #
# the transport verdict must not pre-empt this recovery                        #
# --------------------------------------------------------------------------- #
#
# The loop breaks the turn on `finish_reason == "error"` *before*
# `reply_kind` runs, so anything that reports an empty response as
# an error takes every mode in this file out of service. The tests above stub
# `chat` directly and so never reach the provider's response exit, which is
# where such a verdict is reached -- these go through it on purpose.


class _SilentAfterToolProvider(LiteLLMProvider):
    """Calls a tool, comes back with nothing, and answers once nudged.

    The shape `recovery.py` documents as the common weak-model dud: honest
    usage, no text. Built through the real `_parse_response` so a verdict at
    that exit is in the path.
    """

    def __init__(self) -> None:
        with (
            patch("raven.providers.litellm_provider.litellm"),
            patch.object(LiteLLMProvider, "_setup_env"),
        ):
            super().__init__(api_key="sk-test", provider_name="openrouter")
        self.calls = 0
        self.nudged = False

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kw):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
                usage={"prompt_tokens": 900, "total_tokens": 910},
            )
        if any(m.get("content") == POST_TOOL_NUDGE for m in messages):
            self.nudged = True
            return LLMResponse(content="here are the files", finish_reason="stop")
        # Empty, and billed truthfully: a model that said nothing, not a
        # request that never arrived.
        return self._parse_response(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=None, tool_calls=None, reasoning_content=None),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=2900, completion_tokens=1, total_tokens=2901),
            ),
            sent_chars=12000,
        )


@pytest.mark.asyncio
async def test_a_silent_model_after_a_tool_still_reaches_the_nudge(workspace):
    """Honest usage means the loop keeps ownership of the silence.

    Reported as an error instead, this turn would end on the spot -- and the
    error would claim a transport failure, which for this response is simply
    untrue.
    """
    provider = _SilentAfterToolProvider()
    agent = _make_agent(workspace, provider)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="list the files",
        ),
        session_key="s1",
    )

    assert out is not None
    assert provider.nudged is True, "the nudge never happened; the empty turn was taken as an error"
    assert out[0] == "here are the files"


# --------------------------------------------------------------------------- #
# unit: which providers accept an assistant prefill                            #
# --------------------------------------------------------------------------- #


def test_base_provider_accepts_an_assistant_prefill_by_default():
    provider = _AlwaysEmptyProvider()
    assert provider.supports_assistant_prefill("some/model") is True
    assert provider.supports_assistant_prefill(None) is True


def test_litellm_provider_refuses_an_assistant_prefill_for_anthropic_models():
    """Same judgement that decides whether thinking_blocks go on the wire.

    The illegal request is exactly "trailing assistant message + thinking
    blocks", so the two must be decided by one rule or they drift apart.
    """
    provider = LiteLLMProvider(api_key="test-key", default_model="openai/gpt-4o")
    assert provider.supports_assistant_prefill("anthropic/claude-opus-5") is False
    assert provider.supports_assistant_prefill("claude-opus-4-5") is False
    assert provider.supports_assistant_prefill("openrouter/anthropic/claude-opus-5") is False


def test_litellm_provider_accepts_an_assistant_prefill_for_other_models():
    provider = LiteLLMProvider(api_key="test-key", default_model="openai/gpt-4o")
    assert provider.supports_assistant_prefill("openai/gpt-4o") is True
    assert provider.supports_assistant_prefill("deepseek/deepseek-chat") is True
    assert provider.supports_assistant_prefill(None) is True


def test_litellm_provider_answers_for_its_default_model_when_none_is_named():
    provider = LiteLLMProvider(api_key="test-key", default_model="anthropic/claude-opus-5")
    assert provider.supports_assistant_prefill() is False


def test_delegating_providers_answer_for_the_provider_they_wrap():
    """LazyProvider / PerModelProvider are the shapes the factory actually
    builds; answering from the base default would leave the guard dead in
    production."""
    from raven.providers.base import GenerationSettings
    from raven.providers.lazy import LazyProvider
    from raven.providers.per_model_provider import PerModelProvider

    inner = LiteLLMProvider(api_key="test-key", default_model="anthropic/claude-opus-5")
    lazy = LazyProvider(factory=lambda: inner, default_model="anthropic/claude-opus-5", generation=GenerationSettings())
    assert lazy.supports_assistant_prefill("anthropic/claude-opus-5") is False
    assert lazy.supports_assistant_prefill("openai/gpt-4o") is True

    routed = PerModelProvider(models=[], fallback=inner)
    assert routed.supports_assistant_prefill("anthropic/claude-opus-5") is False
    assert routed.supports_assistant_prefill("openai/gpt-4o") is True


# --------------------------------------------------------------------------- #
# loop: no request may end on an assistant message when prefill is refused     #
# --------------------------------------------------------------------------- #


class _PrefillRefusingThinkingProvider(LLMProvider):
    """Thinking-only for its first ``thinking_calls`` calls, then an answer; refuses assistant prefills."""

    def __init__(self, thinking_calls: int = 1):
        super().__init__(api_key="test")
        self.calls = 0
        self.thinking_calls = thinking_calls
        self.last_roles: list[str] = []
        self.efforts: list[str | None] = []

    def supports_assistant_prefill(self, model: str | None = None) -> bool:
        return False

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        self.last_roles.append(str(messages[-1].get("role")))
        self.efforts.append(reasoning_effort)
        if self.calls <= self.thinking_calls:
            return LLMResponse(content="", reasoning_content="let me think", finish_reason="stop")
        return LLMResponse(content="real answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "anthropic/claude-opus-5"


@pytest.mark.asyncio
async def test_no_request_ends_on_an_assistant_message_when_prefill_is_refused(workspace):
    """Protocol invariant, not just a classification: whatever the recovery
    picks, the transcript handed to a prefill-refusing provider must never end
    with an assistant turn, and the turn must still recover its answer."""
    provider = _PrefillRefusingThinkingProvider()
    agent = _make_agent(workspace, provider)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert out is not None
    assert out[0] == "real answer"
    assert provider.calls == 2
    assert "assistant" not in provider.last_roles, f"a prefill went to the provider: {provider.last_roles}"


@pytest.mark.asyncio
async def test_a_prefill_refusing_provider_goes_straight_to_the_step_down(workspace):
    """It is never prefilled, so its thinking-only replies are stepped down at once -- ``high`` and
    then ``low`` -- and every request it is sent ends on a user message."""
    provider = _PrefillRefusingThinkingProvider(thinking_calls=2)
    agent = _make_agent(workspace, provider)

    out = await agent._process_message(_turn(), session_key="s1")

    assert out is not None and out[0] == "real answer"
    assert provider.efforts == [None, "high", "low"]
    assert provider.last_roles == ["user"] * 3, f"a prefill went to the provider: {provider.last_roles}"


class _AlwaysCutProvider(LLMProvider):
    """Every call ends on the output ceiling having produced nothing at all."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        return LLMResponse(content="", finish_reason="length")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_turn_cut_at_the_ceiling_records_it_on_the_session(workspace):
    """Recorded on the session, not on a message: this turn persists no
    assistant row at all, so a message-seated record has nowhere to land."""
    agent = _make_agent(workspace, _AlwaysCutProvider(), limits=RecoveryLimits())

    await _run_answerless(agent)

    # Recorded although the turn failed: a turn cut at the ceiling is the one
    # most likely to come back with nothing, so a marker written only on the way
    # out of a successful turn would never see the case it exists for.
    session = agent.sessions.get_or_create("s1")
    assert session.metadata.get("output_limit_turn_at") == 0


@pytest.mark.asyncio
async def test_an_answerless_turn_that_was_not_cut_records_no_output_limit(workspace):
    agent = _make_agent(workspace, _AlwaysEmptyProvider(), limits=RecoveryLimits())

    await _run_answerless(agent)

    # Said rather than left unsaid: a save merges over the record on disk, so
    # the marker is cleared by writing None. Its reader asks for an int.
    assert agent.sessions.get_or_create("s1").metadata.get("output_limit_turn_at") is None


class _CutThenAnswersProvider(LLMProvider):
    """Cut on the first call, whole on the second -- a turn that recovered."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="", finish_reason="length")
        return LLMResponse(content="the whole answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_turn_that_recovered_from_a_cut_records_no_output_limit(workspace):
    """The fact says the run's own last word was cut, so a turn that went on to
    deliver must not carry it -- a reader told otherwise would weigh a
    completed answer as a truncated one."""
    agent = _make_agent(workspace, _CutThenAnswersProvider(), limits=RecoveryLimits())

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert out is not None and out[0] == "the whole answer"
    # Said rather than left unsaid: a save merges over the record on disk, so
    # the marker is cleared by writing None. Its reader asks for an int.
    assert agent.sessions.get_or_create("s1").metadata.get("output_limit_turn_at") is None


@pytest.mark.asyncio
async def test_a_turn_that_was_not_cut_clears_a_marker_left_by_an_earlier_one(workspace):
    """The slot has to be turn-scoped by being written OR cleared every turn.

    Scoping it by the message index alone assumes that index only grows, and
    `Session.clear()` (what `/new` calls) and `undo_last_turn` both move it
    back without touching metadata -- so an old marker can sit at an index a
    later turn's own start satisfies, and that turn gets reported as cut.
    """
    agent = _make_agent(workspace, _AlwaysEmptyProvider(), limits=RecoveryLimits())
    agent.sessions.get_or_create("s1").metadata["output_limit_turn_at"] = 0

    # And a turn that fails clears it too: the failing turn is the likeliest one
    # to be reported as cut by a marker it did not write.
    await _run_answerless(agent)

    # Said rather than left unsaid: a save merges over the record on disk, so
    # the marker is cleared by writing None. Its reader asks for an int.
    assert agent.sessions.get_or_create("s1").metadata.get("output_limit_turn_at") is None


# --------------------------------------------------------------------------- #
# the silence gate: a marker never reaches a reader                            #
# --------------------------------------------------------------------------- #


def _collecting() -> tuple[list[str], object]:
    seen: list[str] = []

    async def deliver(text: str) -> None:
        seen.append(text)

    return seen, deliver


@pytest.mark.asyncio
async def test_the_gate_lets_a_reply_through_once_it_cannot_be_a_marker():
    seen, deliver = _collecting()
    gate = SilenceGate(deliver, hold_all=False)

    await gate("He")

    assert seen == ["He"], "a start that is no marker prefix is not held at all"
    await gate("llo")
    assert seen == ["He", "llo"], "and what follows goes straight out"


@pytest.mark.asyncio
async def test_the_gate_holds_a_marker_and_drops_it():
    seen, deliver = _collecting()
    gate = SilenceGate(deliver, hold_all=False)

    for delta in ("NO", "_REP", "LY"):
        await gate(delta)
    assert seen == [], "every prefix of a marker is held, the whole marker included"
    gate.discard()
    await gate("!")
    await gate.release()

    assert seen == [], "nothing is delivered once the gate is discarded"


@pytest.mark.asyncio
async def test_a_held_prefix_that_was_not_a_marker_goes_out_at_the_end():
    seen, deliver = _collecting()
    gate = SilenceGate(deliver, hold_all=False)

    await gate("NO")
    assert seen == []
    await gate.release()
    assert seen == ["NO"]
    await gate("pe")
    assert seen == ["NO", "pe"], "a released gate is open for the rest of the call"


@pytest.mark.asyncio
async def test_a_held_prefix_goes_out_whole_when_the_reply_turns_out_to_be_prose():
    """The cache and what came after leave together, so the reader sees the reply once and in order."""
    seen, deliver = _collecting()
    gate = SilenceGate(deliver, hold_all=False)

    await gate("[SILENT]")
    await gate(" nothing")
    assert seen == ["[SILENT] nothing"]
    await gate(" new here")
    assert seen == ["[SILENT] nothing", " new here"]


@pytest.mark.asyncio
async def test_the_gate_holds_leading_whitespace_until_the_reply_shows_what_it_is():
    """A newline before the marker says nothing about the reply, so it is not the point of no return."""
    seen, deliver = _collecting()
    gate = SilenceGate(deliver, hold_all=False)

    await gate("\n")
    await gate("[SILENT]")
    assert seen == []
    gate.discard()
    assert seen == []

    seen, deliver = _collecting()
    gate = SilenceGate(deliver, hold_all=False)
    await gate("\n")
    await gate("Hello")
    assert seen == ["\nHello"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deltas",
    [
        pytest.param(["*", "NO_REPLY", "*"], id="bold_marker"),
        pytest.param(["**", "[SILENT]", "**"], id="bold_bracketed_marker"),
        pytest.param([".", "NO_REPLY"], id="stray_full_stop"),
        pytest.param(["\n\n", "-", " SILENT"], id="whitespace_and_a_dash"),
    ],
)
async def test_the_gate_holds_a_marker_wrapped_in_punctuation(deltas):
    """The strict rule forgives edge punctuation, so ``*NO_REPLY*`` is a marker and none of it may be shown:
    the ``*`` that arrives first is not the point at which the reply stopped being one."""
    seen, deliver = _collecting()
    gate = SilenceGate(deliver, hold_all=False)

    for delta in deltas:
        await gate(delta)
    assert seen == []
    gate.discard()

    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["42", "1.", "\u4f60", "caf\u00e9"])
async def test_a_reply_that_opens_on_a_letter_or_a_digit_is_not_held(first):
    """No marker begins with a digit or a non-Latin letter, so the first delta already shows what the reply is."""
    seen, deliver = _collecting()
    gate = SilenceGate(deliver, hold_all=False)

    await gate(first)

    assert seen == [first]


@pytest.mark.asyncio
async def test_the_gate_lets_punctuation_that_leads_prose_through_once_the_prose_shows():
    seen, deliver = _collecting()
    gate = SilenceGate(deliver, hold_all=False)

    await gate("**")
    assert seen == [], "nothing readable yet"
    await gate("Summary")
    assert seen == ["**Summary"], "released whole, in order, the moment the reply is plainly not a marker"
    await gate("**")
    assert seen == ["**Summary", "**"]


@pytest.mark.asyncio
async def test_a_proactive_turn_holds_the_whole_call():
    seen, deliver = _collecting()
    gate = SilenceGate(deliver, hold_all=True)

    await gate("2 deals filtered")
    await gate("\n\n")
    assert seen == [], "the marker may be the last line, so nothing is known until the call ends"
    await gate.release()
    assert seen == ["2 deals filtered\n\n"]
    await gate("[SILENT]")
    assert seen == ["2 deals filtered\n\n", "[SILENT]"], "released, it is open"

    seen, deliver = _collecting()
    gate = SilenceGate(deliver, hold_all=True)
    await gate("2 deals filtered\n\n")
    await gate("[SILENT]")
    gate.discard()
    assert seen == []


# --------------------------------------------------------------------------- #
# loop: a reply that is a silence marker ends the turn without being shown     #
# --------------------------------------------------------------------------- #


def _chunked(text: str, size: int = 4) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def _preamble_then_tool_stream(preamble: str) -> _Stream:
    call = SimpleNamespace(index=0, id="c1", function=SimpleNamespace(name="list_dir", arguments='{"path": "."}'))
    calling = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=[call], reasoning_content=None), finish_reason=None
            )
        ],
        usage=None,
    )
    return _Stream([_chunk(preamble), calling, _chunk(finish_reason="tool_calls")], received_finish_reason="tool_calls")


@pytest.mark.asyncio
async def test_a_marker_reply_closes_the_loop_silent_and_never_streams(workspace, monkeypatch):
    calls = _served(monkeypatch, [_finished_stream(*_chunked("[SILENT]"))])
    sink = _Sink()

    final, _used, messages, outcome = await _litellm_agent(workspace)._run_agent_loop(
        _seed(), on_token_delta=sink.delta
    )

    assert len(calls) == 1, "a marker is the model's answer, so recovery has nothing to mend"
    assert final == "[SILENT]" and outcome.silent is True
    assert outcome.status == "completed" and outcome.error is None and outcome.no_output is False
    last = messages[-1]
    assert (last["role"], last["content"], last.get("_silent")) == ("assistant", "[SILENT]", True)
    assert sink.text == [], "the marker is held back and then dropped, so no reader sees any of it"


@pytest.mark.asyncio
async def test_a_marker_wrapped_in_punctuation_is_silent_and_never_streams(workspace, monkeypatch):
    _served(monkeypatch, [_finished_stream("**", "[SILENT]", "**")])
    sink = _Sink()

    final, _used, messages, outcome = await _litellm_agent(workspace)._run_agent_loop(
        _seed(), on_token_delta=sink.delta
    )

    assert outcome.silent is True and final == "**[SILENT]**"
    assert messages[-1].get("_silent") is True
    assert sink.text == []


@pytest.mark.asyncio
async def test_a_cron_turn_may_put_the_marker_on_its_last_line(workspace, monkeypatch):
    _served(monkeypatch, [_finished_stream("2 deals ", "filtered\n\n", "[SILENT]")])
    sink = _Sink()

    final, _used, messages, outcome = await _litellm_agent(workspace)._run_agent_loop(
        _seed(), on_token_delta=sink.delta, origin=Origin.CRON
    )

    assert outcome.silent is True and final == "2 deals filtered\n\n[SILENT]"
    assert messages[-1].get("_silent") is True
    assert sink.text == [], "the marker closed a report only the whole call could judge, so none of it went out"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("[SILENT] nothing new here", id="marker_then_prose"),
        pytest.param("Reply NO_REPLY if you agree", id="marker_in_a_sentence"),
        pytest.param("2 deals filtered\n\n[SILENT]", id="the_cron_report_on_a_persons_turn"),
    ],
)
async def test_a_user_reply_that_only_mentions_a_marker_is_delivered(workspace, monkeypatch, reply):
    """The strict rule: a person is waiting, so anything the model said beside a marker is for them."""
    _served(monkeypatch, [_finished_stream(*_chunked(reply))])
    sink = _Sink()

    final, _used, messages, outcome = await _litellm_agent(workspace)._run_agent_loop(
        _seed(), on_token_delta=sink.delta
    )

    assert final == reply and outcome.silent is False
    assert "_silent" not in messages[-1]
    assert "".join(sink.text) == reply, "held while it could still be a marker, then shown whole and in order"


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [Origin.USER, Origin.CRON])
async def test_a_reply_that_is_no_marker_is_shown_whole(workspace, monkeypatch, origin):
    _served(monkeypatch, [_finished_stream("2 deals ", "filtered")])
    sink = _Sink()

    final, _used, _messages, outcome = await _litellm_agent(workspace)._run_agent_loop(
        _seed(), on_token_delta=sink.delta, origin=origin
    )

    assert final == "2 deals filtered" and outcome.silent is False
    if origin is Origin.CRON:
        assert sink.text == ["2 deals filtered"], "a proactive turn holds the call and shows it in one piece"
    else:
        assert sink.text == ["2 deals ", "filtered"], "a person's reply types out as it always did"


@pytest.mark.asyncio
async def test_an_error_whose_body_is_a_marker_is_still_a_failure(workspace):
    """A marker is the model's own choice; an error has no model behind it to have made one."""

    class _Provider:
        async def chat_with_retry(self, messages, tools=None, model=None, fallback_models=None, **_kw):
            return LLMResponse(
                content="NO_REPLY",
                finish_reason="error",
                error_classification=ErrorClassification("auth", retryable=False, should_fallback=False),
            )

        def get_default_model(self):
            return "stub"

    agent = _make_agent(workspace, _Provider(), RecoveryLimits(llm_error_retry_delays=()))

    _final, _used, messages, outcome = await agent._run_agent_loop(_seed())
    failed = await _fail(agent)

    assert outcome.status == "error" and outcome.error and outcome.silent is False
    assert not any(m.get("_silent") for m in messages)
    assert isinstance(failed, AnswerlessTurnError)


@pytest.mark.asyncio
async def test_a_marker_held_for_a_hook_is_still_never_shown(workspace, monkeypatch):
    """With a hook that can send a reply back the loop holds the whole text for it; the marker
    comes out of that hold into the silence gate, which is where it stops."""
    _served(monkeypatch, [_finished_stream("[SILENT]")])
    sink = _Sink()

    _final, _used, _messages, outcome = await _litellm_agent(workspace, hooks=[_Holds()])._run_agent_loop(
        _seed(), on_token_delta=sink.delta
    )

    assert outcome.silent is True and sink.text == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin,reply",
    [
        pytest.param(Origin.USER, "NO", id="user_a_marker_prefix_that_is_no_marker"),
        pytest.param(Origin.CRON, "2 deals filtered", id="proactive_report"),
    ],
)
async def test_a_reply_held_for_a_hook_is_shown_once_it_is_kept(workspace, monkeypatch, origin, reply):
    """The hook's hold and the silence gate's hold are two, and a kept reply is let go of by both."""
    _served(monkeypatch, [_finished_stream(reply)])
    sink = _Sink()

    final, _used, _messages, outcome = await _litellm_agent(workspace, hooks=[_Holds()])._run_agent_loop(
        _seed(), on_token_delta=sink.delta, origin=origin
    )

    assert final == reply and outcome.silent is False
    assert sink.text == [reply]


class _Replaces(AgentHook):
    @property
    def name(self) -> str:
        return "Replaces"

    async def after_iteration(self, ctx):
        from raven.agent.hook import HookDecision

        return HookDecision(short_circuit_result="the hook's own words")


@pytest.mark.asyncio
async def test_a_hook_that_replaces_a_marker_reply_leaves_the_turn_not_silent(workspace, monkeypatch):
    _served(monkeypatch, [_finished_stream("[SILENT]")])
    sink = _Sink()

    final, _used, messages, outcome = await _litellm_agent(workspace, hooks=[_Replaces()])._run_agent_loop(
        _seed(), on_token_delta=sink.delta
    )

    assert final == "the hook's own words" and outcome.silent is False
    assert sink.text == [], "the marker was the draft the hook replaced, and nobody saw a draft"
    assert not any(m.get("_silent") for m in messages)


@pytest.mark.asyncio
async def test_a_marker_after_a_cut_answer_leaves_what_streamed_as_the_answer(workspace, monkeypatch):
    """The reader already has the start of an answer, so a marker after it ends that answer; it does not take it back."""
    calls = _served(monkeypatch, [_DyingStream("Hello "), _finished_stream("[SILENT]")])
    sink = _Sink()

    final, _used, messages, outcome = await _litellm_agent(workspace)._run_agent_loop(
        _seed(), on_token_delta=sink.delta
    )

    assert len(calls) == 2
    assert final == "Hello" and outcome.silent is False and outcome.status == "completed"
    assert "".join(sink.text) == "Hello ", "the marker is held back and dropped all the same"
    assert (messages[-1]["role"], messages[-1]["content"]) == ("assistant", "Hello")
    assert "_silent" not in messages[-1]


@pytest.mark.asyncio
async def test_a_cut_reply_that_began_like_a_marker_still_shows_its_start(workspace, monkeypatch):
    """A held start is the answer so far once the stream is cut, and the continuation follows it."""
    _served(monkeypatch, [_DyingStream("NO"), _finished_stream(" problem at all.")])
    sink = _Sink()

    final, _used, _messages, outcome = await _litellm_agent(workspace)._run_agent_loop(
        _seed(), on_token_delta=sink.delta
    )

    assert final == "NO problem at all." and outcome.silent is False
    assert "".join(sink.text) == "NO problem at all."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hooks,origin",
    [
        pytest.param([_Holds()], Origin.USER, id="held_for_a_hook"),
        pytest.param([], Origin.CRON, id="held_by_the_silence_gate"),
        pytest.param([_Holds()], Origin.CRON, id="held_by_both"),
    ],
)
async def test_words_beside_a_tool_call_reach_the_reader_before_the_tool_runs(workspace, monkeypatch, hooks, origin):
    """Neither hold parks a preamble for the length of the tool run: it is not the reply a hook can send
    back, nor one a marker could be, so it is let go of the moment the call is known to end in a tool."""
    _served(monkeypatch, [_preamble_then_tool_stream("Checking the inbox"), _finished_stream("done")])
    agent = _litellm_agent(workspace, hooks=hooks)
    sink = _Sink()
    shown_when_the_tool_ran: list[list[str]] = []
    real_execute = agent.tools.execute

    async def execute(name, arguments, **kwargs):
        shown_when_the_tool_ran.append(list(sink.text))
        return await real_execute(name, arguments, **kwargs)

    monkeypatch.setattr(agent.tools, "execute", execute)

    await agent._run_agent_loop(_seed(), on_token_delta=sink.delta, origin=origin)

    assert shown_when_the_tool_ran == [["Checking the inbox"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin,preamble,ending,seen",
    [
        pytest.param(
            Origin.USER, "NO", "There is one file.", ["NO", "There is one file."], id="user_start_of_a_marker"
        ),
        pytest.param(
            Origin.CRON, "Checking the inbox", "[SILENT]", ["Checking the inbox"], id="proactive_words_then_a_marker"
        ),
    ],
)
async def test_the_words_beside_a_tool_call_are_released_with_it(
    workspace, monkeypatch, origin, preamble, ending, seen
):
    """Words said beside a tool call are not a reply a marker could be, so a hold on them ends with the call."""
    _served(monkeypatch, [_preamble_then_tool_stream(preamble), _finished_stream(ending)])
    sink = _Sink()

    _final, _used, _messages, _outcome = await _litellm_agent(workspace)._run_agent_loop(
        _seed(), on_token_delta=sink.delta, origin=origin
    )

    assert sink.text == seen


# --------------------------------------------------------------------------- #
# turn: what a silent turn delivers, files and tells its hooks                 #
# --------------------------------------------------------------------------- #


class _Sees(AgentHook):
    """An ``after_send`` hook that writes down what it is handed and composes ``reply`` when it has one."""

    def __init__(self, reply: str | None = None) -> None:
        self.outbound: list[str | None] = []
        self._reply = reply

    @property
    def name(self) -> str:
        return "Sees"

    async def after_send(self, ctx):
        from raven.agent.hook import HookDecision

        self.outbound.append(ctx.outbound_content)
        return HookDecision() if self._reply is None else HookDecision(modified_content=self._reply)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [Origin.USER, Origin.CRON])
async def test_a_marker_reply_returns_nothing_and_files_a_flagged_row(workspace, monkeypatch, origin):
    """No reply, the row kept and flagged, nothing streamed, and the turn says it was silent."""
    _served(monkeypatch, [_finished_stream(*_chunked("[SILENT]"))])
    agent = _litellm_agent(workspace)
    sink = _Sink()
    silence: dict[str, bool] = {}

    out = await agent._process_message(
        _turn(origin=origin), session_key="s1", on_token_delta=sink.delta, silence=silence
    )

    assert out is None
    assert silence == {"silent": True}
    assert sink.text == [], "none of the marker reached a reader"
    rows = agent.sessions.get_or_create("s1").messages
    assert [(m["role"], m["content"]) for m in rows] == [("user", "hi"), ("assistant", "[SILENT]")]
    assert rows[-1]["silent"] is True and "silent" not in rows[0]


@pytest.mark.asyncio
async def test_a_marker_reply_ends_the_turn_silently(workspace, monkeypatch):
    """A person's turn, the reply is ``[SILENT]``: the turn's exit, end to end through the real adapter."""
    from raven.spine.events import Notice, StreamDelta, Text

    _served(monkeypatch, [_finished_stream("[SIL", "ENT]")])
    agent = _litellm_agent(workspace)

    async def _noop(**_kw) -> None:
        return None

    agent._start_executor = _noop
    agent._connect_mcp = _noop
    returned: list = []
    process = agent._process_message

    async def spy(*args, **kwargs):
        returned.append(await process(*args, **kwargs))
        return returned[-1]

    monkeypatch.setattr(agent, "_process_message", spy)
    events: list = []

    async def emit(event) -> None:
        events.append(event)

    outcome = await agent.run_turn(_turn("anything new?"), emit, lambda: [])

    assert returned == [None], "nothing is left to deliver"
    rows = agent.sessions.get_or_create("test:c1").messages
    assert (rows[-1]["role"], rows[-1]["content"], rows[-1]["silent"]) == ("assistant", "[SILENT]", True)
    deltas = [e.delta for e in events if isinstance(e, StreamDelta)]
    assert not any(ch in delta for delta in deltas for ch in "[SILENT]"), deltas
    assert not any(isinstance(e, Text) for e in events)
    assert [e.kind for e in events if isinstance(e, Notice)] == [NoticeKind.SILENT_TURN]
    assert outcome.silent is True and outcome.explicit_reply is False


@pytest.mark.asyncio
async def test_a_marker_from_a_plain_call_ends_the_turn_silently(workspace, monkeypatch):
    """The same turn on a surface that does not stream: a plain call, one reply, which is the marker."""
    _served(monkeypatch, [_reply("NO_REPLY")])
    agent = _litellm_agent(workspace)
    silence: dict[str, bool] = {}

    out = await agent._process_message(_turn(), session_key="s1", silence=silence)

    assert out is None and silence == {"silent": True}
    assert agent.sessions.get_or_create("s1").messages[-1]["silent"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply,outbound",
    [
        pytest.param("[SILENT]", [None], id="silent_turn"),
        pytest.param("all done", ["all done"], id="an_ordinary_turn"),
    ],
)
async def test_after_send_runs_on_a_silent_turn_with_no_outbound(workspace, monkeypatch, reply, outbound):
    """The chain keeps the turn's books on a silent turn too, so it runs; it is only told there is nothing to send."""
    _served(monkeypatch, [_reply(reply)])
    sees = _Sees()
    modified: list[tuple[str, str]] = []

    def modifier(key: str, content: str) -> str:
        modified.append((key, content))
        return content + " [nudge]"

    from raven.agent.hook.adapters import ResponseModifierAdapter

    agent = _litellm_agent(workspace, hooks=[sees, ResponseModifierAdapter(modifier)])

    out = await agent._process_message(_turn(), session_key="s1")

    assert sees.outbound == outbound
    if reply == "[SILENT]":
        assert out is None
        assert modified == [], "a queued nudge is kept for the next reply, not spent on a message nobody gets"
    else:
        assert out == ("all done [nudge]", [])


@pytest.mark.asyncio
async def test_a_silent_turn_is_not_silenced_by_a_hook_that_composes_nothing(workspace, monkeypatch):
    _served(monkeypatch, [_reply("[SILENT]")])
    sink: dict[str, str] = {}
    silence: dict[str, bool] = {}
    agent = _litellm_agent(workspace, hooks=[_Sees(reply="  \n")])

    out = await agent._process_message(_turn(), session_key="s1", silence=silence, hook_sink=sink)

    assert out is None and silence == {"silent": True}
    assert sink == {}, "blank text is not something to send"


@pytest.mark.asyncio
async def test_text_a_hook_composes_on_a_silent_turn_is_delivered(workspace, monkeypatch):
    """``after_send`` still runs on a silent turn, and text a hook composes there is the reply."""
    _served(monkeypatch, [_reply("[SILENT]")])
    sink: dict[str, str] = {}
    silence: dict[str, bool] = {}
    agent = _litellm_agent(workspace, hooks=[_Sees(reply="\n\nDeck: /out/deck.pptx")])

    out = await agent._process_message(_turn(), session_key="s1", silence=silence, hook_sink=sink)

    assert out == ("Deck: /out/deck.pptx", []), "the separator the hook put in front separates nothing"
    assert silence == {}, "a turn that replied was not silent"
    assert sink == {"appended": "\n\nDeck: /out/deck.pptx"}, "the tail a streaming reader is sent keeps it"
    assert agent.sessions.get_or_create("s1").messages[-1]["content"] == "[SILENT]", "history keeps the model's own row"


@pytest.mark.asyncio
async def test_a_proactive_turn_that_skips_after_send_stays_silent(workspace, monkeypatch):
    _served(monkeypatch, [_reply("nothing changed\n\n[SILENT]")])
    sees = _Sees(reply="never asked")
    silence: dict[str, bool] = {}

    out = await _litellm_agent(workspace, hooks=[sees])._process_message(
        _turn(origin=Origin.SENTINEL), session_key="s1", origin=Origin.SENTINEL, silence=silence
    )

    assert out is None and silence == {"silent": True}
    assert sees.outbound == [], "a sentinel turn has never run the send chain"


@pytest.mark.asyncio
async def test_a_marker_after_the_message_tool_sent_is_not_a_silent_turn(workspace, monkeypatch):
    """The reader has the answer from the tool; the marker only says there is nothing more.
    A turn that answered is not a turn that said nothing, and a surface told otherwise would drop the answer."""
    _served(monkeypatch, [_tool_reply("message", _SEND), _reply("NO_REPLY")])
    agent = _litellm_agent(workspace)
    sent = _wire_message_tool(agent)
    silence: dict[str, bool] = {}

    out = await agent._process_message(_turn(), session_key="s1", silence=silence)

    assert sent == ["here is your answer"]
    assert out is None and silence == {}
    assert agent.sessions.get_or_create("s1").messages[-1]["content"] == "NO_REPLY"


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,taught", [("[SILENT]", []), ("done", [("hi", "done")])])
async def test_a_silent_turn_teaches_the_personalizer_nothing(workspace, monkeypatch, reply, taught):
    learned: list[tuple[str, str]] = []

    class _Personalizer:
        def __init__(self, memory, provider, model) -> None:
            pass

        async def classify(self, message, history=None):
            return {"needs_clarification": False}

        async def post_learn(self, message, answer) -> None:
            learned.append((message, answer))

    monkeypatch.setattr("raven.agent.personalizer.Personalizer", _Personalizer)
    _served(monkeypatch, [_reply(reply)])
    agent = _litellm_agent(workspace)
    agent.enable_personalization = True

    await agent._process_message(_turn(), session_key="s1")
    await asyncio.gather(*list(agent._consolidation_tasks))

    assert learned == taught


@pytest.mark.asyncio
async def test_after_turn_memory_is_handed_the_markers_text_on_a_silent_turn(workspace, monkeypatch):
    """What the turn filed is still handed to the memory pipeline: the marker row is a row of the turn."""
    _served(monkeypatch, [_reply("[SILENT]")])
    agent = _litellm_agent(workspace)
    seen: list[dict] = []

    async def after_turn(key, record):
        seen.append(record)

    monkeypatch.setattr(agent.harness.memory, "after_turn", after_turn)

    await agent._process_message(_turn(), session_key="s1")

    assert [record["final_content"] for record in seen] == ["[SILENT]"]
    assert seen[0]["messages"][-1]["content"] == "[SILENT]"
