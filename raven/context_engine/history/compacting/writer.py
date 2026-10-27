"""The checkpoint writer -- one summarising call, sent as a prefix.

The request this builds is the point of the whole design. It is the messages
the provider was sent last turn, byte for byte, up to and including the last
message being removed, with one instruction appended::

    [system]  [.. the conversation, exactly as it was already sent ..]  [instruction]

Nothing before the instruction is new, which makes the request eligible to be
served from a provider's prefix cache. Eligible is as far as the construction
goes: whether a read happened is the provider's answer, recorded on the
checkpoint as ``cache_status`` and never inferred from the shape of what was
sent. No provider measured so far reports a cache read on a plain completion,
so the honest reading of a reused prefix today is "cache-eligible, unobserved",
and no cost argument rests on it.

The summary is written by the turn's own model rather than by a cheap one
because that is what makes the byte-exact prefix possible at all -- a second
model has no entry to match. Whether it is also the cheaper choice is a
question ``summary_input_tokens`` / ``summary_output_tokens`` and a real
cache-reporting provider have to answer together; the fields are there so the
answer can be measured rather than assumed.

Four things void the reuse, and each has an answer here rather than a hope:

- **a different provider** -- the request goes out on the turn's own provider,
  and there is no setting that points it elsewhere;
- **a different model** -- ``summary.model`` defaults to empty, meaning the
  turn's model; setting it logs that reuse is off and pins
  ``byte_prefix_reused`` false. A record taken under another model is not
  reused either;
- **a discontiguous span** -- COMPACT always runs from the head to the tail, so
  what is removed is a contiguous prefix of what was sent;
- **a changed system prompt** -- sidestepped rather than detected: the recorded
  request's own system message is what gets reused, so the bytes still match
  the entry the provider cached even on a turn whose system prompt has moved.

Reuse is measured, not assumed. Every message is taken from the record at the
same index or built fresh, and ``byte_prefix_reused`` is true only when every one of
them came from the record -- which makes the request a positional prefix of it
by construction.

When the call cannot be made or cannot be read -- no provider, a timeout, a
model that answers with a tool call, a reply cut off at its output ceiling, a
reply the transport classified as an error, a reply that generated no tokens, a
reply that never reached all eight headings -- the turn does not fail and does
not silently lose the messages. A failure a shorter request could fix gets one
retry over the newest half of the span (see :class:`SummaryFailure`); every
other failure, and a retry that fails too, falls back to a fold: the same
checkpoint shape, written by arithmetic, saying how much was removed and where
to ask for it. The fold is deterministic and still carries a ref back to what
was removed.

The writer stores nothing. It returns the checkpoint, and the caller -- which
also knows which ordinals the checkpoint ends up retiring -- persists it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable, Literal, Sequence

from loguru import logger

from raven.context_engine.history.compacting.checkpoint import (
    EMPTY_SECTION,
    SECTIONS,
    CompactionCheckpoint,
    SummaryEntry,
    fold_checkpoint,
    ground_refs,
    merge_refs,
    new_checkpoint_id,
    parse_sections,
    section_template,
    seen_headings,
    timestamp,
)
from raven.context_engine.history.compacting.ledger import MessageLedger
from raven.utils.refs import SourceRef
from raven.utils.tokens import estimate_message_tokens, estimate_prompt_tokens

if TYPE_CHECKING:
    from raven.config.raven import ContextSummaryConfig
    from raven.contracts.llm_provider import LLMProvider

MessageKey = tuple[str, Any]
"""What a message in an assembled request is: ``("system", "")``,
``("session", <ordinal>)``, ``("checkpoint", <id>)`` or ``("user", "")``.

The pairing is what makes reuse checkable. Two requests agree on a prefix when
their keys agree position by position, and a key names the thing a message came
from rather than the bytes it happens to hold, so a body the pruner rewrote is
still recognisably the same message.
"""

SYSTEM_KEY: MessageKey = ("system", "")
USER_KEY: MessageKey = ("user", "")


def session_message_key(ordinal: int) -> MessageKey:
    return ("session", ordinal)


def checkpoint_key(checkpoint_id: str) -> MessageKey:
    return ("checkpoint", checkpoint_id)


COMPACTION_INSTRUCTION = f"""You are writing a context checkpoint for this same conversation, which will
replace the messages above it. The next turn of this conversation is the only
reader.

Output exactly the structure below, with no preamble: every section, in this
order, headings verbatim. Under each heading, replace the bracketed line with
terse bullets of your own. Write "{EMPTY_SECTION}" for a section with nothing to
report -- never drop a section.

{section_template()}

Rules:
1. Every bullet is about this conversation and nothing else. Never answer a
   heading with the text of this request, and do not write about why the
   conversation looks the way it does or what the user may be trying to
   establish.
2. Quote the user's own words verbatim under ## User Constraints, including any
   correction they made. Never paraphrase a constraint.
3. A message above may already be a checkpoint, wrapped in
   <compacted-summary generation="N"> or <compacted-fold generation="N">. It is
   your own earlier output covering an older span of this conversation. Do not
   copy it forward verbatim, and do not append to it: fold it into the eight
   sections above together with everything after it. Keep what is still true,
   drop what has been superseded, and state the current situation once.
4. Preserve every [[ref ...]] tag you see, attached to the same fact it was
   attached to. These tags are how the next turn reaches the original text
   through the context_retrieve tool; a fact that loses its tag becomes
   unverifiable. Never write one you have not seen above: a tag you composed
   reaches nothing.
5. Write engineering prose. Keep exact paths, commands, error strings,
   identifiers, numbers, and signatures.
6. Do not mention this request, and do not mention that the conversation was
   compacted.
7. Do not call any tool. Reply with the eight sections only."""
"""The instruction, and the only new bytes in the request.

Two things in it are load-bearing rather than boilerplate.

**The bracketed line under each heading.** A heading alone is answered from
whatever the surrounding text makes it mean, and what surrounds these is a
numbered list of rules: a bare ``## Constraints`` came back holding this
instruction's own rules, verbatim, and headings with no stated purpose came
back holding commentary about the request. Saying what belongs under a heading
at the point where it is answered is what the harness with the strictest
structure guarantee does, and it costs one line each.

**Rule 7.** Prefix reuse requires the request to carry the conversation's own
tool definitions, so the model has tools in hand while being asked for prose --
a documented failure mode whose symptom is a compaction block with null
content. Carrying the definitions is the price of the cache, so saying "do not
call one" is the price of carrying them.
"""

PARTIAL_REPLAY_NOTE = """

One more thing about what you can see: the conversation above does not start at
its beginning. {dropped} earlier message(s) were left out to keep this request
answerable, and you have no way to read them. Write the eight sections about
what is above and say nothing about what came before it -- in particular, make
no claim that a list is complete when its earlier members may be among the
messages you were not shown."""
"""Appended when the request had to be shortened, and only then.

Without it the model answers ## Goal for a conversation whose opening it never
saw, and answers it confidently: the observed failure of a summary is not that
it says too little but that it asserts completeness over a set it only partly
read. Saying which part is missing is the one thing that turns that from a
wrong answer into a narrower one.
"""

_SUMMARY_TEMPERATURE = 0.1


class SummaryFailure(StrEnum):
    """Why a summarising call produced no checkpoint.

    Split by one question: could a shorter request have done better? The
    first seven are size-shaped -- the request is large by construction, and
    each is a shape a model or a route takes when it is past what it can hold
    -- so they earn one retry over the newest half of the span. The next
    three are not: a credential, a choice to call a tool, and an output
    ceiling all meet a shorter request exactly as they met the long one.
    ``NO_PROVIDER`` is no call at all, and folds without asking anyone.
    """

    TIMEOUT = "timeout"
    REFUSED = "refused"
    TRANSPORT_ERROR = "transport_error"
    NO_TOKENS = "no_tokens"
    NO_SECTIONS = "no_sections"
    MISSING_SECTIONS = "missing_sections"
    EMPTY_SECTIONS = "empty_sections"
    CALL_FAILED = "call_failed"
    TOOL_CALL = "tool_call"
    CUT_OFF = "cut_off"
    NO_PROVIDER = "no_provider"

    @property
    def retryable(self) -> bool:
        return self in _RETRYABLE


_RETRYABLE = frozenset(
    {
        SummaryFailure.TIMEOUT,
        SummaryFailure.REFUSED,
        SummaryFailure.TRANSPORT_ERROR,
        SummaryFailure.NO_TOKENS,
        SummaryFailure.NO_SECTIONS,
        SummaryFailure.MISSING_SECTIONS,
        SummaryFailure.EMPTY_SECTIONS,
    }
)


@dataclass(frozen=True)
class RequestRecord:
    """The exact message list a session's last assembled turn went out as.

    Held so the next compaction can be built out of it rather than out of a
    freshly assembled equivalent. "Equivalent" is not good enough: the pruner
    rewrites tool-result bodies on the turn it fires, so a re-derived message
    can differ byte for byte from the one the provider cached, and the cache
    matches on bytes.

    ``keys`` is empty when the sent list did not line up with the builder's own
    view of it -- the assembler may merge two adjacent assistant messages on
    the way out, and in-turn window management may rewrite a body -- in which
    case nothing is reused rather than something being reused wrongly.

    ``model`` is what the request went out under. A cache entry belongs to one
    model, so a record from another one is a prefix of nothing.

    ``tools`` is the tool list it carried, ``None`` when the sender did not
    say. Tools head a breakpoint-style cache key, and the list a turn sends is
    not the registry's: tool search drops ``tool_search`` itself at small
    catalogues, so a summary sent with the registry's list reads nothing back.
    """

    messages: tuple[dict[str, Any], ...] = ()
    keys: tuple[MessageKey, ...] = ()
    model: str = ""
    tools: tuple[dict[str, Any], ...] | None = None

    @property
    def usable(self) -> bool:
        return len(self.keys) == len(self.messages) and bool(self.messages)


@dataclass(frozen=True)
class ReusedPrefix:
    """The leading messages of a compaction request, and where they came from."""

    messages: tuple[dict[str, Any], ...]
    reused: int
    """How many were taken from the recorded request, position by position."""

    @property
    def whole(self) -> bool:
        return bool(self.messages) and self.reused == len(self.messages)


@dataclass(frozen=True)
class CallCost:
    """What the summarising call cost and what the provider said about caching.

    Taken from provider usage rather than from the local message shape, which
    can only say what was *eligible* for a cache. Input and output tokens
    accumulate over a retry, because both attempts were billed; the cache
    fields describe the attempt that produced the answer, since that is the one
    the checkpoint is a record of.
    """

    status: Literal["hit", "miss", "unobservable"] = "unobservable"
    read_tokens: int = 0
    write_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def of(cls, response: Any) -> "CallCost":
        usage = getattr(response, "usage", None) or {}
        read = usage.get("cache_read_input_tokens")
        return cls(
            status="unobservable" if read is None else ("hit" if int(read) > 0 else "miss"),
            read_tokens=int(read or 0),
            write_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
        )

    def after(self, earlier: "CallCost") -> "CallCost":
        """This attempt's cache facts, billed on top of an earlier attempt."""
        return replace(
            self,
            input_tokens=self.input_tokens + earlier.input_tokens,
            output_tokens=self.output_tokens + earlier.output_tokens,
        )


def reuse_prefix(
    keys: Sequence[MessageKey],
    fresh: Sequence[dict[str, Any]],
    record: "RequestRecord | None",
) -> ReusedPrefix:
    """Take the recorded bytes wherever the recorded request agrees on position.

    Positional rather than by lookup, because a positional match is exactly the
    property a cache needs: when every message comes from the record at its own
    index, the result *is* a prefix of the record, and no further check is
    needed to say so.
    """
    usable = record if record is not None and record.usable else None
    out: list[dict[str, Any]] = []
    reused = 0
    for index, (key, message) in enumerate(zip(keys, fresh)):
        if usable is not None and index < len(usable.keys) and usable.keys[index] == key:
            out.append(usable.messages[index])
            reused += 1
        else:
            out.append(message)
    return ReusedPrefix(messages=tuple(out), reused=reused)


def prefix_tokens(prefix: ReusedPrefix, tool_defs: Sequence[dict[str, Any]]) -> int:
    """What the summarising request costs, by the ledger's own estimator."""
    return estimate_prompt_tokens(list(prefix.messages), list(tool_defs) or None)


def shrink_prefix(prefix: ReusedPrefix, *, budget_tokens: int) -> tuple[ReusedPrefix, int]:
    """Drop the oldest replayed messages until the request fits ``budget_tokens``.

    Returns the shortened prefix and how many messages left it. Two things are
    never dropped: the leading system message, and half of a tool exchange --
    the cut lands where no call is left open, so what the model reads is still
    a conversation, only one that starts later than the real one.

    A shortened request is no longer a byte prefix of the recorded one, so the
    reuse count comes back zero. That is the trade this makes: the cache buys
    nothing on a request the model answers with nothing.

    Returns the prefix unchanged, and zero, when it already fits or when no
    legal cut makes it smaller. The caller must treat that as "cannot shrink"
    rather than retrying the same request.
    """
    messages = list(prefix.messages)
    lead = 1 if messages and messages[0].get("role") == "system" else 0
    body = messages[lead:]
    if len(body) < 2:
        return prefix, 0

    ledger = MessageLedger(body)
    allowance = budget_tokens - sum(estimate_message_tokens(m) for m in messages[:lead])
    start = ledger.align_forward(ledger.walk_back_until(allowance), "tool_pair")
    if start >= len(body):
        # Even one exchange is over the allowance. Keep the newest one anyway:
        # a request the model can hold and answer partially beats one it cannot
        # read at all, and the caller has already failed at the full size.
        start = ledger.align_backward(len(body) - 1, "tool_pair")
    if start <= 0:
        return prefix, 0

    kept = (*messages[:lead], *body[start:])
    return ReusedPrefix(messages=kept, reused=0), start


class CheckpointWriter:
    """Builds the summarising request, reads the reply, and returns the checkpoint.

    Holds no provider of its own. The call goes out on whichever provider and
    model the turn that needs it is running on, passed per call, because one
    writer serves every session of the process and two of them can be on
    different models at once.
    """

    def __init__(
        self,
        cfg: "ContextSummaryConfig",
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self._now_fn = now_fn
        if cfg.model:
            logger.warning(
                "context.history.compacting.summary.model={} -- prefix reuse is off for compaction: "
                "a summarising request only hits the cache on the wire that already holds the conversation",
                cfg.model,
            )

    def summary_model(self, model: str) -> str:
        """The model a summary for a turn on ``model`` is written by."""
        return self.cfg.model or model

    def reusable_record(self, record: "RequestRecord | None", model: str) -> "RequestRecord | None":
        """``record`` when a summary for a turn on ``model`` may replay it, else None.

        A record taken under another model is not replayed; one that names no
        model is.
        """
        if record is not None and record.model and record.model != self.summary_model(model):
            return None
        return record

    def prefers_summary(self, *, reached_target: bool, folded_messages: int) -> bool:
        """Whether this removal is worth a model call.

        ``eager`` always, ``off`` never, and ``lazy`` when the fold would be a
        poor stand-in: either the removal did not get back to target -- so the
        window keeps losing messages and needs something that carries their
        content -- or it took out more than a pointer can usefully represent,
        at which point the model reaches for that pointer turn after turn and
        the retrievals cost more than one summary would have.
        """
        if self.cfg.mode == "off":
            return False
        if self.cfg.mode == "eager":
            return True
        return not reached_target or folded_messages > self.cfg.fold_max_messages

    async def compact(
        self,
        *,
        provider: "LLMProvider | None",
        model: str,
        session_key: str,
        generation: int,
        covers: SourceRef,
        previous: "CompactionCheckpoint | None",
        keys: Sequence[MessageKey],
        fresh: Sequence[dict[str, Any]],
        record: "RequestRecord | None",
        tool_defs: Sequence[dict[str, Any]],
        folded_messages: int,
        folded_tokens: int,
    ) -> CompactionCheckpoint:
        """One summary, or the fold that stands in when there is none.

        ``provider`` and ``model`` are the turn's own; ``summary.model``, when
        set, replaces the model and keeps the provider.
        """
        summary_model = self.summary_model(model)
        record = self.reusable_record(record, model)
        prefix = reuse_prefix(keys, fresh, record)
        reply, prefix, cost, failure = await self._ask(provider, summary_model, prefix, tool_defs)
        if reply is None:
            logger.info(
                "Compaction for {} folds instead of summarising ({})",
                session_key,
                failure or "no reply",
            )
            return self.fold(
                session_key=session_key,
                generation=generation,
                covers=covers,
                previous=previous,
                folded_messages=folded_messages,
                folded_tokens=folded_tokens,
            )
        sections = ground_refs(merge_refs(reply, previous), covers)
        checkpoint = CompactionCheckpoint(
            checkpoint_id=new_checkpoint_id(generation),
            session_key=session_key,
            created_at=timestamp(self._now_fn),
            generation=generation,
            covers=covers,
            supersedes=previous.checkpoint_id if previous is not None else None,
            sections=sections,
            model=summary_model,
            byte_prefix_reused=prefix.whole and not self.cfg.model,
            cache_status=cost.status,
            cache_read_tokens=cost.read_tokens,
            cache_write_tokens=cost.write_tokens,
            summary_input_tokens=cost.input_tokens,
            summary_output_tokens=cost.output_tokens,
            kind="summary",
        )
        logger.info(
            "Wrote compaction checkpoint {} (generation {}) for {} over {}; "
            "{}/{} leading message(s) reused from the last request",
            checkpoint.checkpoint_id,
            generation,
            session_key,
            covers.render(),
            prefix.reused,
            len(prefix.messages),
        )
        return checkpoint

    def fold(
        self,
        *,
        session_key: str,
        generation: int,
        covers: SourceRef,
        previous: "CompactionCheckpoint | None",
        folded_messages: int,
        folded_tokens: int,
    ) -> CompactionCheckpoint:
        """The fallback a failed summary leaves: see :func:`fold_checkpoint`."""
        return fold_checkpoint(
            session_key=session_key,
            generation=generation,
            covers=covers,
            previous=previous,
            folded_messages=folded_messages,
            folded_tokens=folded_tokens,
            created_at=timestamp(self._now_fn),
        )

    # ------------------------------------------------------------------

    async def _ask(
        self,
        provider: "LLMProvider | None",
        model: str,
        prefix: ReusedPrefix,
        tool_defs: Sequence[dict[str, Any]],
    ) -> "tuple[dict[str, tuple[SummaryEntry, ...]] | None, ReusedPrefix, CallCost, SummaryFailure | None]":
        """The call, the one shorter retry it may get, and the fold otherwise.

        The first attempt is the whole covered span, because that is the one
        the provider has already cached and the only one that can answer over
        everything being removed. What follows is for the case that attempt
        fails in a way a shorter request could fix -- measured at 128K, where a
        216,610-token request came back with all eight headings and nothing
        under any of them, and the checkpoint folded.

        Retrying only counts when the request actually got shorter: a rerun of
        the same bytes is not a second attempt, it is the same one billed
        twice.

        Returns the prefix the answer actually came from, so the checkpoint
        records whether reuse survived rather than whether it was available.
        """
        attempt, dropped = self._within_cap(prefix, tool_defs)
        sections, failure, cost = await self._attempt(provider, model, attempt, tool_defs, dropped=dropped)
        if sections is not None or failure is None or not failure.retryable or not self.cfg.retry_on_empty:
            return sections, attempt, cost, failure

        shorter, further = shrink_prefix(attempt, budget_tokens=prefix_tokens(attempt, tool_defs) // 2)
        if further == 0:
            logger.warning("Nothing further to drop from the summarising request; compaction folds instead")
            return None, attempt, cost, failure
        logger.info(
            "Retrying the summarising call over the newest {} of {} replayed message(s)",
            len(shorter.messages),
            len(attempt.messages),
        )
        sections, failure, retry_cost = await self._attempt(
            provider, model, shorter, tool_defs, dropped=dropped + further
        )
        return sections, shorter, retry_cost.after(cost), failure

    def _within_cap(
        self,
        prefix: ReusedPrefix,
        tool_defs: Sequence[dict[str, Any]],
    ) -> tuple[ReusedPrefix, int]:
        """The prefix the first attempt goes out as, under ``maxInputTokens``."""
        cap = self.cfg.max_input_tokens
        if cap <= 0:
            return prefix, 0
        size = prefix_tokens(prefix, tool_defs)
        if size <= cap:
            return prefix, 0
        shorter, dropped = shrink_prefix(prefix, budget_tokens=cap)
        if dropped:
            logger.info(
                "Summarising request estimated at {:,} tokens against a "
                "context.history.compacting.summary.maxInputTokens of {:,}; dropping the oldest "
                "{} message(s) and giving up prefix reuse",
                size,
                cap,
                dropped,
            )
        return shorter, dropped

    async def _attempt(
        self,
        provider: "LLMProvider | None",
        model: str,
        prefix: ReusedPrefix,
        tool_defs: Sequence[dict[str, Any]],
        *,
        dropped: int,
    ) -> "tuple[dict[str, tuple[SummaryEntry, ...]] | None, SummaryFailure | None, CallCost]":
        """One call. Returns the sections, or why there are none."""
        chat = getattr(provider, "chat_with_retry", None)
        if not callable(chat):
            logger.debug("No provider that can answer a summarising call; compaction folds instead")
            return None, SummaryFailure.NO_PROVIDER, CallCost()
        instruction = COMPACTION_INSTRUCTION
        if dropped:
            instruction += PARTIAL_REPLAY_NOTE.format(dropped=dropped)
        messages = [*prefix.messages, {"role": "user", "content": instruction}]
        try:
            response = await asyncio.wait_for(
                chat(
                    messages=messages,
                    tools=list(tool_defs) or None,
                    model=model,
                    max_tokens=self.cfg.max_tokens,
                    temperature=_SUMMARY_TEMPERATURE,
                ),
                timeout=self.cfg.timeout_seconds,
            )
        except asyncio.TimeoutError:
            # Size-shaped: the request is large by construction and a summary
            # is the longest thing this conversation asks a model for.
            logger.warning("Summarising call timed out after {}s", self.cfg.timeout_seconds)
            return None, SummaryFailure.TIMEOUT, CallCost()
        except Exception as exc:  # noqa: BLE001 -- a failed summary must not fail the turn
            # Credentials, routing, a model name that does not exist: a shorter
            # request meets the same wall, so this one does not get a retry.
            logger.warning("Summarising call failed ({}); compaction folds instead", exc)
            return None, SummaryFailure.CALL_FAILED, CallCost()

        cost = CallCost.of(response)
        if getattr(response, "has_tool_calls", False):
            # Rule 7 asks for prose while the conversation's tools are in the
            # request anyway, so a model that reaches for one answers with no
            # content at all. That is a choice about the tools, not about the
            # size, and it survives a shorter replay.
            logger.warning("Summarising call answered with a tool call instead of prose; compaction folds instead")
            return None, SummaryFailure.TOOL_CALL, cost
        if getattr(response, "finish_reason", "") == "error":
            # Includes the provider refusing the request outright, which on a
            # summarising call is usually its length.
            logger.warning("Summarising call answered with no usable content")
            return None, SummaryFailure.REFUSED, cost
        if getattr(response, "error_classification", None) is not None:
            # A transport that classified the call as an error and still handed
            # back whatever text arrived. ``finish_reason`` cannot be trusted to
            # say so: a missing one reads "unknown" and an ordinary one "stop",
            # and this call treats neither as a failure, so a failed route that
            # returned a fragment reads here exactly like a completed reply. Parsing the fragment is how a partial answer
            # becomes a checkpoint that claims the span held only what the
            # fragment happened to mention.
            logger.warning(
                "Summarising call came back classified as an error ({}); compaction folds instead",
                getattr(response, "error_classification", None),
            )
            return None, SummaryFailure.TRANSPORT_ERROR, cost
        usage = getattr(response, "usage", None) or {}
        if usage and not int(usage.get("completion_tokens", 0) or 0):
            # Usage reported, and it says nothing was generated. A completion
            # that produced text produced tokens, so text with a zero count is a
            # fragment from a call that did not finish -- the shape a failed
            # route hands back. Judged only when usage is present: a provider
            # (or a test double) that reports none is saying "unknown", and
            # unknown is not evidence of failure.
            logger.warning("Summarising reply reported no generated tokens; compaction folds instead")
            return None, SummaryFailure.NO_TOKENS, cost
        if getattr(response, "truncated", False) or getattr(response, "finish_reason", "") == "length":
            # A reply cut at the ceiling parses: the headings it reached are
            # there and the ones it never got to read as empty. Storing it
            # would file a checkpoint that says the conversation had no next
            # step, which is a worse account of the removed span than the fold
            # -- the fold at least says it does not know.
            # An output bound, not an input one: a shorter replay buys no more
            # room to answer in, so this is the one failure that gets no retry.
            logger.warning(
                "Summarising reply was cut off at its {}-token ceiling "
                "(context.history.compacting.summary.maxTokens); compaction folds instead",
                getattr(response, "max_tokens", None) or self.cfg.max_tokens,
            )
            return None, SummaryFailure.CUT_OFF, cost
        text = getattr(response, "content", None) or ""
        sections = parse_sections(text)
        if sections is None:
            logger.warning("Summarising reply carried none of the eight sections")
            return None, SummaryFailure.NO_SECTIONS, cost
        missing = [name for name in SECTIONS if name not in seen_headings(text)]
        if missing:
            # The instruction says to write the emptiness marker rather than
            # drop a section, so an absent heading is a reply that ended before
            # it was done -- the shape a routing failure takes when the error
            # never reaches finish_reason and the tokens it did spend look
            # ordinary. Storing it would file the sections it never reached as
            # "nothing to report".
            logger.warning(
                "Summarising reply never reached {} of the eight sections ({}); compaction folds instead",
                len(missing),
                ", ".join(missing),
            )
            return None, SummaryFailure.MISSING_SECTIONS, cost
        if not any(sections.values()):
            # Headings and nothing under them -- a reply that copied the
            # template back, or answered every section with its emptiness
            # marker. It would store as a checkpoint saying the removed span
            # held nothing, and with no line to hang a ref on it would not even
            # carry the way back that the fold does.
            logger.warning("Summarising reply filled none of the eight sections")
            return None, SummaryFailure.EMPTY_SECTIONS, cost
        return sections, None, cost


__all__ = [
    "COMPACTION_INSTRUCTION",
    "PARTIAL_REPLAY_NOTE",
    "SYSTEM_KEY",
    "USER_KEY",
    "CallCost",
    "CheckpointWriter",
    "MessageKey",
    "RequestRecord",
    "ReusedPrefix",
    "SummaryFailure",
    "checkpoint_key",
    "prefix_tokens",
    "reuse_prefix",
    "session_message_key",
    "shrink_prefix",
]
