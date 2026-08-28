"""One ACP session: its directories, its engine, and the prompt in flight.

A session here is heavier than the protocol's idea of one, and deliberately: it
owns an engine of its own. The reason is in ``raven/ppt/contracts/project.py`` --
``Project.root`` is ``workspace / "deck"`` and its docstring says outright that "a
workspace is one task: the launcher makes a directory per spawn and the agent is
fenced inside it". Two sessions sharing one engine would share one ``deck/`` and
each would build over the other. So the session is the unit the workspace, the
staged material and the published deck all belong to.

Two invariants the rest of the layer relies on:

* **Exactly one terminating event resolves a prompt.** The gate is the future's
  own state rather than a separate flag: a cancel followed by the sink's failure
  event is the normal shape, not a bug, and a second settle must be a no-op
  rather than an ``InvalidStateError`` raised inside a delivery worker where
  nothing would report it.
* **One prompt per session at a time.** The lane would serialise a second one
  anyway, but the client needs to be told rather than left waiting: two prompts
  on one session cannot both be answered by a stream that carries one turn's
  events.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

MATERIALS_DIRNAME = "materials"
OUT_DIRNAME = "out"
"""The two directories a job carries beside the ppt tool's own ``deck/``.

Named here rather than inline because the prompt text tells the model both paths
and the deck verification reads one of them; a second spelling would have the
agent publish where nothing looks.
"""


class TurnAlreadyRunningError(RuntimeError):
    """A second ``session/prompt`` arrived while one was still in flight."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id} already has a prompt in flight")
        self.session_id = session_id


@dataclass
class _Turn:
    """One in-flight ``session/prompt``."""

    future: asyncio.Future[str]

    def settle(self, stop: str) -> bool:
        """Resolve the prompt once, and report whether this call was the one."""
        if self.future.done():
            return False
        self.future.set_result(stop)
        return True


@dataclass
class AcpSession:
    """One session, its job directory, and its engine.

    ``session_id`` is both the ACP session id and the raven session key. One
    identity rather than two: spine addresses a turn's events by conversation id,
    every ``session/update`` frame is addressed by ``sessionId``, and a second id
    space would need a map that survives nothing to be worth anything.

    ``cwd`` is the client's working directory, not this session's workspace. It
    is where the finished deck is delivered -- for a raven host that is the live
    session workspace of the conversation that delegated the deck, which is
    exactly where the launcher's ``deliver()`` used to put it.
    """

    session_id: str
    cwd: str
    root: Path
    engine: Any = None
    turn: _Turn | None = None
    # Sources staged for this session so far, as (source, staged copy). Kept per
    # session rather than per turn: a second turn adds material without losing
    # what the first one grounded the deck in, and the prompt lists all of it.
    staged: list[tuple[str, Path]] = field(default_factory=list)
    # Basenames already used under ``materials/``, so a second turn's source
    # cannot overwrite a first turn's copy of the same name.
    taken: set[str] = field(default_factory=set)
    # This turn's reply text, as the outlet put it on the wire. Accumulated
    # because the deck search reads the ``MEDIA:`` line out of it: the host builds
    # the sub-agent's reply from ``agent_message_chunk`` text and nothing else, so
    # what the client will read is exactly what is collected here.
    reply: list[str] = field(default_factory=list)
    # Set when the connection is shutting down; see ``begin_turn``.
    closing: bool = False

    @property
    def materials(self) -> Path:
        return self.root / MATERIALS_DIRNAME

    @property
    def out(self) -> Path:
        return self.root / OUT_DIRNAME

    def ensure_dirs(self) -> None:
        for directory in (self.materials, self.out):
            directory.mkdir(parents=True, exist_ok=True)

    def begin_turn(self) -> asyncio.Future[str]:
        if self.turn is not None:
            raise TurnAlreadyRunningError(self.session_id)
        self.reply.clear()
        self.turn = _Turn(future=asyncio.get_running_loop().create_future())
        if self.closing:
            # A latch, not a check-and-refuse. The connection is going away, and a
            # prompt frame that was read before EOF but whose handler had not run
            # yet would otherwise open a turn nothing will ever settle -- and then
            # sit there for the whole shutdown grace period. Answering it now is
            # the truthful outcome and the prompt exit for free.
            self.turn.settle("cancelled")
        return self.turn.future

    def reply_text(self) -> str:
        return "".join(self.reply)

    def settle(self, stop: str) -> bool:
        """Resolve this session's pending prompt, if it still has one."""
        turn = self.turn
        return False if turn is None else turn.settle(stop)

    def end_turn(self) -> None:
        self.turn = None


class SessionTable:
    """The sessions one connection holds.

    Not a plain dict because release has to be idempotent and has to tear the
    engine down: a client that closes a session and then closes the connection
    would otherwise run the teardown twice, and a session dropped without it
    leaves an outlet worker and an MCP subprocess behind.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, AcpSession] = {}

    def add(self, session: AcpSession) -> None:
        self._sessions[session.session_id] = session

    def get(self, session_id: Any) -> AcpSession | None:
        if not isinstance(session_id, str):
            return None
        return self._sessions.get(session_id)

    def all(self) -> list[AcpSession]:
        return list(self._sessions.values())

    def settle_all(self, stop: str = "cancelled") -> int:
        """Answer every pending prompt, without touching the engines.

        The first half of a connection shutdown, and it has to be its own step.
        A suspended ``session/prompt`` returns through its own code once its
        future resolves -- but that code still reads the session's engine, so the
        teardown cannot have run yet. Hence: settle here, let the handlers finish,
        release afterwards.
        """
        settled = 0
        for session in self._sessions.values():
            session.closing = True
            settled += bool(session.settle(stop))
        return settled

    async def release(self, session_id: str) -> None:
        """Drop one session and tear its engine down. Unknown id is a no-op."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        # Settled before the teardown, not after: a handler suspended on this
        # session's prompt has cleanup of its own to run, and the teardown it is
        # waiting behind would otherwise pull the engine out from under it.
        session.settle("cancelled")
        await _close_engine(session)

    async def aclose(self) -> None:
        """Release every session. Best-effort: one failure must not cost the rest."""
        for session_id in list(self._sessions):
            try:
                await self.release(session_id)
            except Exception:
                logger.exception("acp: releasing session {} failed", session_id)


async def _close_engine(session: AcpSession) -> None:
    engine = session.engine
    if engine is None:
        return
    session.engine = None
    teardown: Callable[[], Awaitable[None]] | None = getattr(engine, "teardown", None)
    if teardown is None:
        return
    try:
        await teardown()
    except Exception:
        # Teardown is best-effort by construction; this catches a failure in the
        # callable itself, which would otherwise replace a clean session close
        # with a traceback the client cannot act on.
        logger.exception("acp: tearing down the engine for {} failed", session.session_id)


__all__ = [
    "MATERIALS_DIRNAME",
    "OUT_DIRNAME",
    "AcpSession",
    "SessionTable",
    "TurnAlreadyRunningError",
]
