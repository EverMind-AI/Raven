"""Empty-response recovery for the agent loop.

Pure decision logic, no I/O — the loop owns the side effects (appending
messages, incrementing counters, ``continue``). Kept separate from the loop so
the branching can be unit-tested in isolation, mirroring the repo's other small
policy units (nudge_policy, decision_router).

A turn that ends with no visible text would otherwise surface a canned
"no response to give" dud — a zero-score turn on weaker models. This recovers
the turn before giving up, in three bounded modes:

  PREFILL  thinking-only — the model emitted only reasoning (a structured field
           or an inline <think> block) and no body. Re-feed its own reasoning so
           it continues into the answer.
  NUDGE    post-tool empty — the model ran a tool then returned nothing. Inject a
           short user nudge so it processes the tool result.
  RETRY    plain empty — re-request with less reasoning asked for than the
           call that came back empty (:func:`lower_reasoning_effort`), and less
           on the wire rather than only in the label. Re-sent as-is it was the
           same bytes, so three retries were three guaranteed repeats of one
           failure.

A provider that refuses a trailing assistant message (its
``supports_assistant_prefill`` answers False: Anthropic with thinking on) never
gets PREFILL. The same thinking-only turn takes NUDGE after a tool, else RETRY,
so the request handed back never ends on an assistant message.

Spending every budget without a word coming back is FAIL, and the loop ends the
turn as an error. It is not a fourth mode -- there is nothing left to try -- it
is the answer to the question the caller asks next, which is whether this turn
produced anything.

This is distinct from Sentinel's NudgeInjector / NudgePolicy, which inject
*proactive suggestions* onto an outbound reply; this module instead recovers an
empty turn before it is ever sent.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

from raven.contracts.llm_provider import LLMResponse

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
    rf"<{_NS}(think|thinking|reasoning)>[\s\S]*?</{_NS}\1>",
    re.IGNORECASE,
)

#: Reasoning efforts this repo sends, strongest first. The set the Anthropic
#: transport can map a thinking budget for, and the set OpenRouter accepts in
#: its native reasoning object, so a rung taken from here is a value every
#: backend the loop talks to already understands.
REASONING_EFFORT_LADDER: tuple[str, ...] = ("max", "xhigh", "high", "medium", "low", "minimal")


def lower_reasoning_effort(
    current: str | None,
    wire_keys: Callable[[str | None], object] | None = None,
) -> str | None:
    """One rung less reasoning than ``current``, or None when there is none left.

    The plain empty retry's way of asking a different question. An empty body
    with reasoning behind it is a call whose thinking spent the output ceiling
    before the answer began -- measured, five in a row at exactly the ceiling --
    so the request that has to change is the one deciding how much of that
    ceiling thinking may take.

    ``None`` in means the turn named no effort and the backend applied its own,
    which is the case the log showed: not a low request, an unstated one. It
    descends to the lowest rung this repo documents in config rather than to the
    floor, so the first retry is a stated request for little thinking and the
    next one can still differ from it.

    An effort this ladder does not carry returns None: a value a caller invented
    is not one we can reason about a rung below.

    ``wire_keys`` is the provider's answer to "what would a request for this
    effort carry" (``LLMProvider.reasoning_wire_keys``), and a rung whose answer
    equals ``current``'s is skipped rather than sent: a request that has not
    changed gets the answer that has not changed. It is a rung the label hides
    -- on the Anthropic Messages wire ``minimal`` and ``low`` both serialize to
    ``effort: low``, so descending by label alone bought a second retry that was
    the first failure again at full price. Skipping is not descending twice:
    when every rung below collapses onto this one there is nothing left to
    change and this returns None, which the caller reads as the same dead end as
    a spent budget. Without ``wire_keys`` the labels stand on their own, which is
    right for every wire that sends the effort as the caller named it.
    """
    candidate = _next_rung(current)
    if candidate is None or wire_keys is None:
        return candidate
    here = wire_keys(current)
    while candidate is not None and wire_keys(candidate) == here:
        candidate = _next_rung(candidate)
    return candidate


def _next_rung(current: str | None) -> str | None:
    """The next label down, before any question about what it sends."""
    if current is None:
        return "low"
    try:
        index = REASONING_EFFORT_LADDER.index(current.strip().lower())
    except (AttributeError, ValueError):
        return None
    if index + 1 >= len(REASONING_EFFORT_LADDER):
        return None
    return REASONING_EFFORT_LADDER[index + 1]


POST_TOOL_NUDGE = (
    "You executed tool calls but returned an empty response. Use the tool "
    "results above to continue the task, or give your final answer now."
)

# A turn that ended empty at the output ceiling: no visible text, no tool call,
# nothing kept. The effort descent above changes what the next request asks
# *for*; this is the only thing that tells the model what happened, and the only
# place a payload too large to finish is named -- no rung of the ladder can
# shrink a write. `Tool.truncation_hint` says the same thing better but rides a
# refused tool call, and a turn cut before any call produced none.
#
# Every sentence here is held to what the trigger proves, which is only that an
# empty turn hit the ceiling. It does not say the reasoning spent the budget:
# true for a reasoning model and false for one whose answer simply ran long, and
# the trigger cannot tell them apart. It does not name a tool call as the owed
# reply either -- a plain question arrives here too. Both were review findings,
# in that order; a third would be the same mistake again.
OUTPUT_LIMIT_NUDGE = (
    "Your previous turn was cut off at the output token limit before it produced "
    "any reply or tool call, so nothing from it was kept. Keep this turn short: "
    "answer briefly, or make the tool call the task needs. If a payload you were "
    "writing is too large to finish in one call, send it across several smaller "
    "calls instead of one."
)


class RecoveryAction(Enum):
    """What the loop should do about an empty assistant response."""

    COMPLETE = auto()  # visible text present, or recovery off → finish the turn
    PREFILL = auto()  # thinking-only → re-feed reasoning, re-request
    NUDGE = auto()  # post-tool empty → inject (empty) + user nudge, re-request
    RETRY = auto()  # plain empty → re-request as-is
    FAIL = auto()  # budgets spent with nothing to show → end the turn as an error


@dataclass(frozen=True)
class RecoveryLimits:
    """Per-turn retry budgets."""

    enabled: bool = True
    post_tool_empty_max_nudges: int = 1
    thinking_prefill_max_retries: int = 2
    empty_content_max_retries: int = 3
    #: How long the loop waits, and how many times, when a model call comes back as
    #: a retryable error after the provider's own ladder gave up. The provider's
    #: ladder is seconds long -- right for a dropped connection, and too short for
    #: a gateway that serves error pages for a few minutes. One measured run had 62
    #: minutes and 23.8M tokens of work behind it when a 40-second outage ended it.
    llm_error_retry_delays: tuple[float, ...] = (15.0, 30.0, 60.0)
    #: Whether a streamed call that failed after it had already produced output is
    #: asked again. Off, the turn fails (N-TURNFAILED): a person watching the stream
    #: has seen the words and would see them twice. On, for an unattended agent whose
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
        return (15.0, 30.0, 60.0)
    return tuple(float(delay) for delay in configured)


def strip_think_blocks(text: str) -> str:
    """Remove paired think blocks. Debris is left in place for the check below.

    Matched by the same shape as ``_THINK_TAG_RE``: any of the three tag names,
    with or without a vendor namespace prefix. A single spelling here would let
    a complete ``<mm:think>...</mm:think>`` block through to the user in full,
    which is the one outcome stripping exists to prevent.

    The backreference keeps the two ends the same tag -- ``<think>x</thinking>``
    is a malformed pair, and deleting everything between two unrelated tags
    would take real content with it. The prefixes are not tied to each other:
    a backend that stamps one end and not the other still wrote one block.
    """
    return _THINK_BLOCK_RE.sub("", text).strip()


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


def classify_empty_response(
    response: LLMResponse,
    visible: str | None,
    *,
    prev_had_tool_calls: bool,
    nudges_done: int,
    prefill_retries: int,
    empty_retries: int,
    limits: RecoveryLimits,
    prefill_supported: bool = True,
) -> RecoveryAction:
    """Decide how to handle a no-tool-call assistant response.

    ``visible`` is ``response.content`` after stripping <think> blocks — i.e. the
    user-facing text. Non-empty ``visible`` (or recovery disabled) means the turn
    is done.

    Ordering puts PREFILL before NUDGE so a thinking-only response is continued
    via prefill rather than spending the post-tool nudge on it; the
    ``not thinking`` guard on NUDGE keeps them mutually exclusive.

    Spending the budgets without ever getting a word back returns FAIL, not
    COMPLETE. The two used to be the same answer, which is how a measured deck
    run reported ``end_turn`` for a turn that never produced a token: five calls
    of about 16.8 MB each came back HTTP 200 with empty content and usage all
    zeros, the ladder ran itself out, and the turn was filed as a completion
    holding a canned line. Nothing downstream could then tell "the model
    finished" from "the model never answered". COMPLETE keeps the two cases that
    really are ends: a response with text in it, and a turn whose recovery is
    switched off -- where no budget was spent and there is nothing to have
    exhausted.
    """
    if visible or not limits.enabled:
        return RecoveryAction.COMPLETE

    thinking = has_thinking(response)

    # The provider refuses a trailing assistant message (Anthropic with
    # thinking on): PREFILL would build a request the vendor rejects, and behind
    # a gateway that rejection can arrive as a stream that never yields a byte,
    # so the turn hangs to the wall-clock cap instead of failing. Decided before
    # PREFILL, with only the actions that leave a user or tool message last:
    # the post-tool nudge (its synthetic assistant sits before the nudge), then
    # the plain retry budget, then give up.
    if thinking and not prefill_supported:
        if prev_had_tool_calls and nudges_done < limits.post_tool_empty_max_nudges:
            return RecoveryAction.NUDGE
        if empty_retries < limits.empty_content_max_retries:
            return RecoveryAction.RETRY
        return RecoveryAction.FAIL

    # thinking-only prefill — the model reasoned but produced no body.
    if thinking and prefill_retries < limits.thinking_prefill_max_retries:
        return RecoveryAction.PREFILL

    # post-tool empty nudge — exclude thinking-only (handled above).
    if prev_had_tool_calls and not thinking and nudges_done < limits.post_tool_empty_max_nudges:
        return RecoveryAction.NUDGE

    # Fallback plain retry. The ``prefill_exhausted`` clause is load-bearing:
    # some models (e.g. mimo-v2-pro via OpenRouter) always populate a reasoning
    # field, so gating retry on ``not thinking`` alone would permanently block
    # retries for every reasoning model once prefill is spent.
    prefill_exhausted = thinking and prefill_retries >= limits.thinking_prefill_max_retries
    if empty_retries < limits.empty_content_max_retries and (not thinking or prefill_exhausted):
        return RecoveryAction.RETRY

    return RecoveryAction.FAIL


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
