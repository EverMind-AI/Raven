"""The pluggable execution contract for a spawned sub-agent."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from loguru import logger

from raven.agent.subagent import activity
from raven.contracts.subagent_backend import (  # noqa: F401
    SubagentActionAbortedError,
    SubagentBackend,
    SubagentNoAnswerError,
)

IN_SUBAGENT_RUN: ContextVar[bool] = ContextVar("raven_in_subagent_run", default=False)
"""True while an in-process backend is executing a sub-agent task.

Orchestration tools (``run_subagent_dag``) read this to refuse re-entry: a
sub-agent must not fan out another graph of sub-agents. Only in-process
backends can set it — an out-of-process CLI/HTTP agent runs its own tools
beyond Raven's reach, so the flag makes no claim about those."""


ABORTED_ACTION_RESULT = (
    "The subtask stopped because a safety decision terminated the requested operation. "
    "No alternative method was attempted."
)


def bounded_delta(
    on_delta: "Callable[[str], Awaitable[None]] | None", limit: int
) -> "Callable[[str], Awaitable[None]] | None":
    """Wrap a delta callback so what streams stays a prefix of what returns.

    The backends that cap a reply truncate their return value to
    ``max_output_chars``. An uncapped stream would render text the record
    never stores and the next turn's history never replays -- the transcript on
    screen would be the only place that text ever existed, and it would vanish
    on the next switch into the instance.

    Returns ``None`` unchanged, so a caller can wrap unconditionally.

    One deliberate exception: raven's own truncation notice is sent through the
    *unbounded* callback, past this budget. A reply that saturates the budget is
    precisely the one about to be capped, so honouring the limit for that line
    would drop the only sentence saying the rest of the answer is missing. The
    exempt text is raven's, never the agent's, and it is in the record too.
    """
    if on_delta is None:
        return None

    remaining = limit

    async def emit(text: str) -> None:
        nonlocal remaining
        if remaining <= 0 or not text:
            return
        chunk = text[:remaining]
        remaining -= len(chunk)
        await on_delta(chunk)

    return emit


def _truncation_notice(kept: int, total: int, limit: int, reason: str) -> str:
    """The one line a capped reply carries so nobody has to guess it was capped.

    Worded the way the rest of the repository words this -- "first N of M", the
    same shape the filesystem tool and the DAG reader use -- rather than a new
    spelling of the same fact.
    """
    return (
        f"\n\n[raven] Output truncated: this reply is the first {kept} of {total} characters "
        f"({reason}={limit}). The complete output is in this call's record, as out.md."
    )


async def clamp_output(
    text: str,
    limit: int,
    *,
    agent: str,
    reason: str = "max_output_chars",
    reserved: str = "",
    sink: "Callable[[str], Awaitable[None]] | None" = None,
) -> str:
    """Cap a sub-agent's reply at ``limit``, and never do it silently.

    The cap exists to protect the caller's context window, and it stays where it
    is applied -- at the backend's return. What changes here is that dropping
    text now has three consequences it did not have: a warning in the log, a
    line in the reply itself, and structured counters plus the *whole* answer on
    the run's activity, from which the record writes ``out.md``.

    Silence was the defect. A capped reply ended mid-sentence, the run was
    recorded ``completed``, and the record advertised as the recovery artifact
    held the same truncated bytes -- so neither the host agent nor the user nor
    a later reader had any way to learn that the decisive paragraph had been at
    character 31000.

    ``reserved`` is text the caller has already decided to append (the cli
    lane's "not resumable" warning). It is budgeted first, then the notice, and
    only then the reply -- both lines are raven's own account of the run and are
    worth more per character than the reply's tail. Note that a caller passing
    ``reserved`` gets it back even when nothing was truncated, so the two are
    appended in one place rather than two.

    ``sink`` is the *unbounded* delta callback, for the same reason the acp
    lane's partial-turn notice takes one: a caller that streamed is never handed
    the return value a second time, so a notice riding only on it would reach
    the record and never the screen. The bounded wrapper would be the wrong one
    -- a reply that saturates the budget would swallow the line explaining that
    it was cut.
    """
    if len(text) + len(reserved) <= limit:
        return text + reserved
    # The notice is measured with `limit` standing in for `kept`: `kept` can
    # never exceed `limit`, so it can never have more digits, which makes this
    # a bound rather than a guess. Costs a character or two of reply and avoids
    # a second formatting pass whose result could no longer fit.
    room = limit - len(reserved) - len(_truncation_notice(limit, len(text), limit, reason))
    if room > 0:
        notice = _truncation_notice(room, len(text), limit, reason)
        kept = room
    else:
        # A cap too small to hold the notice keeps the reply instead of a
        # sentence about the reply: at that size the notice would *be* the whole
        # answer. The loss is still logged and still on the activity, so it stays
        # legible where legibility costs the caller nothing it was going to get.
        notice = ""
        kept = max(0, limit - len(reserved))
    logger.warning(
        "Subagent {!r} reply capped: kept {} of {} chars ({}={}); the record keeps the whole of it",
        agent,
        kept,
        len(text),
        reason,
        limit,
    )
    activity.note_output_truncation(text, returned=kept, reason=reason)
    if notice:
        # Reaches the record's closing row too, for the lane that reports one --
        # the reader of that record goes there for the answer, and the sentence
        # saying the answer is partial has to be in the same place.
        activity.append_closing(notice)
        if sink is not None:
            await sink(notice)
    # Clamped again: with a `limit` below the fixed lines' own length there is
    # no arrangement that fits, and the cap is the promise that holds.
    return (text[:kept] + notice + reserved)[:limit]
