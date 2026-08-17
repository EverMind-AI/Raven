"""Multi-turn conversation support for DR mode (dr@3.0, product surface only).

Every published DR reading is turn one of a fresh session. The bench harness
sends exactly one message per question, so the second turn of a DR conversation
has never run under measurement - not once across the whole dr@1.0 - dr@3.0
ladder. That is the reason this module exists and also the reason everything in
it is off by default: it is a product feature whose effect size is unknown, and
an unmeasured default-on capability is the shape this repo has paid for most.

Three things happen here.

**1. A turn-level decision about whether the research machine should run.**
``DRFlowConfig.enabled`` decides turn one. From turn two on, a small classifier
decides, because "mode" is the wrong granularity for a conversation: a follow-up
like "expand the third paragraph" or "thanks" would otherwise pay for the verify
gate, the spin breaker, the fetch floor and a research appendix reporting an empty
trail. The classifier is a fresh two-message conversation sharing
nothing with the turn's context, exactly like the verify reviewer, so it cannot
be steered by the history it is judging.

**What the gate deliberately does NOT touch: the system prompt.** The DR identity
and contract stay byte-identical on every turn of a session even when the turn is
answered from context. The system prompt is the head of the cached prefix, so
varying it per turn re-bills the whole conversation at uncached rates - measured
on this stack at 4.3x when a provider slot silently disabled caching. A gate that
saves one verify call and loses the prefix cache is a net loss. So the gate cuts
*behaviour* (observers, iteration budget, answer shaping) and never *text*.

**Fail-open direction is "research".** A classifier that errors, times out or
returns something unparseable yields a research turn. The two failures are not
symmetric: a needless research turn costs latency and quota, a wrongly skipped
one answers a factual question from stale context and states it with the same
confidence. For a research product the second is the one that must not happen by
accident.

**The iteration cap is deliberately not part of what the gate cuts.** It is an
upper bound, not a spend: a turn that needs no tools ends at iteration one whether
the ceiling above it is 6 or 60. Lowering it for a non-research turn would buy
nothing measurable and would add a way for a mis-classified turn to be cut off
mid-answer, which is the one failure this gate must not manufacture.

**2. A research memo carried across turns.** Turn one's evidence does not survive
in usable form: ``_save_turn`` truncates every tool result to 16k chars and the
history trimmer then drops whole messages by priority, so by turn three the pages
the answer rests on are gone while the answer's claims remain. The memo is a
compact, deterministic record of what was searched and opened, rebuilt from the
client-side ledger rather than asked of the model - same source as the process
appendix, same reason: a self-report is unverifiable and free to be wrong in the
direction that flatters the turn.

⚠️ **The memo is a different artifact from the process appendix and must stay
one.** ``process_appendix`` states as part of its contract that the model never
sees it, in this turn or as history in the next one, and that anyone who wants it
in the transcript is proposing a different change with a different risk. This is
that different change, so it is a separate object with a separate budget: sources
only, capped, no integrity numbers, no ``citation_grounding_rate``. Feeding the
appendix back to the model would put a metric the model can read into the context
of the turn that produces the next value of it.

**3. Identity scope.** The web tools reset their "already seen this" sets at every
turn boundary, on the stated reasoning that the same query on a later turn is a
re-check rather than a loop. That is right for a benchmark item and wrong for a
conversation about one topic, where turn two re-searches and re-opens turn one's
pages. ``identity_scope="topic"`` keeps the identity sets across turns of a
session. It is not the default because it has a real failure mode - see
``SearchSaturation.reset``.
"""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from raven.agent.flow.answer_text import visible_answer
from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision

# Wraps the injected memo so ``AgentLoop._save_turn`` can strip it back out. The
# memo is rebuilt from session metadata at every assembly, so persisting it would
# accumulate: turn three would carry turn two's memo as history plus a fresh one
# describing the same pages.
MEMO_OPEN = "[research memo — evidence gathered earlier in this conversation]"
MEMO_CLOSE = "[/research memo]"

# Whether the turn running in this context is a research turn. A ContextVar and
# not an AgentLoop attribute: one loop instance serves every session of a gateway
# and each turn runs in its own asyncio task, so an attribute would let a chat
# follow-up in one conversation switch off the verify gate of a research turn in
# another. Same reasoning as the ledger's per-turn handle and ask_user's
# conversation id, and the same bug class this repo already paid for there.
#
# Default True so that with the feature off - and on turn one, which the gate
# never sees - every consumer behaves exactly as it did before this module.
_RESEARCH_TURN: ContextVar[bool] = ContextVar("dr_research_turn", default=True)


def set_research_turn(value: bool) -> None:
    _RESEARCH_TURN.set(value)


def is_research_turn() -> bool:
    return _RESEARCH_TURN.get()


# The turn's ledger rows, handed from the agent loop (which owns the ledger's
# lifetime and must close it) to the message handler (which owns the session the
# memo is stored on). A ContextVar for the same reason as above - one loop, many
# concurrent turns - and read-once so a turn that produced no rows cannot inherit
# the previous turn's.
_TURN_LEDGER_ROWS: ContextVar[list[dict[str, Any]] | None] = ContextVar("dr_turn_ledger_rows", default=None)


# URLs earlier turns of this conversation opened, handed from assembly (which has
# the session) to the appendix seam (which does not). Set on every turn, including
# to an empty tuple, so a turn cannot inherit the previous one's list - the value
# widens a grounding check's denominator, and a stale one would absolve a citation
# to a page this conversation never read.
_PRIOR_SOURCES: ContextVar[tuple[str, ...]] = ContextVar("dr_prior_sources", default=())


def set_prior_sources(urls: tuple[str, ...]) -> None:
    _PRIOR_SOURCES.set(urls)


def prior_sources() -> tuple[str, ...]:
    return _PRIOR_SOURCES.get()


def stash_turn_rows(rows: list[dict[str, Any]]) -> None:
    _TURN_LEDGER_ROWS.set(rows)


def take_turn_rows() -> list[dict[str, Any]]:
    rows = _TURN_LEDGER_ROWS.get()
    _TURN_LEDGER_ROWS.set(None)
    return rows or []


_GATE_SYSTEM = """You decide whether a follow-up message in an ongoing research \
conversation needs a new round of web research, or can be answered from what is \
already in the conversation.

Answer with JSON only: {"research": true|false, "why": "<one short clause>"}

Choose research=true when the message asks for any fact, figure, source, date, \
name or development that the conversation does not already contain - including \
when it asks to re-check or confirm something, or asks about anything after the \
period the existing evidence covers.

Choose research=false when the message only asks to reformat, shorten, expand, \
translate, explain or summarise material already present, asks about the \
assistant's own reasoning, or is conversational (thanks, acknowledgement, a \
correction of tone or scope).

When the two readings are close, choose research=true."""


@dataclass
class TurnMode:
    """Whether this turn runs the research machine, and on whose authority.

    ``source`` is recorded rather than inferred because a turn that skipped
    research because the classifier said so and a turn that skipped it because
    the classifier was unreachable look identical in the output - the same
    "a zero cannot say which of its causes produced it" shape that made
    ``dedup_skipped`` unreadable for two versions. ``gate_error`` and
    ``gate_unparsed`` therefore never collapse into ``gate``.
    """

    research: bool
    source: str
    why: str = ""

    def counters(self) -> dict[str, Any]:
        return {"dr_turn_research": self.research, "dr_turn_source": self.source, "dr_turn_why": self.why}


class ConversationGate:
    """Decides, per turn, whether a DR conversation's turn needs research."""

    def __init__(
        self,
        provider: Any,
        *,
        model: str | None = None,
        max_tokens: int = 256,
        timeout_seconds: float = 20.0,
        history_messages: int = 6,
        history_chars: int = 4000,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_tokens = max_tokens
        self._timeout_seconds = timeout_seconds
        self._history_messages = history_messages
        self._history_chars = history_chars

    async def decide(self, question: str, history: list[dict[str, Any]]) -> TurnMode:
        """Classify one follow-up. Never raises; every failure is a research turn."""
        user = self._render(question, history)
        try:
            response = await asyncio.wait_for(
                self._provider.chat_with_retry(
                    messages=[
                        {"role": "system", "content": _GATE_SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    model=self._model,
                    max_tokens=self._max_tokens,
                    temperature=0.0,
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("conversation-gate: timed out after {}s; running research", self._timeout_seconds)
            return TurnMode(True, "gate_error", "timeout")
        except Exception as exc:  # noqa: BLE001 - any provider failure is a research turn
            logger.warning("conversation-gate: call failed ({}: {}); running research", type(exc).__name__, exc)
            return TurnMode(True, "gate_error", type(exc).__name__)
        # A truncated generation is not a verdict. Read as-is it would usually
        # parse to nothing and fail open anyway, but a reasoning model that spends
        # the budget thinking and emits a bare `{"research": false` fragment is
        # exactly the case json_repair would happily complete in the unsafe
        # direction.
        if getattr(response, "finish_reason", "") in ("length", "error"):
            logger.warning("conversation-gate: generation did not complete; running research")
            return TurnMode(True, "gate_error", str(getattr(response, "finish_reason", "")))
        text = visible_answer(getattr(response, "content", None) or "")
        verdict = self._parse(text)
        if verdict is None:
            logger.warning("conversation-gate: unparseable verdict; running research")
            return TurnMode(True, "gate_unparsed", "")
        research, why = verdict
        return TurnMode(research, "gate", why)

    def _render(self, question: str, history: list[dict[str, Any]]) -> str:
        """Build the classifier's only input.

        Assistant and user text only. Tool calls and tool results are dropped on
        purpose: they are most of the volume, they are the part the trimmer will
        have mangled, and "how much searching happened last turn" is not evidence
        about whether *this* message needs searching - a turn that searched
        heavily is if anything more likely to be followed by a formatting request.
        """
        lines: list[str] = []
        for m in history[-self._history_messages :]:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            content = m.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            lines.append(f"{role}: {content.strip()[: self._history_chars]}")
        prior = "\n\n".join(lines) or "(no prior text)"
        return f"Conversation so far:\n{prior}\n\nNew message:\n{question.strip()}"

    @staticmethod
    def _parse(text: str) -> tuple[bool, str] | None:
        if not text:
            return None
        candidates = [text]
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            candidates.append(text[start : end + 1])
        for candidate in candidates:
            try:
                import json_repair

                data = json_repair.loads(candidate)
            except Exception:
                try:
                    data = json.loads(candidate)
                except Exception:
                    continue
            if not isinstance(data, dict):
                continue
            value = data.get("research")
            if isinstance(value, str) and value.strip().lower() in ("true", "false"):
                value = value.strip().lower() == "true"
            if isinstance(value, bool):
                why = data.get("why")
                return value, str(why)[:200] if isinstance(why, str) else ""
        return None


class GatedHook(AgentHook):
    """Forwards to a DR observer only on turns the gate called research turns.

    A wrapper rather than a check inside each gate, for two reasons. The DR
    observers are the measured surface - every one of them carries a version
    label describing a distribution - and adding a branch inside them would put
    product-surface logic on the path the benchmark arms run. And a wrapper makes
    the off-state provable in one place: with ``predicate`` returning True the
    chain is the same object graph it was before this module existed.
    """

    def __init__(self, inner: AgentHook, predicate) -> None:
        self._inner = inner
        self._predicate = predicate

    @property
    def name(self) -> str:
        return f"Gated({self._inner.name})"

    @property
    def inner(self) -> AgentHook:
        return self._inner

    async def _maybe(self, phase: str, ctx: AgentHookContext) -> HookDecision:
        if not self._predicate():
            return HookDecision()
        return await getattr(self._inner, phase)(ctx)

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        return await self._maybe("before_user_inbound", ctx)

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        return await self._maybe("before_iteration", ctx)

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        return await self._maybe("before_execute_tools", ctx)

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        return await self._maybe("after_iteration", ctx)

    async def terminal_answerless(self, ctx: AgentHookContext) -> HookDecision:
        return await self._maybe("terminal_answerless", ctx)

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        return await self._maybe("after_send", ctx)


# --------------------------------------------------------------------------- #
# Research memo                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class ResearchMemo:
    """Sources and queries accumulated over a conversation's research turns.

    Stored structured rather than pre-rendered so it can be re-capped when the
    limits change and de-duplicated when a later turn re-opens a page. A stored
    string would freeze both decisions at the moment it was first written.
    """

    turns: int = 0
    queries: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    """Every URL this conversation has read, for the grounding check only.

    Separate from ``sources`` because the two are bounded for opposite reasons and a
    single list is therefore wrong for one of them. ``sources`` is rendered into the
    next turn's prompt, so its cap buys context; this list is never rendered, so its
    only cost is bytes in the session file, and what it needs is completeness.

    Measured on the first eight-turn run: turn one alone opened 18 pages against a
    12-source cap for the whole conversation, so most of turn one's reading had been
    evicted by turn five - and a later turn citing one of those pages was reported as
    a fabricated citation. Sharing one cap between "what the model is shown" and
    "what the check will accept" is the knob-serving-two-opposed-goals shape: it has
    to be wrong on one of them, and it was wrong on the one that accuses the answer.
    """

    @classmethod
    def from_metadata(cls, data: Any) -> "ResearchMemo":
        if not isinstance(data, dict):
            return cls()
        queries = [str(q) for q in data.get("queries", []) if isinstance(q, str)]
        sources = [s for s in data.get("sources", []) if isinstance(s, dict) and s.get("url")]
        opened = [str(u) for u in data.get("opened", []) if u]
        # A conversation that started before ``opened`` existed still has its rendered
        # sources on disk. Seeding from them keeps the grounding check working across
        # the upgrade instead of accusing the next answer of inventing pages it read
        # last turn - the failure this field exists to remove.
        if not opened:
            opened = [str(s["url"]) for s in sources]
        turns = data.get("turns")
        return cls(
            turns=turns if isinstance(turns, int) else 0,
            queries=queries,
            sources=sources,
            opened=opened,
        )

    def to_metadata(self) -> dict[str, Any]:
        return {
            "turns": self.turns,
            "queries": self.queries,
            "sources": self.sources,
            "opened": self.opened,
        }

    @property
    def empty(self) -> bool:
        return not self.sources and not self.queries

    def merge_ledger(
        self,
        rows: list[dict[str, Any]],
        *,
        max_sources: int,
        max_queries: int,
        max_opened: int = 200,
    ) -> "ResearchMemo":
        """Fold one turn's ledger rows in, newest-first, de-duplicated by URL.

        Only successful fetches become sources. A failed fetch is not evidence,
        and listing it invites the next turn to cite a page nobody read - the
        fabricated-citation channel the appendix exists to detect, handed a head
        start.
        """
        self.turns += 1
        seen = {s.get("url") for s in self.sources}
        fresh: list[dict[str, Any]] = []
        for row in rows:
            if row.get("op") != "fetch" or not row.get("ok"):
                continue
            url = str(row.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            fresh.append({"url": url, "chars": int(row.get("chars") or 0), "turn": self.turns})
        # Newest turn first: when the cap bites, the pages this turn opened are
        # the ones the next message is most likely to be about.
        self.sources = (fresh + self.sources)[:max_sources]
        # The check's set keeps everything the render cap evicts. Its own bound is
        # far looser because a URL is ~60 bytes and nothing here reaches the model;
        # it exists only so a very long conversation cannot grow the session file
        # without limit.
        known_opened = set(self.opened)
        newly_opened = [s["url"] for s in fresh if s["url"] not in known_opened]
        self.opened = (newly_opened + self.opened)[:max_opened]

        known = {" ".join(q.lower().split()) for q in self.queries}
        new_queries: list[str] = []
        for row in rows:
            if row.get("op") != "search":
                continue
            q = str(row.get("query") or "").strip()
            key = " ".join(q.lower().split())
            if not key or key in known:
                continue
            known.add(key)
            new_queries.append(q)
        self.queries = (new_queries + self.queries)[:max_queries]
        return self

    def render(self, *, max_chars: int) -> str:
        """The block prepended to the next turn's user message, or ``""``.

        Bounded by construction and then by ``max_chars``, because this text is
        billed on every remaining turn of the conversation: unlike the evidence
        it replaces it never falls out of history on its own.
        """
        if self.empty:
            return ""
        lines = [MEMO_OPEN]
        if self.sources:
            lines.append("Pages already opened (do not re-open unless the content is stale):")
            lines += [f"- {s['url']}" for s in self.sources]
        if self.queries:
            lines.append("Searches already run: " + "; ".join(self.queries))
        lines.append(
            "This is a record of retrieval, not of findings - the findings are in the "
            "replies above. Trust it for what was looked at, not for what was concluded."
        )
        lines.append(MEMO_CLOSE)
        block = "\n".join(lines)
        if len(block) <= max_chars:
            return block
        # Truncate whole source lines rather than mid-line: half a URL reads as a
        # real one and is exactly the input that makes a fabricated citation.
        keep: list[str] = []
        budget = max_chars - len(MEMO_OPEN) - len(MEMO_CLOSE) - 2
        for line in lines[1:-1]:
            if budget - len(line) - 1 < 0:
                break
            keep.append(line)
            budget -= len(line) + 1
        return "\n".join([MEMO_OPEN, *keep, MEMO_CLOSE])


def strip_memo(content: str) -> str:
    """Remove an injected memo block from a message before it is persisted.

    Exact-delimiter based: the alternative, matching the opening line and cutting
    at the next blank line, silently keeps half a memo when a URL list contains a
    blank line, and half a memo is worse than either whole outcome.
    """
    if not content.startswith(MEMO_OPEN):
        return content
    end = content.find(MEMO_CLOSE)
    if end < 0:
        return content
    return content[end + len(MEMO_CLOSE) :].lstrip("\n")


__all__ = [
    "MEMO_CLOSE",
    "MEMO_OPEN",
    "ConversationGate",
    "GatedHook",
    "ResearchMemo",
    "TurnMode",
    "is_research_turn",
    "prior_sources",
    "set_prior_sources",
    "set_research_turn",
    "stash_turn_rows",
    "strip_memo",
    "take_turn_rows",
]
