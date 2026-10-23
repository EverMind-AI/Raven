"""Empty-response recovery for the agent loop.

Pure decision logic, no I/O -- the loop owns the side effects (appending
messages, counting, ``continue``). Kept separate from the loop so the branching
can be unit-tested in isolation, mirroring the repo's other small policy units
(nudge_policy, decision_router).

A model reply that carries no tool call is read as one :class:`ReplyKind`, and
:func:`decide` maps that kind and what the turn has already spent
(:class:`RecoveryState`) to the next :class:`RecoveryAction`. The first row that
matches wins:

  text                 a reply to deliver                      COMPLETE
  silence marker       the model chose to say nothing          SILENT
  refusal              the upstream declined                   FALLBACK once, then FAIL
  stream cut           text from a stream that died            CONTINUE, then COMPLETE with the partial
  thinking only        reasoning and no answer                 PREFILL, STEP_DOWN, FALLBACK, FAIL
  cut, nothing in it   stopped at the ceiling, empty           CONTINUE with more room, then FAIL
  empty after a tool   nothing right after tool results        NUDGE once, then as an empty reply
  empty                nothing at all                          RETRY, FALLBACK, FAIL

A refusal is never resent, and outranks the text that came with it: that text is
the model's explanation. A stream that died mid-answer (``STREAM_CUT``) is asked to
go on from what it sent, up to ``MAX_CONTINUATIONS`` times; one that closed cleanly
without a terminal chunk, and a body that names no finish, are whole answers. A cut
at the output ceiling is asked again with more room for the answer, and the effort is
left alone because nothing in it names the reasoning as the cause.

PREFILL re-feeds the model its own reasoning so it continues into the answer. A
provider that refuses a trailing assistant message (its
``supports_assistant_prefill`` answers False: Anthropic with thinking on) never
gets it, and goes straight to STEP_DOWN, so the request handed back never ends on
an assistant message; so does a reply whose reasoning is known only by its token
count (OpenAI and Azure hide the text), which leaves nothing to re-feed.
STEP_DOWN asks the same step again at ``high`` and then at ``low``, whichever lie
below the turn's effort (an unstated, ``max`` or ``xhigh`` effort gets both, ``high``
or ``medium`` only ``low``, ``low`` or ``minimal`` neither), once each a turn --
never ``minimal``, which Codex refuses with a 400.
RETRY resends the request unchanged after a jittered backoff, because nothing in a
plain empty reply says what to change; two zero-output replies of one model, or a
reply whose reported cost is dear, cut the resends short. A fallback model gets
one try: on one nothing is repeated.

Spending every move without an answer is FAIL, and the loop ends the turn as an
error the caller fails the turn on -- unless one of the turn's tools has already
put an answer in front of the reader. It is not another move -- there is nothing
left to try -- it is the answer to the question the caller asks next, which is
whether this turn produced anything.

Both functions are pure: the loop supplies the facts they cannot see (the
provider's prefill support, whether the reply carries thinking to re-feed, the
rung to step down to, the fallback models left, the resends the cost allows) and
does the acting.

This is distinct from Sentinel's NudgeInjector / NudgePolicy, which inject
*proactive suggestions* onto an outbound reply; this module instead recovers an
empty turn before it is ever sent.
"""

from __future__ import annotations

import random
import re
import unicodedata
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from enum import Enum, auto

from raven.config import schema as config_schema
from raven.contracts.llm_provider import REASONING_EFFORT_LADDER, LLMResponse
from raven.providers.streaming import DECLINED_FINISH_REASONS, STREAM_CUT
from raven.providers.usage import reported_cost, token_count
from raven.spine.silence import SILENCE_MAX_CHARS, is_silence, may_become_silence
from raven.spine.turn import Origin

# In-content thinking markers. Some models (Ollama, certain Qwen gateways) put
# the reasoning in ``content`` as <think>…</think> rather than in the structured
# reasoning_content field, so a content scan is required — checking only the
# structured fields would miss them.
# Matches an opening or a closing tag, with or without a vendor namespace
# prefix (``<mm:think>``). A turn cut off inside an inlined block arrives as
# a lone closing tag, so an opener-only pattern reads it as ordinary prose.
_THINK_TAG_RE = re.compile(r"</?(?:[a-z][\w.-]{0,15}:)?(?:think|thinking|reasoning)>", re.IGNORECASE)
_NS = r"(?:[a-z][\w.-]{0,15}:)?"
_THINK_BLOCK_RE = re.compile(
    rf"<{_NS}(think|thinking|reasoning)>([\s\S]*?)</{_NS}\1>",
    re.IGNORECASE,
)

MAX_CONTINUATIONS = 3
REASONING_EXCERPT_MAX = 500
EMPTY_RETRY_BASE_DELAY_S = 5.0
EMPTY_RETRY_MAX_DELAY_S = 60.0

# Worded after Hermes, without the emoji Hermes puts on its failures, except where Raven's narrower
# trigger would make Hermes' words false. OUTPUT_LIMIT_CONTINUATION follows a cut that left no text,
# no reasoning and no tool call, so it keeps Hermes' frame but cannot ask the model to go on from where
# it stopped, nor presume an answer was owed rather than a call. POST_TOOL_NUDGE also allows a final
# answer: the tool results may be all the task needed.
POST_TOOL_NUDGE = "You just executed tool calls but returned an empty response. Please process the tool results above and continue with the task, or give your final answer now."
OUTPUT_LIMIT_CONTINUATION = "[System: Your previous response was cut off by the output length limit before it produced any text or tool call, so nothing from it was kept. Keep this response short: answer briefly, or make the tool call the task needs. If a payload you were writing is too large for one response, send it across several smaller calls.]"
NETWORK_CUT_CONTINUATION = "[System: The previous response was cut off by a network error mid-stream. Continue exactly where you left off. Do not restart or repeat prior text. Finish the answer directly.]"
EMPTY_REPLY_FAILURE = "No reply: the model returned empty content after retries and any fallback models. Try `continue`, switch the model, or check the tool output above."
THINKING_ONLY_FAILURE = "The model produced only internal reasoning and no final answer, despite retries. Its last reasoning, which may contain the answer:"
# For a turn that had nothing to ask again with: no prefill the provider takes, no rung, no fallback model.
THINKING_ONLY_UNRETRIED_FAILURE = (
    "The model produced only internal reasoning and no final answer. Its last reasoning, which may contain the answer:"
)
TRUNCATED_FAILURE = "Processing stopped: the response remained truncated after {n} continuation attempt{s}. Try again."
REFUSAL_FAILURE = "The model declined to respond to this request (a safety refusal, not a Raven failure)."
REFUSAL_EXPLANATION = " The model's explanation: {text}"
REFUSAL_NO_EXPLANATION = " The model returned no explanation."
PARTIAL_STREAM_NOTE = (
    "No reply: streaming stopped early and only a partial response was recovered. Send `continue` to resume."
)


class ReplyKind(Enum):
    """What a reply without a tool call is (:func:`reply_kind`)."""

    TEXT = auto()  # an answer to deliver
    SILENT = auto()  # a silence marker: the model chose to say nothing
    REFUSAL = auto()  # the upstream declined; any text is the model's explanation
    NETWORK_CUT = auto()  # a stream that died after sending text
    THINKING_ONLY = auto()  # reasoning and no answer
    TRUNCATED = auto()  # stopped at the output ceiling with nothing in it
    POST_TOOL_EMPTY = auto()  # nothing, right after the turn ran tools
    EMPTY = auto()  # nothing at all


class RecoveryAction(Enum):
    """What the loop should do about an empty assistant response."""

    COMPLETE = auto()  # visible text present, partial text to deliver as it stands, or recovery off → finish the turn
    SILENT = auto()  # a silence marker → end the turn without delivering it
    PREFILL = auto()  # thinking-only → re-feed reasoning, re-request
    NUDGE = auto()  # post-tool empty → inject (empty) + user nudge, re-request
    RETRY = auto()  # plain empty → re-request as-is
    STEP_DOWN = auto()  # thinking-only, prefills spent → re-request at the next effort down
    CONTINUE = auto()  # cut off → ask the model to carry on where it stopped
    FALLBACK = auto()  # → re-request on the next fallback model
    FAIL = auto()  # budgets spent with nothing to show → the turn fails, it does not reply


@dataclass(frozen=True)
class RecoveryLimits:
    """Per-turn retry budgets."""

    enabled: bool = True
    post_tool_empty_max_nudges: int = 1
    thinking_prefill_max_retries: int = 2
    empty_content_max_retries: int = 3
    #: The reported cost (USD) of one empty reply at which it is resent once
    #: rather than ``empty_content_max_retries`` times (:func:`resend_budget`).
    empty_retry_cost_threshold_usd: float = 0.25
    #: How long the loop waits, and how many times, when a model call comes back as
    #: a retryable error after the provider's own ladder gave up. The provider's
    #: ladder is seconds long -- right for a dropped connection, and too short for
    #: a gateway that serves error pages for a few minutes. One measured run had 62
    #: minutes and 23.8M tokens of work behind it when a 40-second outage ended it.
    llm_error_retry_delays: tuple[float, ...] = field(
        default_factory=lambda: config_schema.LLM_ERROR_RETRY_DELAYS_DEFAULT
    )
    #: Whether a streamed call that failed after it had already produced output is
    #: asked again, whole: a person watching the stream has seen the words and would
    #: see them twice. Off, the loop's answering call hands back what streamed: with
    #: recovery ``enabled`` the model is asked to go on from it (``NETWORK_CUT``), and
    #: without it the words are delivered with ``PARTIAL_STREAM_NOTE`` and nothing
    #: more is asked. Any other streamed call has no continuation, so the call fails
    #: and its caller decides what follows: an exhausted turn's wrap-up ends on its
    #: static line, the watch-work judgement leaves the turn unjudged, and a
    #: sub-agent's run fails (N-TURNFAILED). On, for an unattended agent whose
    #: client is a machine: one measured deck build had two hours behind it when a
    #: mid-stream "Network connection lost" ended the turn with nothing published.
    llm_retry_after_output: bool = False
    #: Base64 bytes of tool-shown pictures one request may carry before the standing
    #: image window collapses to the newest messages (``agents.defaults.
    #: imageWindowBudgetBytes``). Counted as the pictures travel, encoded, because
    #: that is what the request body and the gateway are charged for. 0 turns the
    #: standing pass off: pictures then leave only through the ladder that answers a
    #: size refusal.
    image_window_budget_bytes: int = 12_000_000


def limits_from_defaults(defaults: object) -> RecoveryLimits:
    """Build limits from an ``agents.defaults`` config object (duck-typed).

    Centralizes the config→RecoveryLimits mapping so the several AgentLoop
    construction sites don't each repeat the field plumbing.
    """
    return RecoveryLimits(
        enabled=getattr(defaults, "empty_recovery_enabled", True),
        post_tool_empty_max_nudges=getattr(defaults, "post_tool_empty_max_nudges", 1),
        thinking_prefill_max_retries=getattr(defaults, "thinking_prefill_max_retries", 2),
        empty_content_max_retries=getattr(defaults, "empty_content_max_retries", 3),
        empty_retry_cost_threshold_usd=getattr(defaults, "empty_retry_cost_threshold_usd", 0.25),
        llm_error_retry_delays=_ladder(getattr(defaults, "llm_error_retry_delays", None)),
        llm_retry_after_output=bool(getattr(defaults, "llm_retry_after_output", False)),
        image_window_budget_bytes=max(0, int(getattr(defaults, "image_window_budget_bytes", 12_000_000))),
    )


def _ladder(configured: object) -> tuple[float, ...]:
    """The outer retry ladder as configured, where an explicit `[]` means none.

    Only an absent setting takes the default: `[]` is the documented way to turn
    the ladder off, and `or` read it as absent and put the 105 seconds back.
    """
    if configured is None:
        return config_schema.LLM_ERROR_RETRY_DELAYS_DEFAULT
    return tuple(float(delay) for delay in configured)


@dataclass(frozen=True)
class EmptyAttempt:
    """One empty reply of the turn's current streak, as :func:`deterministic_empty` reads it."""

    model: str
    finish_reason: str
    usage_present: bool  # a prompt was counted (cache included) and completion_tokens is reported
    zero_output: bool  # usage_present and completion_tokens + reasoning_tokens == 0

    @classmethod
    def of(cls, response: LLMResponse, model: str) -> EmptyAttempt:
        """The attempt ``response`` was, read with the providers' rules for a count.

        A usage object with no prompt counted in it is no evidence -- a proxy can
        send one with every field zero -- so it fails open, as Hermes's guard does.
        """
        usage = _usage(response)
        prompt = token_count(usage.get("prompt_tokens")) or 0
        if not usage.get("prompt_tokens_include_cache", True):
            # Anthropic counts the cache apart, so a prompt read wholly from it reports no fresh tokens.
            prompt += (token_count(usage.get("cache_read_input_tokens")) or 0) + (
                token_count(usage.get("cache_creation_input_tokens")) or 0
            )
        completion = token_count(usage.get("completion_tokens"))
        reasoning = token_count(usage.get("reasoning_tokens")) or 0
        usage_present = prompt > 0 and completion is not None
        return cls(
            model=model,
            finish_reason=response.finish_reason or "",
            usage_present=usage_present,
            zero_output=usage_present and (completion or 0) + reasoning == 0,
        )


@dataclass
class RecoveryState:
    """What one turn's recovery has spent so far, as :func:`decide` reads it."""

    rungs_used: set[str]  # the turn's, shared by every attempt of it
    prefills: int = 0
    nudges: int = 0
    resends: int = 0
    continuations: int = 0
    max_tokens_base: int | None = None  # the ceiling the first truncated call carried
    fallbacks_used: int = 0
    refusal_fell_back: bool = False
    empty_streak: list[EmptyAttempt] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)  # what a network-cut answer has streamed so far
    # The output-ceiling continuations the current step has had: what a TRUNCATED failure reports, where
    # ``continuations`` is the turn's budget for every kind.
    ceiling_continuations: int = 0

    @property
    def asked_again(self) -> bool:
        """Whether recovery has asked the model anything again this turn, by any move."""
        return bool(
            self.prefills or self.nudges or self.resends or self.continuations or self.fallbacks_used or self.rungs_used
        )


def _usage(response: LLMResponse) -> dict:
    usage = response.usage
    return usage if isinstance(usage, dict) else {}


def strip_think_blocks(text: str, *, trim: bool = True) -> str:
    """Remove paired think blocks. Debris is left in place for the check below.

    ``trim`` is off for text that is about to be joined to more of the same
    reply: the space at the end of a cut stream, or at the head of its
    continuation, is the space between two words.

    Matched by the same shape as ``_THINK_TAG_RE``: any of the three tag names,
    with or without a vendor namespace prefix. A single spelling here would let
    a complete ``<mm:think>...</mm:think>`` block through to the user in full,
    which is the one outcome stripping exists to prevent.

    The backreference keeps the two ends the same tag -- ``<think>x</thinking>``
    is a malformed pair, and deleting everything between two unrelated tags
    would take real content with it. The prefixes are not tied to each other:
    a backend that stamps one end and not the other still wrote one block.
    """
    stripped = _THINK_BLOCK_RE.sub("", text)
    return stripped.strip() if trim else stripped


def is_only_think_debris(text: str) -> bool:
    """True when the text is nothing but think tags once they are removed.

    The shape a turn cut off inside an inlined reasoning block arrives in: a
    lone closing tag with no opener, which pairs with nothing and so survives
    ``strip_think_blocks`` as an eleven-character string that reads like a real
    answer. Asked of the residue rather than of vendor spellings -- which
    prefix a backend picked is not knowable in advance.
    """
    return bool(text) and not _THINK_TAG_RE.sub("", text).strip()


def has_inline_thinking(content: str | None) -> bool:
    """True when raw content carries a <think>/<thinking>/<reasoning> marker."""
    return bool(content) and bool(_THINK_TAG_RE.search(content))


def has_thinking(response: LLMResponse) -> bool:
    """True when the response produced reasoning in any form (structured or inline)."""
    return bool(response.reasoning_content or response.thinking_blocks or has_inline_thinking(response.content))


def reply_kind(
    response: LLMResponse,
    visible: str | None,
    *,
    origin: Origin | None,
    prev_had_tool_calls: bool,
    streamed: bool,
    reply_awaited: bool = False,
) -> ReplyKind:
    """What a reply that carries no tool call is, by the first of these rules it meets.

    ``visible`` is ``response.content`` once think blocks are out. A refusal is named before text
    because the text is the model's explanation of it, and thinking before a cut because a reply that
    thought and was cut is thinking-only: TRUNCATED is a cut with no reasoning behind it.
    ``reply_awaited`` is a turn whose caller waits on its text, where a marker is text like any other.
    """
    has_text = bool(visible and visible.strip())
    if has_text and not reply_awaited and is_silence(visible, origin):
        return ReplyKind.SILENT
    if response.finish_reason in DECLINED_FINISH_REASONS:
        return ReplyKind.REFUSAL
    if has_text:
        # Only a stream that died carries ``STREAM_CUT``. A clean close without a terminal chunk, and a body
        # that names no finish, read ``unknown`` and are whole answers.
        return ReplyKind.NETWORK_CUT if streamed and response.finish_reason == STREAM_CUT else ReplyKind.TEXT
    if has_thinking(response) or (token_count(_usage(response).get("reasoning_tokens")) or 0) > 0:
        return ReplyKind.THINKING_ONLY
    if response.truncated:
        return ReplyKind.TRUNCATED
    return ReplyKind.POST_TOOL_EMPTY if prev_had_tool_calls else ReplyKind.EMPTY


# The kinds with no text of their own: when a cut answer already streamed, they leave it as the answer.
_TEXTLESS_KINDS = frozenset({ReplyKind.EMPTY, ReplyKind.POST_TOOL_EMPTY, ReplyKind.THINKING_ONLY, ReplyKind.TRUNCATED})


def decide(
    kind: ReplyKind,
    state: RecoveryState,
    limits: RecoveryLimits,
    *,
    prefill_supported: bool,
    thinking_to_refeed: bool,
    next_rung: str | None,
    fallback_left: bool,
    resend_budget: int,
) -> RecoveryAction:
    """The next move for a reply of ``kind``, given what the turn has already spent (``state``).

    The caller supplies what only it can see: whether the provider takes a prefill, whether the
    reply carries thinking a prefill can re-feed (:func:`has_thinking`: a reasoning field, thinking
    blocks or inline tags, not a bare token count), the rung :func:`next_step_down` offers, whether
    a fallback model remains, and :func:`resend_budget`'s answer. A fallback model gets one try: on
    one an empty or thinking-only reply is not resent, prefilled or stepped down again, so the next
    move is another model or the end. A cut answer is still continued there, and a reply right
    after tools still nudged.
    """
    if kind is ReplyKind.SILENT:
        return RecoveryAction.SILENT
    if not limits.enabled:
        # The turn ends as it did before recovery existed; the caller fails it if nothing answered.
        return RecoveryAction.FAIL if kind is ReplyKind.REFUSAL else RecoveryAction.COMPLETE
    if kind is ReplyKind.TEXT or (state.parts and kind in _TEXTLESS_KINDS):
        return RecoveryAction.COMPLETE
    if kind is ReplyKind.REFUSAL:
        return _fallback_or_fail(fallback_left and not state.refusal_fell_back)
    if kind is ReplyKind.NETWORK_CUT:
        return RecoveryAction.CONTINUE if state.continuations < MAX_CONTINUATIONS else RecoveryAction.COMPLETE
    if kind is ReplyKind.TRUNCATED:
        return RecoveryAction.CONTINUE if state.continuations < MAX_CONTINUATIONS else RecoveryAction.FAIL
    on_fallback = state.fallbacks_used > 0
    if kind is ReplyKind.THINKING_ONLY:
        if not on_fallback:
            if prefill_supported and thinking_to_refeed and state.prefills < limits.thinking_prefill_max_retries:
                return RecoveryAction.PREFILL
            if next_rung is not None:
                return RecoveryAction.STEP_DOWN
        return _fallback_or_fail(fallback_left)
    if kind is ReplyKind.POST_TOOL_EMPTY and state.nudges < limits.post_tool_empty_max_nudges:
        return RecoveryAction.NUDGE
    # A plain empty, or a post-tool one whose nudges are spent.
    if not on_fallback and state.resends < resend_budget and not deterministic_empty(state.empty_streak):
        return RecoveryAction.RETRY
    return _fallback_or_fail(fallback_left)


def _fallback_or_fail(fallback_left: bool) -> RecoveryAction:
    return RecoveryAction.FALLBACK if fallback_left else RecoveryAction.FAIL


def deterministic_empty(streak: Sequence[EmptyAttempt]) -> bool:
    """Whether the streak says one more resend would come back empty too.

    Hermes's zero-output guard: two or more replies, each with usage that proves nothing was
    generated, from one model and one finish. Usage that is missing or shows output keeps the full
    budget, so the guard fails open.
    """
    if len(streak) < 2:
        return False
    first = streak[0]
    return all(
        attempt.usage_present
        and attempt.zero_output
        and (attempt.model, attempt.finish_reason) == (first.model, first.finish_reason)
        for attempt in streak
    )


def resend_budget(response: LLMResponse, limits: RecoveryLimits) -> int:
    """How many times an empty reply may be resent: once, when the call that came back empty was dear.

    Every resend bills the whole prompt again for no answer. The cost is the one the call reported
    (``usage["cost_usd"]``): estimating it can fetch the price table on the event loop, and a call
    that reports none keeps the full budget.
    """
    cost = reported_cost(_usage(response).get("cost_usd"))
    if cost is not None and cost >= limits.empty_retry_cost_threshold_usd:
        return min(1, limits.empty_content_max_retries)
    return limits.empty_content_max_retries


# The only rungs a step-down lands on. Never `minimal`, which Codex refuses with HTTP 400.
_STEP_DOWN_RUNGS = ("high", "low")


def next_step_down(
    current: str | None,
    wire_keys: Callable[[str | None], object] | None = None,
    *,
    used: Collection[str] = (),
) -> str | None:
    """The next of ``high`` and ``low`` below ``current``, or None when there is none to ask for.

    ``None`` is an effort the turn never stated, which sits above both. A rung already in ``used``
    is skipped -- each is asked for once a turn -- and so is one ``wire_keys`` says serializes like
    ``current`` (``LLMProvider.reasoning_wire_keys``): the same request would only fail again at full
    price. An effort the ladder does not carry has no rung below it.
    """
    try:
        depth = -1 if current is None else REASONING_EFFORT_LADDER.index(current.strip().lower())
    except (AttributeError, ValueError):
        return None
    here = wire_keys(current) if wire_keys is not None else None
    for rung in _STEP_DOWN_RUNGS:
        if REASONING_EFFORT_LADDER.index(rung) <= depth or rung in used:
            continue
        if wire_keys is not None and wire_keys(rung) == here:
            continue
        return rung
    return None


def empty_retry_delay(attempt: int, *, rand: Callable[[], float] = random.random) -> float:
    """Seconds to wait before the ``attempt``-th (1-based) resend or step-down: Hermes's jittered backoff.

    Each move counts its own: a resend by the turn's resends, a step-down by the rungs it has
    stepped to. The bounds are read when called so a test can shorten them.
    """
    exponent = max(0, attempt - 1)
    # The same guard Hermes has: the cap has long since won, and a huge power overflows the float.
    delay = (
        EMPTY_RETRY_MAX_DELAY_S
        if exponent >= 63
        else min(EMPTY_RETRY_BASE_DELAY_S * 2**exponent, EMPTY_RETRY_MAX_DELAY_S)
    )
    return delay * (1 + 0.5 * rand())


def reasoning_excerpt(response: LLMResponse) -> str:
    """The first ``REASONING_EXCERPT_MAX`` characters of what the model thought, "" if it left no text.

    For a failure that has no answer to show, so the reader can still see where the model got to.
    """
    text = _thinking_text(response)
    return text[:REASONING_EXCERPT_MAX] + "..." if len(text) > REASONING_EXCERPT_MAX else text


def _thinking_text(response: LLMResponse) -> str:
    """The reasoning field, else the thinking blocks, else the text inside inline think tags."""
    if response.reasoning_content and response.reasoning_content.strip():
        return response.reasoning_content.strip()
    # Joined as the Anthropic adapter joins the same blocks into ``reasoning_content``.
    blocks = "".join(
        block["thinking"]
        for block in response.thinking_blocks or ()
        if isinstance(block, dict) and isinstance(block.get("thinking"), str)
    ).strip()
    if blocks:
        return blocks
    inline = (match.group(2).strip() for match in _THINK_BLOCK_RE.finditer(response.content or ""))
    return "\n\n".join(part for part in inline if part)


# A continuation after reasoning cut at the output ceiling picks up mid-thought:
# the model was fed its own truncated reasoning and carries on from the cut,
# so the first fragment of what it now emits as *content* is the tail of that
# thought (measured 2026-09-08: " Lägg in an token. Let me start researching."
# streamed as the head of a research answer). The fragment ends at the first
# sentence boundary. Only a fragment that reads as mid-sentence is dropped --
# it starts with whitespace or a lowercase letter -- so an answer that opens on
# a heading, a list marker or a capitalised sentence is left whole.
# The second class is the CJK full stop, exclamation and question marks. A
# Han character has no case, so a continuation that opens directly on CJK
# never passes the gate above and is left whole -- deliberately: without case
# there is no way to tell a mid-sentence opening from a sentence's first word,
# and losing an answer's first sentence costs more than keeping a fragment.
# The CJK stops are therefore where an admitted fragment ENDS: one that opens
# on whitespace, or on a lowercase Latin word running into CJK text, stops at
# the first CJK mark instead of running on to a Latin period or a newline.
_CUT_BOUNDARY = re.compile(r"\n|[.!?](?=\s|$)|[\u3002\uff01\uff1f]")
_CUT_HEAD_MAX_CHARS = 200


def cut_reasoning_head(text: str | None) -> str | None:
    """``text`` without a leading mid-sentence fragment, when it has one.

    The rule the stream gate and the stored content both apply, so what the
    reader saw and what the session keeps agree.
    """
    if not text or not (text[0].isspace() or text[0].islower()):
        return text
    match = _CUT_BOUNDARY.search(text)
    if match is None:
        return text
    head, rest = text[: match.start()], text[match.end() :]
    if len(head) > _CUT_HEAD_MAX_CHARS or not rest.strip():
        return text
    return rest.lstrip()


class DraftGate:
    """Holds a response's deltas until something has decided to keep it.

    `ContinuationGate` below holds only until a rule can be decided from the
    text. This holds until a caller says so, because the decision is not in the
    text: a hook reading state the response does not carry can send the whole
    thing back (`HookDecision.rollback`), and a rollback pops history the stream
    has already left. A draft the loop discarded and a reader who saw it are not
    the same turn -- on the ACP lane `_TurnCollector` concatenates every chunk,
    so the rejected draft and its replacement arrive as one answer.

    `cut_head` folds in `ContinuationGate`'s job for the iteration that follows a
    length-truncated response. Holding the whole text makes that cut exact rather
    than a rule decided on a prefix, which is why the two do not stack.
    """

    def __init__(self, deliver, *, cut_head: bool = False) -> None:
        self._deliver = deliver
        self._cut_head = cut_head
        self._held = ""
        self._open = False
        self._dropped = False

    async def __call__(self, delta: str) -> None:
        if self._dropped:
            return
        if self._open:
            await self._deliver(delta)
            return
        self._held += delta

    async def release(self) -> None:
        """Deliver what is held, then let the rest through untouched."""
        if self._dropped or self._open:
            return
        self._open = True
        text = cut_reasoning_head(self._held) if self._cut_head else self._held
        self._held = ""
        if text:
            await self._deliver(text)

    def discard(self) -> None:
        """Drop the held draft. Nothing kept it, so nobody reads it."""
        self._held = ""
        self._dropped = True


class ContinuationGate:
    """Streams a continuation's deltas with :func:`cut_reasoning_head` applied.

    Deltas are held until the rule can be decided -- a boundary followed by
    content, or more text than a fragment could be -- then released; ``finish``
    releases whatever is still held once the response has ended.
    """

    def __init__(self, deliver) -> None:
        self._deliver = deliver
        self._held = ""
        self._open = False

    async def __call__(self, delta: str) -> None:
        if self._open:
            await self._deliver(delta)
            return
        self._held += delta
        match = _CUT_BOUNDARY.search(self._held)
        decided = (match is not None and self._held[match.end() :].strip()) or len(self._held) > _CUT_HEAD_MAX_CHARS
        if not decided:
            return
        await self._release()

    async def finish(self) -> None:
        if not self._open:
            await self._release()

    async def _release(self) -> None:
        self._open = True
        text = cut_reasoning_head(self._held) or ""
        self._held = ""
        if text:
            await self._deliver(text)


class SilenceGate:
    """Holds the start of a reply back while it could still turn out to be a silence marker.

    A marker is the model saying nothing, so a surface that streams must never show it and then
    take it back. Deltas are held while the text so far could still resolve to a marker under the
    strict rule, and go out together with everything after them the moment it cannot -- which a
    reply that begins like prose does at its first delta, so an ordinary answer waits for nothing.
    Before its first letter or digit the text is undecided only while it is whitespace and the
    punctuation the strict rule forgives at a marker's edge (the ``*`` of ``*NO_REPLY*``): any other
    character there -- a symbol, an emoji, a box-drawing line -- starts no marker, and the reply goes
    out at once. From the first letter or digit on, it is the strict rule's prefix test
    (``may_become_silence``). Each delta is read on its own against what the ones before it left,
    so a long hold costs a delta no more than a short one. A turn from a proactive origin
    (``hold_all``) is read by the loose rule, whose marker may close a report on its last line,
    and only the whole call can show that, so every delta waits for :meth:`release` or
    :meth:`discard`.

    The loop decides once it has read the reply: ``discard`` for a marker, which goes to no one,
    ``release`` for anything else, and for a reply that ends in tool calls, whose words are not a
    reply a marker could be.
    """

    def __init__(self, deliver, *, hold_all: bool) -> None:
        self._deliver = deliver
        self._hold_all = hold_all
        self._held: list[str] = []
        self._open = False
        self._dropped = False
        # What the strict rule reads of the held text: the text with its edge whitespace off, and the
        # whitespace after it, which joins it only once more text follows. Neither grows past
        # SILENCE_MAX_CHARS, beyond which no text is a marker.
        self._body = ""
        self._gap = ""
        self._worded = False
        self._may_be_marker = True

    async def __call__(self, delta: str) -> None:
        if self._dropped:
            return
        if self._open:
            await self._deliver(delta)
            return
        self._held.append(delta)
        if not self._hold_all and not self._undecided_with(delta):
            await self.release()

    def _undecided_with(self, delta: str) -> bool:
        """Whether the held text, ``delta`` now included, could still resolve to a marker."""
        grew = False
        for char in delta:
            if char.isspace():
                if self._body and len(self._body) + len(self._gap) < SILENCE_MAX_CHARS:
                    self._gap += char
                continue
            if len(self._body) + len(self._gap) >= SILENCE_MAX_CHARS:
                return False
            if char.isalnum():
                self._worded = True
            elif not self._worded and not unicodedata.category(char).startswith("P"):
                return False
            self._body += self._gap + char
            self._gap = ""
            grew = True
        if grew and self._worded:
            self._may_be_marker = may_become_silence(self._body)
        return self._may_be_marker

    async def release(self) -> None:
        """Deliver what is held, then let the rest through untouched."""
        if self._dropped or self._open:
            return
        self._open = True
        text = "".join(self._held)
        self._held = []
        if text:
            await self._deliver(text)

    def discard(self) -> None:
        """Drop what is held and everything after it: the reply was a marker, and a marker is shown to no one."""
        self._held = []
        self._dropped = True
