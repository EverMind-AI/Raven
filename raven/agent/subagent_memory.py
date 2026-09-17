"""What a sub-agent wrote into long-term memory during one call.

A sub-agent runs in a process of its own, with a memory the host never sees.
The host holds the call's prompt and output; the memory backend holds what the
sub-agent concluded from it. This module joins the two, through the contract
and nothing else -- it used to speak EverOS's HTTP API directly, which made an
audit trail for every sub-agent depend on one backend being the one installed.

The join needs no cooperation from the sub-agent: every Raven fork passes the
host-minted ``{agent_id}`` through to its own Raven as ``--session <prefix><id>``,
and that session id lands on every memory the backend extracts from the call.
The host mints that id and keeps it in ``InstanceRegistry``, so one
``recall_session`` answers "what did this sub-agent write here".

The file this produces is read by *another sub-agent*, not by a human auditor,
so it carries text and nothing else. Identity, session id, timings and item ids
are diagnostics: they go to the log, where they cost a reader nothing.

A sub-agent with no memory of its own writes nothing to read back. For those,
the host hands its own backend the conversation it already captured and lets it
extract (``source="trace"``). The record names which of the two happened,
because a memory the agent wrote and a memory the host synthesised from its
transcript are not equally strong evidence.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from raven.contracts.memory import MemoryBackend

# One look's ceiling. The poll's own budget bounds the whole sequence; this
# stops a single stalled look from spending all of it.
_LOOK_TIMEOUT_S = 30.0

# Episodes are the user track, cases the agent track; the contract takes one
# track per call, so each is asked for separately.
#
# Profiles and skills are deliberately absent. They accumulate across calls --
# they describe what a sub-agent *is*, not what it just did -- so including
# them would dilute the one signal the reader came for. The backend decides
# what a track contains; this module only names which track it is asking.
_TRACKS: tuple[str, ...] = ("user_id", "agent_id")


@dataclass(frozen=True)
class MemoryScope:
    """How the host addresses one sub-agent's memories.

    ``block`` is the agent's ``memory`` config as written, handed to the
    backend untouched: which keys identify a memory is the backend's
    vocabulary. ``source`` and ``session_prefix`` are read here because the
    host mints the join key and chooses which of the two paths runs.
    """

    block: dict[str, Any]
    source: str = "agent"
    session_prefix: str = "cli:"

    @property
    def user_id(self) -> str | None:
        value = self.block.get("user_id") or self.block.get("userId")
        return str(value) if value else None

    @property
    def agent_id(self) -> str | None:
        value = self.block.get("agent_id") or self.block.get("agentId")
        return str(value) if value else None


@dataclass(frozen=True)
class MemoryItem:
    """One memory, as the record carries it."""

    type: str
    text: str


def scope_from_config(cfg: Any) -> MemoryScope | None:
    """Read a sub-agent's declared memory block, or ``None`` if it has none.

    Args:
        cfg (`SubagentMemoryConfig | None`):
            The agent's declared block.

    Returns:
        `MemoryScope | None`:
            The scope, or ``None`` when nothing was declared.
    """
    if cfg is None:
        return None
    block = cfg.model_dump(exclude_none=True) if hasattr(cfg, "model_dump") else dict(cfg)
    if block.pop("base_url", None) or block.pop("baseUrl", None):
        # Never had a user: no config, fixture or document set one. Honouring
        # it would mean every backend growing a per-call way to address a
        # different server, for a key nobody writes.
        logger.warning("Sub-agent memory: baseUrl is no longer supported and was ignored")
    source = str(block.pop("source", None) or "agent")
    prefix = str(block.pop("session_prefix", None) or block.pop("sessionPrefix", None) or "cli:")
    return MemoryScope(block=block, source=source, session_prefix=prefix)


TRACE_BUDGET_S = 360.0
"""Poll budget for a record whose memories the host had to have extracted.

Sized to insure against a flush that returns before the extraction it
triggered is queryable: the poll begins right after `prime_from_turn`'s flush
call, and everos itself budgets 360s for that call (`_MEMORIZE_TIMEOUT_S`), so
a shorter budget here could give up on a call that was still going to settle.

The empty case pays for that insurance in full: `_delays(360.0)` backs off to
16 looks spanning the whole budget, so a call with nothing to find holds its
background task open for close to 6 minutes and spends 32 requests (two
owners per look) before concluding `pending`.
"""


def trace_session_id(agent: str, call_id: str) -> str:
    """The join key for a call whose memories the host extracts itself.

    Its own namespace rather than the configured ``session_prefix``: that field
    is a fork launcher's convention (see its own docstring), which this id is
    not, and reusing it would stamp ``cli:`` on an acp agent's memories -- naming
    a transport that was never involved.
    """
    return f"trace:{agent}:{call_id}"


@asynccontextmanager
async def started_backend(backend: "MemoryBackend | None", *, label: str) -> AsyncIterator["MemoryBackend | None"]:
    """``backend``, started for the length of one record and stopped after it.

    The host's factory hands back a backend nobody has started, and a record
    runs off the dispatch path where no started one is in reach. Handing an
    unstarted backend to ``record_memories`` records "unavailable" without ever
    attempting the write -- the shipped EverOS adapter's first ``store`` only
    schedules its readiness probe and answers ``False`` -- and leaves the
    adapter's HTTP client open once per record.

    Yields ``None`` when there is no backend or it will not start, which is the
    case every caller already handles as "no record to write".
    """
    if backend is None:
        yield None
        return
    try:
        await backend.start()
    except Exception as exc:  # noqa: BLE001 - a record must never fail the call it describes
        logger.warning("{}: backend did not start ({})", label, exc)
        yield None
        return
    try:
        yield backend
    finally:
        try:
            await backend.stop()
        except Exception:  # noqa: BLE001 - same
            logger.opt(exception=True).debug("{}: backend stop failed", label)


async def prime_from_turn(
    *,
    backend: "MemoryBackend",
    scope: MemoryScope,
    session_id: str,
    turn: list[dict[str, Any]],
) -> bool:
    """Hand one call's conversation to the backend and make it extract now.

    Returns whether the conversation landed. ``False`` rather than raising: the
    caller's next move is to record ``unavailable``, not to fail a run.

    ``flush`` is set because this conversation has already finished -- waiting
    for the backend's own cadence would wait for a turn that never comes, and
    the read back happens immediately after. The owner ids travel with it: the
    content is the sub-agent's, and filing it under the host's identity would
    put it where recall for that agent never looks.
    """
    if not scope.user_id or not scope.agent_id:
        # A missing owner must not fall back to a shared default: that would
        # write this sub-agent's memories into the host's own track instead.
        missing = "user_id" if not scope.user_id else "agent_id"
        logger.warning("Trace for {} has no {} declared; nothing written", session_id, missing)
        return False
    if not turn:
        return False
    try:
        landed = await backend.store(
            session_id,
            _monotonic(turn),
            metadata={"flush": True, **scope.block},
        )
    except Exception as exc:  # noqa: BLE001 - an audit trail must not fail a run
        logger.warning("Trace for {} could not be written: {}", session_id, exc)
        return False
    return landed is not False


def _as_ms_epoch(value: Any) -> int | None:
    """Milliseconds since the epoch, for the clock shapes a turn row carries.

    Reimplemented rather than borrowed from the memory plugin: ordering the
    rows of a captured conversation is the host's own arithmetic, and reaching
    into a plugin for it made this module unusable without that plugin.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        ms = int(value)
        # A ten-digit value is seconds; anything longer is already ms. The
        # boundary is the year 2001 in ms and the year 33658 in seconds, so no
        # real timestamp is ambiguous.
        return ms * 1000 if ms < 100_000_000_000 else ms
    if isinstance(value, str):
        from datetime import datetime

        text = value.strip().replace("Z", "+00:00")
        try:
            return int(datetime.fromisoformat(text).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _monotonic(turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The turn with its first row's clock pulled back before the rest.

    ``append_turn`` stamps the ``user`` row when the turn *ends*, so its clock
    is later than the work it caused. List order is right and the clock is not,
    and a consumer that sorts by timestamp would read the prompt as the last
    thing that happened. Only the first row moves, and only backwards.
    """
    if len(turn) < 2:
        return turn
    stamps = [ms for row in turn[1:] if (ms := _as_ms_epoch(row.get("timestamp")))]
    first = _as_ms_epoch(turn[0].get("timestamp"))
    if not stamps or first is None or first <= min(stamps):
        return turn
    return [{**turn[0], "timestamp": min(stamps) - 1}, *turn[1:]]


async def collect_memories(
    backend: "MemoryBackend",
    scope: MemoryScope,
    session_id: str,
) -> list[MemoryItem]:
    """Every memory the backend holds under ``session_id``, both tracks.

    Raises whatever the backend raises: the caller decides what an unreachable
    memory service means for the record.

    Args:
        backend (`MemoryBackend`):
            The configured memory backend.
        scope (`MemoryScope`):
            Whose memories to read.
        session_id (`str`):
            The join key, already prefixed.

    Returns:
        `list[MemoryItem]`:
            Items with usable text, the user track first.
    """
    items: list[MemoryItem] = []
    for track in _TRACKS:
        owner_id = getattr(scope, track)
        if not owner_id:
            continue
        for memory in await backend.recall_session(session_id, **{track: owner_id}):
            text = " ".join(str(memory.text or "").split())
            if not text:
                continue
            kind = str((memory.metadata or {}).get("type") or track)
            items.append(MemoryItem(type=kind, text=text))
    return items


_DEFAULT_BUDGET_S = 60.0
_BACKOFF_S = (2.0, 4.0, 8.0, 16.0, 30.0)

SETTLED = "settled"
PENDING = "pending"
UNAVAILABLE = "unavailable"


def _delays(budget_s: float) -> list[float]:
    """Backoff steps that fit the budget, always at least one immediate look."""
    delays: list[float] = [0.0]
    spent = 0.0
    for step in _BACKOFF_S:
        if spent + step > budget_s:
            break
        delays.append(step)
        spent += step
    while spent + _BACKOFF_S[-1] <= budget_s:
        delays.append(_BACKOFF_S[-1])
        spent += _BACKOFF_S[-1]
    return delays


async def _poll(
    backend: "MemoryBackend",
    scope: MemoryScope,
    session_id: str,
    budget_s: float,
) -> tuple[list[MemoryItem], str]:
    """Look until the result stops growing, or the budget is spent.

    A backend extracts asynchronously -- it runs a model -- so the first look after a
    call usually finds nothing. Growth stopping is the signal that extraction
    finished, which is why this cannot just take one snapshot.

    ``budget_s`` bounds the whole poll, not merely the sleeps between looks.
    The first look always runs in full -- a budget of zero still means "look
    once" -- but every look after it is itself capped to whatever of the
    budget remains, so a backend that accepts the call and then stalls
    cannot keep this alive for however long its own transport timeout
    allows. A look that runs out of its share of the budget, or fails
    outright, returns whatever was already found rather than losing it (see
    ``record_memories``'s own docstring on why ``unavailable`` means nothing
    was ever read, not that reading stopped).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_s
    found: list[MemoryItem] = []
    for index, delay in enumerate(_delays(budget_s)):
        look_timeout = _LOOK_TIMEOUT_S
        if index > 0:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            if delay:
                await asyncio.sleep(min(delay, remaining))
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
            look_timeout = min(_LOOK_TIMEOUT_S, remaining)
        try:
            current = await asyncio.wait_for(collect_memories(backend, scope, session_id), timeout=look_timeout)
        except asyncio.TimeoutError:
            break
        except Exception as exc:  # noqa: BLE001 - a transient failure keeps what was already found
            logger.warning("Memory poll for {} failed mid-poll: {}", session_id, exc)
            return (found, SETTLED) if found else ([], UNAVAILABLE)
        if current and len(current) == len(found):
            return current, SETTLED
        found = current
    return (found, SETTLED) if found else ([], PENDING)


async def record_memories(
    *,
    agent: str,
    backend: "MemoryBackend",
    scope: MemoryScope,
    resolve_session_id: Callable[[], Awaitable[str | None]],
    write: Callable[[str], Awaitable[None]],
    budget_s: float = _DEFAULT_BUDGET_S,
    instance: str | None = None,
    prime: Callable[[str], Awaitable[bool]] | None = None,
) -> None:
    """Write one call's Memory record.

    Never raises. This is an audit trail written after the call it describes
    has already answered, and losing it must not disturb anything.

    ``resolve_session_id`` is called here rather than before dispatch because
    the registry only holds the instance's id once the backend has committed
    it, which happens when the run ends.

    Args:
        agent (`str`):
            The sub-agent's configured name, recorded verbatim.
        backend (`MemoryBackend`):
            The configured memory backend.
        scope (`MemoryScope`):
            Whose memories to read.
        resolve_session_id (`Callable[[], Awaitable[str | None]]`):
            Yields the join key, or ``None`` when this call has none (a
            stateless agent, or an id that was never read back). ``None``
            writes no file: there is nothing truthful to say.
        write (`Callable[[str], Awaitable[None]]`):
            Receives the record's JSON text.
        budget_s (`float`):
            How long to keep looking for a memory the backend has not
            extracted yet.
        instance (`str | None`):
            The call's instance handle, recorded when it had one. Unlike the
            identity and session id, this is something the reader can act on:
            passing it back as ``spawn``'s ``instance`` continues the same
            conversation. Omitted rather than nulled when absent -- a key that
            is always present but usually empty costs every reader a check.
        prime (`Callable[[str], Awaitable[bool]] | None`):
            Called with the session id before the first read, for an agent whose
            memories the host has to have extracted rather than read back.
            Returning ``False`` -- or raising -- records ``unavailable`` without
            polling. ``None`` reads whatever is already there.
    """
    try:
        session_id = await resolve_session_id()
    except Exception as exc:  # noqa: BLE001 - a failing resolver gives no join key
        logger.warning("Memory record for {} could not resolve session id: {}", agent, exc)
        return
    if not session_id:
        logger.debug("Memory record for {} skipped: no instance id to join on", agent)
        return
    primed = True
    if prime is not None:
        try:
            primed = bool(await prime(session_id))
        except Exception as exc:  # noqa: BLE001 - an unwritten trace is a status
            logger.warning("Memory record for {} could not prime memory: {}", agent, exc)
            primed = False
    if not primed:
        # Nothing landed, so nothing can have been extracted. Polling would
        # spend the whole budget confirming an absence already known.
        items, status = [], UNAVAILABLE
    else:
        try:
            items, status = await _poll(backend, scope, session_id, budget_s)
        except Exception as exc:  # noqa: BLE001 - an unreachable service is a status, not a failure
            logger.warning("Memory record for {} could not read memory: {}", agent, exc)
            items, status = [], UNAVAILABLE
    payload = {
        "agent": agent,
        **({"instance": instance} if instance else {}),
        "source": scope.source,
        "status": status,
        "memories": [{"type": item.type, "text": item.text} for item in items],
    }
    logger.debug(
        "Memory record for {} ({}): {} item(s) under {}",
        agent,
        status,
        len(items),
        session_id,
    )
    try:
        await write(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.warning("Memory record for {} could not be written: {}", agent, exc)
