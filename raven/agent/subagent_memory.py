"""What a sub-agent wrote into everos during one call.

A sub-agent running on the everos memory backend writes into a store the host
never sees. The host holds the call's prompt and output; everos holds what the
sub-agent concluded from it. This module joins the two.

The join needs no cooperation from the sub-agent: every Raven fork passes the
host-minted ``{agent_id}`` through to its own Raven as ``--session cli:<id>``,
and that session id lands on every memory everos extracts from the call. The
host mints that id and keeps it in ``InstanceRegistry``, so one filtered read
of ``/api/v2/memory/get`` answers "what did this sub-agent write here".

The file this produces is read by *another sub-agent*, not by a human auditor,
so it carries text and nothing else. Identity, session id, timings and item ids
are diagnostics: they go to the log, where they cost a reader nothing.

A sub-agent that does not run everos writes nothing to read back. For those, the
host hands everos the conversation it already captured (``prime_from_turn``) and
lets everos extract from it, then reads the result through the same poll. The
record names which of the two happened, because a memory the agent wrote and a
memory the host synthesised from its transcript are not equally strong evidence.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from raven.core.plugin_stack import everos_plugin_installed, everos_plugin_missing_note

_PAGE_SIZE = 100
_HTTP_TIMEOUT_S = 30.0
_FLUSH_TIMEOUT_S = 360.0
"""Budget for `prime_from_turn`'s flush call, not the plain `add` before it.

Flush is what triggers extraction -- it runs an LLM -- and everos itself
budgets 360s for exactly this call (`_MEMORIZE_TIMEOUT_S` in
`plugins-dist/everos-memory/raven_everos/backend.py`). `add` is a plain append and keeps
the module's regular `_HTTP_TIMEOUT_S`.
"""

# episode is user-owned, agent_case is agent-owned; the endpoint takes exactly
# one owner per call, so each is asked for separately.
#
# profile and agent_skill are deliberately absent. They accumulate across calls
# -- they describe what a sub-agent *is*, not what it just did -- so including
# them would dilute the one signal the reader came for.
_OWNED: tuple[tuple[str, str, str], ...] = (
    ("user_id", "episode", "episodes"),
    ("agent_id", "agent_case", "agent_cases"),
)


@dataclass(frozen=True)
class EverosIdentity:
    """How the host addresses one sub-agent's memories."""

    user_id: str | None
    agent_id: str | None
    base_url: str
    session_prefix: str
    source: str = "agent"


@dataclass(frozen=True)
class MemoryItem:
    """One memory, as the record carries it."""

    type: str
    text: str


def identity_from_config(cfg: Any, default_base_url: str) -> EverosIdentity | None:
    """Read a sub-agent's declared everos identity, or ``None`` if it has none.

    Args:
        cfg (`SubagentEverosConfig | None`):
            The agent's declared block.
        default_base_url (`str`):
            The host's own everos base url, used when the block names none.

    Returns:
        `EverosIdentity | None`:
            The identity, or ``None`` when nothing was declared.
    """
    if cfg is None:
        return None
    base = (getattr(cfg, "base_url", None) or default_base_url).rstrip("/")
    return EverosIdentity(
        user_id=getattr(cfg, "user_id", None),
        agent_id=getattr(cfg, "agent_id", None),
        base_url=base,
        session_prefix=getattr(cfg, "session_prefix", "cli:"),
        source=getattr(cfg, "source", "agent"),
    )


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


async def prime_from_turn(
    *,
    identity: EverosIdentity,
    session_id: str,
    turn: list[dict[str, Any]],
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Hand one call's conversation to everos and make it extract from it.

    Returns whether the conversation landed. ``False`` rather than raising: the
    caller's next move is to record ``unavailable``, not to fail a run.

    everos extracts on ``flush``, not on ``add`` (see ``EverosBackend.store``),
    so both calls are made here and each must land for anything to be readable.
    """
    if not identity.user_id or not identity.agent_id:
        # A missing owner must not fall back to a shared default: that would
        # write this sub-agent's memories into the host's own track instead.
        missing = "user_id" if not identity.user_id else "agent_id"
        logger.warning("Trace for {} has no {} declared; nothing written", session_id, missing)
        return False
    if not turn:
        return False
    if not everos_plugin_installed():
        # The read path below needs only httpx, so an agent that writes its own
        # memories still reads back; priming is the half that needs the plugin's
        # message shapes, and its absence is a status, not a failure.
        logger.warning("Trace for {} was not written: {}", session_id, everos_plugin_missing_note())
        return False
    from raven_everos.backend import convert_messages

    payload = convert_messages(
        _monotonic(turn),
        agent_id=identity.agent_id,
        user_id=identity.user_id,
    )
    if not payload:
        return False
    owned = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(_HTTP_TIMEOUT_S))
    try:
        add_response = await http.post(
            f"{identity.base_url}/api/v2/memory/add",
            json={"session_id": session_id, "messages": payload},
            timeout=_HTTP_TIMEOUT_S,
        )
        add_response.raise_for_status()
        flush_response = await http.post(
            f"{identity.base_url}/api/v2/memory/flush",
            json={"session_id": session_id},
            timeout=_FLUSH_TIMEOUT_S,
        )
        flush_response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - an unwritten trace is a status, not a failure
        logger.warning("Trace for {} could not be written to {}: {}", session_id, identity.base_url, exc)
        return False
    finally:
        if owned:
            await http.aclose()
    return True


def _monotonic(turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The turn with its first row's clock pulled back before the rest.

    ``append_turn`` stamps the ``user`` row when the turn *ends*, so its clock
    is later than the work it caused. List order is right and the clock is not,
    and a consumer that sorts by timestamp would read the prompt as the last
    thing that happened. Only the first row moves, and only backwards.
    """
    if len(turn) < 2:
        return turn
    from raven_everos.backend import as_ms_epoch

    stamps = [ms for row in turn[1:] if (ms := as_ms_epoch(row.get("timestamp")))]
    first = as_ms_epoch(turn[0].get("timestamp"))
    if not stamps or first is None or first <= min(stamps):
        return turn
    return [{**turn[0], "timestamp": min(stamps) - 1}, *turn[1:]]


def _joined(*parts: Any) -> str:
    """The given fields as one whitespace-normalised line, empties dropped.

    Deliberately uncapped. The reader is a sub-agent whose file tool already
    handles length, so trimming here would only drop the end of what the
    sub-agent concluded -- which is where a narrative keeps its findings.
    """
    return " - ".join(" ".join(str(p).split()) for p in parts if p and str(p).strip())


def _text_of(memory_type: str, row: dict) -> str:
    if memory_type == "episode":
        # `summary` is a hard 200-character prefix of `episode` (verified against
        # everos 1.2.1), so it is the fallback, never the choice: taking it drops
        # the rest of the sentence it cuts mid-word.
        return _joined(row.get("subject"), row.get("episode") or row.get("summary"))
    return _joined(row.get("task_intent"), row.get("approach"), row.get("key_insight"))


async def collect_memories(
    client: httpx.AsyncClient,
    identity: EverosIdentity,
    session_id: str,
) -> list[MemoryItem]:
    """Every memory everos holds for this identity under ``session_id``.

    Raises whatever httpx raises: the caller decides what an unreachable everos
    means for the record.

    Args:
        client (`httpx.AsyncClient`):
            Client to talk to everos with.
        identity (`EverosIdentity`):
            Whose memories to read.
        session_id (`str`):
            The join key, already prefixed.

    Returns:
        `list[MemoryItem]`:
            Items with usable text, episodes first.
    """
    items: list[MemoryItem] = []
    for owner_key, memory_type, data_key in _OWNED:
        owner_id = getattr(identity, owner_key)
        if not owner_id:
            continue
        response = await client.post(
            f"{identity.base_url}/api/v2/memory/get",
            json={
                owner_key: owner_id,
                "memory_type": memory_type,
                "filters": {"session_id": session_id},
                "page_size": _PAGE_SIZE,
            },
            timeout=_HTTP_TIMEOUT_S,
        )
        response.raise_for_status()
        data = (response.json() or {}).get("data") or {}
        for row in data.get(data_key) or []:
            if not isinstance(row, dict):
                continue
            text = _text_of(memory_type, row)
            if text:
                items.append(MemoryItem(type=memory_type, text=text))
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
    client: httpx.AsyncClient,
    identity: EverosIdentity,
    session_id: str,
    budget_s: float,
) -> tuple[list[MemoryItem], str]:
    """Look until the result stops growing, or the budget is spent.

    everos extracts asynchronously (it runs an LLM), so the first look after a
    call usually finds nothing. Growth stopping is the signal that extraction
    finished, which is why this cannot just take one snapshot.

    ``budget_s`` bounds the whole poll, not merely the sleeps between looks.
    The first look always runs in full -- a budget of zero still means "look
    once" -- but every look after it is itself capped to whatever of the
    budget remains, so an everos that accepts the connection and then stalls
    cannot keep this alive for however long a single HTTP call's own timeout
    allows. A look that runs out of its share of the budget, or fails
    outright, returns whatever was already found rather than losing it (see
    ``record_memories``'s own docstring on why ``unavailable`` means nothing
    was ever read, not that reading stopped).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_s
    found: list[MemoryItem] = []
    for index, delay in enumerate(_delays(budget_s)):
        look_timeout = _HTTP_TIMEOUT_S
        if index > 0:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            if delay:
                await asyncio.sleep(min(delay, remaining))
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
            look_timeout = min(_HTTP_TIMEOUT_S, remaining)
        try:
            current = await asyncio.wait_for(collect_memories(client, identity, session_id), timeout=look_timeout)
        except asyncio.TimeoutError:
            break
        except Exception as exc:  # noqa: BLE001 - a transient failure keeps what was already found
            logger.warning("Memory poll for {} at {} failed mid-poll: {}", session_id, identity.base_url, exc)
            return (found, SETTLED) if found else ([], UNAVAILABLE)
        if current and len(current) == len(found):
            return current, SETTLED
        found = current
    return (found, SETTLED) if found else ([], PENDING)


async def record_memories(
    *,
    agent: str,
    identity: EverosIdentity,
    resolve_session_id: Callable[[], Awaitable[str | None]],
    write: Callable[[str], Awaitable[None]],
    budget_s: float = _DEFAULT_BUDGET_S,
    client: httpx.AsyncClient | None = None,
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
        identity (`EverosIdentity`):
            Whose memories to read.
        resolve_session_id (`Callable[[], Awaitable[str | None]]`):
            Yields the join key, or ``None`` when this call has none (a
            stateless agent, or an id that was never read back). ``None``
            writes no file: there is nothing truthful to say.
        write (`Callable[[str], Awaitable[None]]`):
            Receives the record's JSON text.
        budget_s (`float`):
            How long to keep looking for a memory everos has not extracted yet.
        client (`httpx.AsyncClient | None`):
            Injected in tests; otherwise one is built and closed here.
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
    owned = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(_HTTP_TIMEOUT_S))
    try:
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
                logger.warning("Memory record for {} could not prime everos: {}", agent, exc)
                primed = False
        if not primed:
            # Nothing landed, so nothing can have been extracted. Polling would
            # spend the whole budget confirming an absence already known.
            items, status = [], UNAVAILABLE
        else:
            try:
                items, status = await _poll(http, identity, session_id, budget_s)
            except Exception as exc:  # noqa: BLE001 - an unreachable everos is a status, not a failure
                logger.warning("Memory record for {} could not read everos at {}: {}", agent, identity.base_url, exc)
                items, status = [], UNAVAILABLE
        payload = {
            "agent": agent,
            **({"instance": instance} if instance else {}),
            "source": identity.source,
            "status": status,
            "memories": [{"type": item.type, "text": item.text} for item in items],
        }
        logger.debug(
            "Memory record for {} ({}): {} item(s) under {} at {}",
            agent,
            status,
            len(items),
            session_id,
            identity.base_url,
        )
        try:
            await write(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("Memory record for {} could not be written: {}", agent, exc)
    finally:
        if owned:
            await http.aclose()
