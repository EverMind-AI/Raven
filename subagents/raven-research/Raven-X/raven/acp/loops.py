"""One ``AgentLoop`` per ACP session, and the pieces the process shares.

The ACP server used to hold a single engine for every session on the
connection, which forced the user pool to one: ``WebSearchTool`` /
``WebFetchTool`` keep their per-turn state (saturation, evidence round, seen
sets, retry budgets) as instance attributes, and ``run_turn`` resets them at
every turn start, so two concurrent turns on one loop silently cleared each
other's in-flight state. A registry keyed by conversation id removes that
sharing at its root: every session gets its own tools, its own flow objects,
and its own budgets, while the expensive singletons (provider, session
manager, memory backend) stay process-wide.

``session_id`` is both the wire ``sessionId`` and the spine conversation id
(``AcpSession``), so the registry needs no id map -- the key a turn arrives
with is the key its engine is filed under.

Reclamation is by cap, not by protocol event: ``session/delete`` is not served
(``methods.UNIMPLEMENTED_METHODS``), so nothing tells this registry a session
is finished. The cap evicts the least recently used *idle* session; a session
with a turn in flight is never evicted, however old -- which means a build can
find no victim and go over the cap on purpose, so the cap is re-applied on
every :meth:`AcpLoops.get` and, through :meth:`AcpLoops.reclaim`, every time a
turn settles. Both, because neither alone is enough: a quiet connection makes
no further ``get`` calls, and a turn ending is only visible to the spine.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from loguru import logger

DEFAULT_MAX_LOOPS = 8
"""How many engines one connection keeps alive at once.

A ceiling on resident memory, not a concurrency limit -- that is the user pool.
Each engine holds a full tool registry, prompt and skills state, so the count
has to be bounded for a process that outlives every session it serves.
"""


async def close_loop(loop: Any) -> None:
    """Release one engine's own resources, in the order the CLI surfaces use.

    Duck-typed throughout: a stub loop in a test has none of these, and a
    registry that insisted on them could not be driven without a real engine.
    Each step is guarded on its own because a failure in one must not strand
    the rest -- an unclosed sandbox executor outlives the process that leaked
    it, and an undrained store loses the turns that were slowest to index.
    """
    stop = getattr(loop, "stop", None)
    if callable(stop):
        try:
            stop()
        except Exception:
            logger.exception("acp: stopping a session engine failed")
    for name in ("drain_backend_stores", "close_executor", "close_mcp"):
        method = getattr(loop, name, None)
        if not callable(method):
            continue
        try:
            await method()
        except Exception:
            logger.exception("acp: {} failed while closing a session engine", name)


class AcpLoops:
    """The connection's engine registry: one loop per conversation id.

    ``factory`` is called with the conversation id, so a per-session engine can
    derive per-session paths from it. ``session_manager`` is the one shared
    piece the protocol layer reads directly (``session/load`` peeks the store
    before any engine for that id exists), which is why it is injected here
    rather than fished off a loop.
    """

    def __init__(
        self,
        factory: Callable[[str], Any],
        *,
        session_manager: Any = None,
        max_loops: int = DEFAULT_MAX_LOOPS,
        per_session: bool = True,
        tag_for: Callable[[str], Any] | None = None,
    ) -> None:
        if max_loops < 1:
            raise ValueError(f"max_loops must be at least 1, got {max_loops}")
        self._factory = factory
        self.session_manager = session_manager
        self._max_loops = max_loops
        self.per_session = per_session
        self._tag_for = tag_for
        self._loops: OrderedDict[str, Any] = OrderedDict()
        self._tags: dict[str, Any] = {}
        self._on_create: list[Callable[[Any], None]] = []
        self._busy: Callable[[str], bool] = lambda _conversation: False
        self._closing: set[asyncio.Task[None]] = set()

    @classmethod
    def single(cls, loop: Any, **kwargs: Any) -> AcpLoops:
        """A registry that serves one engine to every session.

        The pre-per-session shape, kept for the surfaces that genuinely have
        one engine (a test driving the protocol against a stub). Marked
        ``per_session=False`` so :func:`raven.acp.spine.build_acp` refuses to
        run concurrent user turns on it.
        """
        kwargs.setdefault("session_manager", getattr(loop, "sessions", None))
        return cls(lambda _conversation: loop, per_session=False, **kwargs)

    def on_create(self, hook: Callable[[Any], None]) -> None:
        """Register a hook run on every engine as it is built.

        The arming seam: capabilities the client declares once (``ask_user``)
        have to reach the engines built after the declaration too, not just the
        ones alive when it arrived.
        """
        self._on_create.append(hook)

    def set_busy(self, predicate: Callable[[str], bool]) -> None:
        """Teach the cap which conversations have a turn in flight."""
        self._busy = predicate

    def peek(self, conversation: str) -> Any | None:
        """The engine for ``conversation`` if one exists; never builds one."""
        return self._loops.get(conversation)

    async def get(self, conversation: str) -> Any:
        """The engine for ``conversation``, building it on first use.

        A coroutine with nothing to await yet, deliberately: it is passed
        straight to ``AcpMethods(on_session_open=...)``, which awaits it, and an
        engine that comes to need async setup (a warm-up, a probe) must not turn
        into a signature change at both call sites.
        """
        loop = self._loops.get(conversation)
        if loop is not None and self._stale(conversation):
            # The session's construction inputs changed under it (a mode
            # switch). Rebuilt HERE and nowhere else: this runs at the start of
            # a turn, before the engine has any in-flight tool state, so a
            # switch mid-turn costs the running turn nothing and the next turn
            # gets the profile that was asked for. Releasing at the moment of
            # the switch would instead take the tools out from under whatever
            # was running.
            logger.info("acp: rebuilding the engine for session {}; its profile changed", conversation)
            self.release(conversation)
            loop = None
        if loop is not None:
            self._loops.move_to_end(conversation)
            # Also on the hit path: an earlier build that found every victim
            # mid-turn left the registry over the cap on purpose, and nothing
            # rechecks it for free -- see :meth:`reclaim`.
            self._evict_over_cap(keep=conversation)
            return loop
        loop = self._factory(conversation)
        self._loops[conversation] = loop
        if self._tag_for is not None:
            self._tags[conversation] = self._tag_for(conversation)
        logger.info("acp: engine built for session {} ({} resident)", conversation, len(self._loops))
        for hook in self._on_create:
            try:
                hook(loop)
            except Exception:
                logger.exception("acp: a session-engine create hook failed")
        self._evict_over_cap(keep=conversation)
        return loop

    def _stale(self, conversation: str) -> bool:
        """Whether the resident engine was built under a tag that has moved.

        No tagger (every caller before session modes) means nothing is ever
        stale, so the registry behaves exactly as it did.
        """
        if self._tag_for is None:
            return False
        return self._tags.get(conversation) != self._tag_for(conversation)

    def cwd_for(self, conversation: str) -> str | None:
        """The directory a tool call's relative paths resolve against.

        Peeks: an outlet frame for a session whose engine is already gone
        reports the path as it was given rather than building an engine to
        answer a formatting question.
        """
        loop = self.peek(conversation)
        if loop is None:
            return None
        root = getattr(loop, "file_workspace", None) or getattr(loop, "workspace", None)
        return str(root) if root else None

    def live(self) -> tuple[Any, ...]:
        return tuple(self._loops.values())

    def resident(self) -> tuple[str, ...]:
        return tuple(self._loops)

    def reclaim(self) -> None:
        """Bring the registry back under the cap now that a turn has ended.

        The cap is applied when an engine is built, and a build that found every
        other resident session mid-turn goes over it deliberately rather than
        pull the tools out from under a running turn. Those turns ending is not
        an event this registry sees, so without a call here the extra engines --
        a full tool registry and executor each -- stay resident past the
        configured ceiling until the next :meth:`get`, which on an idle
        connection is never.
        """
        self._evict_over_cap()

    def _evict_over_cap(self, *, keep: str | None = None) -> None:
        """Bring the registry back under the cap, oldest idle session first.

        ``keep`` is the session whose engine the caller is about to use. It
        matters when every other resident session is mid-turn: those are skipped,
        which leaves the entry just handed out as the only candidate, and the
        turn would start on an engine that had been closed under it. ``None``
        from :meth:`reclaim`, where no engine is being handed out and the busy
        predicate alone decides.
        """
        while len(self._loops) > self._max_loops:
            victim = next((key for key in self._loops if key != keep and not self._busy(key)), None)
            if victim is None:
                # Every other resident session is mid-turn. Going over the cap
                # is the lesser fault: evicting a running turn's engine would
                # take its tools out from under it.
                logger.warning(
                    "acp: {} engines resident, none evictable; over the cap of {}",
                    len(self._loops),
                    self._max_loops,
                )
                return
            self.release(victim)

    def release(self, conversation: str) -> None:
        """Drop one engine and close it in the background.

        Closing is detached because the caller is on the path of a turn that is
        starting: draining another session's memory writes must not be charged
        to it. The task is tracked so :meth:`aclose` still waits for it.
        """
        loop = self._loops.pop(conversation, None)
        self._tags.pop(conversation, None)
        if loop is None:
            return
        logger.info("acp: releasing the engine for session {}", conversation)
        task = asyncio.ensure_future(close_loop(loop))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def aclose(self) -> None:
        """Close every resident engine, and whatever is still being closed."""
        residents = tuple(self._loops.items())
        self._loops.clear()
        self._tags.clear()
        if residents:
            await asyncio.gather(*(close_loop(loop) for _key, loop in residents), return_exceptions=True)
        pending = tuple(self._closing)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


__all__ = ["DEFAULT_MAX_LOOPS", "AcpLoops", "close_loop"]
