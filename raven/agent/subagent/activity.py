"""What a delegated run *did*, beside the answer it returned.

A backend already knows far more than the string it hands back: which tools the
sub-agent called, how many tokens it spent, how long it spent thinking. All of it
went to a tracing span and nowhere else (see
:mod:`raven.agent.subagent.backends.observability`), so the panel that shows a
run's transcript could only ever show two messages -- the prompt, and the answer
-- and anyone asking "what did it actually do" had to open a trace viewer to find
out. For an ACP agent that is doubly odd: the protocol reports every tool call as
it happens, and the answer arrived with all of that already collected and then
thrown away.

This module is the one seam that carries it to the record on disk.

A contextvar rather than a parameter on ``SubagentBackend.run``: that protocol has
four implementations here plus whatever a third party wrote, and widening it
would break every one of them for a field most cannot fill. A backend with
nothing to report simply never publishes, and its record looks exactly as it did.
The cli lane has no *structured* per-step account -- no tool calls, no usage --
but it does publish its console tail, which is everything that lane can see.

Nothing here may fail a run. An audit trail that takes down the work it is
describing is worse than no audit trail, so ``publish`` swallows a bad shape
rather than raising into a backend's happy path.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# Names a provider might report token counts under. OpenAI-shaped
# (``prompt_tokens``) is what raven's own providers normalise to; the camelCase
# spellings are what an ACP agent's ``usage_update`` has been seen to send, and
# neither side is in a position to make the other change.
_IN_KEYS = ("prompt_tokens", "promptTokens", "input_tokens", "inputTokens")
_OUT_KEYS = ("completion_tokens", "completionTokens", "output_tokens", "outputTokens")

_MAX_TOOL_CALLS = 200
"""Beyond this the list stops growing. A run that called 200 tools has already
told the reader what kind of run it was, and meta.json is read on every poll of a
panel that polls every few seconds."""


@dataclass
class RunActivity:
    """One delegated run's account of itself, filled in by whoever ran it.

    Every field is optional in the same sense ``record_outcome`` means it: absent
    is "this lane cannot say", which is not the same as zero. A row rendering
    "0 tokens" for an agent that never reports usage would be a lie about the
    agent rather than a gap in the record.
    """

    tool_calls: list[str] = field(default_factory=list)
    tokens_in: int | None = None
    tokens_out: int | None = None
    thought_chars: int = 0
    step_counts: dict[str, int] = field(default_factory=dict)
    # The run's own transcript, as provider-shaped messages (assistant entries
    # carrying reasoning/tool_calls, tool entries carrying results), when the
    # transport can see it. Not part of as_meta: it is written to its own file
    # beside the record, not into a meta.json read on every panel poll.
    transcript: list[dict[str, Any]] = field(default_factory=list)
    # Where this run's ACP frames sit in its connection's journal: the file, the
    # byte range this call occupied, and where the connection's own handshake
    # ends. A pointer rather than the frames themselves -- they are already a
    # file, and meta.json is read on every poll of a panel that polls often.
    frames: dict[str, Any] = field(default_factory=dict)
    # What the run said after its last step, when the transport can tell that
    # apart from what it said on the way. `None` is "this lane cannot say" and
    # the full output stands in; `""` is "it ended on a step and said nothing
    # after it", which is a different fact and must not fall back.
    closing: str | None = None
    # What this run was asked, so a reader watching it can show the turn's own
    # prompt before the turn lands. No file holds it yet for the reader that
    # matters: the record's `prompt.md` is addressed by a task id, and the
    # instance log gets the row only at the end.
    prompt: str | None = None
    # When this run began, so a reader watching it can date the turn. Set on
    # construction rather than reported by the run: what a watcher needs is one
    # instant that does not move between polls, and nothing else on the way holds
    # one -- the transcript rows a transport publishes carry no clock of their
    # own, and the record's timestamps do not exist until the turn lands.
    started_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    # The tail of the run's raw console output, for the lane whose only
    # in-flight signal IS its console (a cli agent that streams no transcript).
    # Live-only, like transcript: the finished record keeps the full output.
    console: str = ""
    # How to merge a person's words into this run while it is in flight, or
    # None when its transport cannot. Set by the backend for the span of its
    # prompt (``offer_steer``); a reader of the run's conversation reaches it
    # through ``live_instance``. Answers the steer's status string rather than
    # raising for a refusal. Not persisted: a live handle, not a fact about the
    # run, which is why it is absent from ``as_meta``.
    steer: Callable[[str], Awaitable[str]] | None = None

    @property
    def tokens(self) -> int | None:
        """What a row shows as the cost of the run, or None if nobody said."""
        if self.tokens_in is None and self.tokens_out is None:
            return None
        return (self.tokens_in or 0) + (self.tokens_out or 0)

    def as_meta(self) -> dict[str, Any]:
        """The subset worth persisting, with empty fields left out entirely.

        Omitted rather than written as null: ``meta.json`` is read back by a
        reader that treats a missing key and a null as the same "not reported",
        and writing both spellings of it would invite the two to drift.
        """
        meta: dict[str, Any] = {}
        if self.tool_calls:
            meta["tool_calls"] = self.tool_calls
        if self.tokens_in is not None:
            meta["tokens_in"] = self.tokens_in
        if self.tokens_out is not None:
            meta["tokens_out"] = self.tokens_out
        if self.thought_chars:
            meta["thought_chars"] = self.thought_chars
        if self.step_counts:
            meta["step_counts"] = self.step_counts
        if self.frames:
            meta["acp_frames"] = self.frames
        return meta


_current: ContextVar[RunActivity | None] = ContextVar("raven_subagent_activity", default=None)

_live: dict[str, RunActivity] = {}
"""Runs being collected right now, keyed by their record's call id.

The disk record is written when the run finishes, so while it is in flight the
only account of it lives in the ``RunActivity`` being collected. This index is
what lets ``subagent.context`` serve that account to a panel watching the run,
instead of a prompt and nothing until the end. Entries live exactly as long as
their ``collecting`` block."""


_live_instances: dict[tuple[str, str, str], RunActivity] = {}
"""The same activities, addressed the way a *conversation* reader has to ask.

``_live`` is keyed by the record's own directory, which is what the panel
watching one call already holds. A reader of an instance's conversation holds
``(session_key, agent, handle)`` and nothing else -- the record name is a task id
it never saw -- so the same run is indexed twice rather than having that reader
guess at a directory layout. Entries live exactly as long as their ``collecting``
block, as ``_live``'s do."""


@contextmanager
def collecting(
    live_key: str | None = None,
    instance: tuple[str, str, str] | None = None,
    prompt: str | None = None,
) -> Iterator[RunActivity]:
    """Collect one run's activity for the duration of the block.

    Opened by whoever owns the record -- the spawn manager, or the DAG runner for
    one node -- because that is who will write the result down. A backend never
    opens one: it publishes into whatever is open, and publishing into nothing is
    a no-op, which is what keeps a backend usable outside either path (a probe, a
    test, a direct call).

    ``live_key`` registers the activity in the live index for the duration of
    the block, so a reader can watch the run before its record lands on disk.
    ``instance`` is ``(session_key, agent, handle)`` and registers it a second
    time for the reader that watches an instance's conversation rather than one
    call.

    ``prompt`` is set here rather than by a call afterwards so that it is in
    place the moment the run becomes findable: a reader landing in the gap would
    otherwise see a turn with steps and no question, and drop the prompt row it
    had already drawn.
    """
    activity = RunActivity(prompt=prompt)
    token = _current.set(activity)
    if live_key:
        _live[live_key] = activity
    if instance:
        _live_instances[instance] = activity
    try:
        yield activity
    finally:
        _current.reset(token)
        if live_key:
            _live.pop(live_key, None)
        if instance:
            _live_instances.pop(instance, None)


def offer_steer(activity: RunActivity | None, steer: Callable[[str], Awaitable[str]] | None) -> None:
    """Publish (or, with None, withdraw) the way to steer the run being collected."""
    if activity is not None:
        activity.steer = steer


def live(key: str) -> RunActivity | None:
    """The activity of a run in flight, or None once it has finished."""
    return _live.get(key)


def live_instance(session_key: str, agent: str, handle: str) -> RunActivity | None:
    """The activity of the turn this instance is answering, if it is answering one."""
    return _live_instances.get((session_key, agent, handle))


def current() -> RunActivity | None:
    """The activity being collected, if anything is collecting."""
    return _current.get()


def note_tool_call(name: str) -> None:
    """Record one tool call, in the order it happened."""
    activity = _current.get()
    if activity is None or not isinstance(name, str) or not name:
        return
    if len(activity.tool_calls) < _MAX_TOOL_CALLS:
        activity.tool_calls.append(name)


def note_usage(usage: Any) -> None:
    """Add one provider usage report to the run's running total.

    Added, not replaced: the in-process loop calls the model once per iteration,
    so a run that used five iterations has five reports and the cost of the run is
    their sum. An ACP agent sends one cumulative ``usage_update`` instead, which
    is why that backend publishes exactly once.
    """
    activity = _current.get()
    if activity is None or not isinstance(usage, dict):
        return
    try:
        for keys, attr in ((_IN_KEYS, "tokens_in"), (_OUT_KEYS, "tokens_out")):
            value = next((usage[k] for k in keys if isinstance(usage.get(k), (int, float))), None)
            if value is None:
                continue
            setattr(activity, attr, (getattr(activity, attr) or 0) + int(value))
    except Exception as exc:  # noqa: BLE001 - the record must never fail the run
        logger.debug("subagent activity: unreadable usage report ({})", exc)


def note_frames(frames: dict[str, Any] | None) -> None:
    """Record where this run's wire frames live. Replaced, not merged."""
    activity = _current.get()
    if activity is not None and isinstance(frames, dict) and frames:
        activity.frames = dict(frames)


def note_steps(counts: dict[str, int] | None) -> None:
    """Record how many updates of each kind the run produced."""
    activity = _current.get()
    if activity is None or not isinstance(counts, dict):
        return
    for kind, count in counts.items():
        if isinstance(kind, str) and isinstance(count, int):
            activity.step_counts[kind] = activity.step_counts.get(kind, 0) + count


def note_thoughts(chars: int) -> None:
    activity = _current.get()
    if activity is not None and isinstance(chars, int) and chars > 0:
        activity.thought_chars += chars


_MAX_CONSOLE_CHARS = 8000
"""The console keeps a tail, not a log: a panel polling a live run redraws the
last screenful, and the finished record already keeps the whole output."""


def note_console(text: str) -> None:
    """Append a chunk of the run's console output, keeping only the tail."""
    activity = _current.get()
    if activity is None or not isinstance(text, str) or not text:
        return
    activity.console = (activity.console + text)[-_MAX_CONSOLE_CHARS:]


_MAX_TRANSCRIPT_MESSAGES = 400


def set_transcript(activity: "RunActivity | None", messages: list[dict[str, Any]] | None) -> None:
    """Record a transcript on one named run, rather than on the ambient one.

    A publisher that does not run in the task the run was opened in has to say
    which run it means. ``_current`` is a ContextVar, and a task copies the
    context at creation: the ACP read loop is created when the *connection* is
    opened, so it sees whatever was current then -- nothing, or worse, another
    run that happens to share the pooled connection.
    """
    if activity is None or not isinstance(messages, list):
        return
    activity.transcript = [m for m in messages[:_MAX_TRANSCRIPT_MESSAGES] if isinstance(m, dict)]


def note_closing(text: str | None) -> None:
    """Record what the run said after its last step, for a lane that can tell.

    Separate from the transcript because it is not a row in it: it is what the
    reader of the record appends as the closing message, and the transcript is
    everything before that.
    """
    activity = _current.get()
    if activity is not None and text is not None:
        activity.closing = text


def append_closing(text: str) -> None:
    """Add to the closing message a run has already reported.

    For a line raven itself appends to the reply after the agent has stopped --
    the partial-turn notice. It has to reach the closing row too, or the one
    sentence saying the answer is incomplete is missing from exactly the record
    a reader goes to for the answer.
    """
    activity = _current.get()
    if activity is not None and activity.closing is not None:
        activity.closing = f"{activity.closing}{text}"


def note_transcript(messages: list[dict[str, Any]] | None) -> None:
    """Record the run's own transcript, for the lane that can actually see one.

    Replaced, not extended: an ACP backend hands over the whole turn at once,
    and a second call within one run would mean the run itself retried -- where
    the later account is the one that produced the answer.
    """
    set_transcript(_current.get(), messages)


__all__ = [
    "append_closing",
    "RunActivity",
    "collecting",
    "current",
    "set_transcript",
    "live",
    "live_instance",
    "note_closing",
    "note_console",
    "note_frames",
    "note_steps",
    "note_thoughts",
    "note_tool_call",
    "note_transcript",
    "note_usage",
]
