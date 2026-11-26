"""Telling a thought that has started repeating itself from one still going somewhere.

A model can fall into a loop inside its reasoning and keep writing one templated line
until the output ceiling stops it, minutes later and with nothing to show. The stream
is the only place that can stop it sooner, so this answers one question for it, on the
tail of the thought so far: does that stretch repeat itself? Repetition is judged by
how well the stretch compresses, which catches a template whose slot changes from line
to line as well as a line repeated verbatim.
"""

from __future__ import annotations

import zlib
from collections.abc import AsyncGenerator
from contextlib import aclosing

from loguru import logger

from raven.providers.base import ChatDelta

#: The finish of a call stopped while its thought was repeating itself, before any answer or tool call.
REASONING_LOOP = "reasoning_loop"

# Measured on 2026-11-26: four MiroThinker-2.0 loops ("Need maybe mention X from Y.
# Good." to the 32768-token ceiling) compressed to 0.04-0.13 of their bytes over this
# window, while 18 ordinary thoughts and 49 delivered reports never went below 0.34.
LOOP_WINDOW_CHARS = 8000
LOOP_CHECK_EVERY_CHARS = 1000
LOOP_MAX_COMPRESSION = 0.15


def reasoning_loops(thought: str) -> bool:
    """Whether the last ``LOOP_WINDOW_CHARS`` of ``thought`` repeat themselves."""
    if len(thought) < LOOP_WINDOW_CHARS:
        return False
    tail = thought[-LOOP_WINDOW_CHARS:].encode("utf-8", errors="replace")
    return len(zlib.compress(tail, 6)) / len(tail) < LOOP_MAX_COMPRESSION


class LoopWatch:
    """Reads a thought as it streams and says when to stop it, checking every ``LOOP_CHECK_EVERY_CHARS``."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._chars = 0
        self._check_at = LOOP_WINDOW_CHARS

    def stop(self, piece: str, *, answering: bool) -> bool:
        """Take ``piece`` of the thought; True when the thought now loops and no answer has begun."""
        self._parts.append(piece)
        self._chars += len(piece)
        if answering or self._chars < self._check_at:
            return False
        self._check_at = self._chars + LOOP_CHECK_EVERY_CHARS
        if not reasoning_loops("".join(self._parts)):
            return False
        logger.warning("the model's reasoning is repeating itself after {} chars; stopping the call", self._chars)
        return True


async def stop_looping_thought(stream: AsyncGenerator[ChatDelta, None]) -> AsyncGenerator[ChatDelta, None]:
    """Pass ``stream`` through, ending it on a ``REASONING_LOOP`` finish once its thought loops unanswered.

    Left alone, a looping thought runs to the output ceiling (182-200 s a call, measured).
    Ending here closes ``stream``, which lets the server stop generating.
    """
    watch = LoopWatch()
    answering = False
    async with aclosing(stream):
        async for delta in stream:
            answering = answering or bool(delta.content or delta.tool_call_delta)
            piece = getattr(delta, "reasoning_content", None)
            if piece and watch.stop(piece, answering=answering):
                yield ChatDelta(content=None, finish_reason=REASONING_LOOP)
                return
            yield delta
