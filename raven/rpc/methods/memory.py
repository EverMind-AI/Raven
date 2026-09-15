"""``memory.*`` RPC handlers -- the GUI's data & memory surface.

Three methods back the memory page:

* ``memory.stats``  — per-kind totals for the four EverOS memory kinds.
* ``memory.list``   — paginated listing (``/get``) or semantic search
  (``/search``) over one kind, projected to card-sized rows.
* ``memory.delete`` — remove one memory row, with the episode family
  cascade (facts / foresights sharing the episode's memcell).

Read paths go over EverOS's HTTP API (the same server the memory
backend talks to), so the service boundary stays intact. The delete
path is the one exception: EverOS 1.1.3 exposes no delete endpoint, so
``memory.delete`` uses EverOS's own repository layer in-process against
the same index (LanceDB commits are atomic + optimistically concurrent,
so a raven-side delete and a server-side write do not corrupt each
other). When upstream grows ``DELETE /api/v1/memory/...`` this handler
should switch to it.

Kind names follow EverOS's ``/get`` contract verbatim: ``episode`` /
``profile`` (user track) and ``agent_case`` / ``agent_skill`` (agent
track). The owner id for each track comes from ``memory.user_id`` /
``memory.agent_id`` in raven's config — the same identities the backend
stamps on stored turns, so the page sees exactly what recall sees.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.core.plugin_stack import everos_plugin_installed, everos_plugin_missing_note
from raven.rpc.errors import ConfigValidationError, InternalError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher

_HTTP_TIMEOUT_S = 15.0
# The backend contribution whose store this page reads. Named once: the page
# is EverOS-shaped down to its four tabs, so every check for "is this page
# looking at the right store" has to mean the same thing.
_EVEROS_BACKEND = "everos"

_USER_KINDS = ("episode", "profile")
_AGENT_KINDS = ("agent_case", "agent_skill")
_KINDS = _USER_KINDS + _AGENT_KINDS

# /search answers per track, not per kind; this maps a kind to the array
# holding it in SearchData / GetData.
_KIND_FIELD = {
    "episode": "episodes",
    "profile": "profiles",
    "agent_case": "agent_cases",
    "agent_skill": "agent_skills",
}


def _cfg() -> tuple[str, str, str]:
    """(base_url, user_id, agent_id) from raven's config.

    ``plugins`` / ``memory`` live on the composed :class:`RavenConfig`,
    not the base channels ``Config`` — hence ``load_raven_config``.
    """
    from raven.config.raven import load_raven_config
    from raven_everos.server import DEFAULT_EVEROS_BASE_URL

    cfg = load_raven_config()
    plug = (cfg.plugins.config or {}).get("everos-memory", {})
    base_url = str(plug.get("base_url") or DEFAULT_EVEROS_BASE_URL).rstrip("/")
    user_id = cfg.memory.user_id or "default"
    agent_id = cfg.memory.agent_id or "default"
    return base_url, user_id, agent_id


async def _post(base_url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(_HTTP_TIMEOUT_S)) as client:
        r = await client.post(f"{base_url}{path}", json=body)
        r.raise_for_status()
        return r.json() or {}


def _owner_body(kind: str, user_id: str, agent_id: str) -> dict[str, Any]:
    if kind in _USER_KINDS:
        return {"user_id": user_id}
    return {"agent_id": agent_id}


def _iso(v: Any) -> str | None:
    return str(v) if v else None


def _project(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    """Card-sized projection of one /get | /search row."""
    out: dict[str, Any] = {"id": row.get("id", ""), "kind": kind}
    if "score" in row and row.get("score") is not None:
        out["score"] = float(row["score"])
    if kind == "episode":
        out.update(
            session_id=row.get("session_id"),
            timestamp=_iso(row.get("timestamp")),
            subject=row.get("subject") or "",
            summary=row.get("summary") or "",
            body=row.get("episode") or "",
        )
    elif kind == "profile":
        out.update(profile_data=row.get("profile_data") or {})
    elif kind == "agent_case":
        out.update(
            session_id=row.get("session_id"),
            timestamp=_iso(row.get("timestamp")),
            subject=row.get("task_intent") or "",
            body=row.get("approach") or "",
            key_insight=row.get("key_insight"),
            quality_score=row.get("quality_score"),
        )
    else:  # agent_skill
        out.update(
            subject=row.get("name") or "",
            summary=row.get("description") or "",
            body=row.get("content") or "",
            confidence=row.get("confidence"),
            maturity_score=row.get("maturity_score"),
        )
    return out


def _unavailable_note() -> str | None:
    """Why this page has nothing to show, in a sentence, or ``None``.

    Two ways to arrive at an empty memory browser that are not a failure and
    that a person cannot tell apart from one: the plugin is not installed, and
    the plugin is installed but is not what ``memory.backend`` names. Both used
    to render as four zeros or a retry button, which reads as "your memories
    are gone" rather than "this page is not where they are".
    """
    from raven.config.raven import load_raven_config

    if not everos_plugin_installed():
        return everos_plugin_missing_note()
    try:
        backend = load_raven_config().memory.backend
    except Exception:  # noqa: BLE001 - an unreadable config is not this page's to report
        return None
    if backend == _EVEROS_BACKEND:
        return None
    if not backend:
        return "Long-term memory is turned off, so there is nothing stored to browse."
    return (
        f"Long-term memory runs on {backend!r}, and this page reads EverOS's store only. "
        "Nothing here is what recall uses."
    )


async def memory_stats(params: dict) -> dict:
    """``memory.stats`` — never raises; the page opens even when EverOS is down."""
    del params
    note = _unavailable_note()
    if note is not None:
        # Same shape as an unreachable server plus the one thing that shape
        # could never carry: why. Without it the page says zero and leaves the
        # reader to guess between "not installed", "not the configured
        # backend", and "your memories are gone".
        logger.warning("memory.stats: {}", note)
        return {
            "ok": False,
            "note": note,
            "base_url": "",
            "episodes": 0,
            "profiles": 0,
            "agent_cases": 0,
            "agent_skills": 0,
        }
    base_url, user_id, agent_id = _cfg()
    counts: dict[str, int] = {}
    ok = True
    for kind in _KINDS:
        body = _owner_body(kind, user_id, agent_id) | {
            "memory_type": kind,
            "page": 1,
            "page_size": 1,
        }
        try:
            payload = await _post(base_url, "/api/v1/memory/get", body)
            counts[kind] = int((payload.get("data") or {}).get("total_count", 0))
        except Exception as e:  # noqa: BLE001 — stats degrade, never break the page
            logger.warning("memory.stats: {} unavailable ({})", kind, e)
            counts[kind] = 0
            ok = False
    return {
        "ok": ok,
        "note": None,
        "base_url": base_url,
        "episodes": counts["episode"],
        "profiles": counts["profile"],
        "agent_cases": counts["agent_case"],
        "agent_skills": counts["agent_skill"],
    }


async def memory_list(params: dict) -> dict:
    kind = str(params.get("kind") or "")
    if kind not in _KINDS:
        raise ConfigValidationError(f"unknown memory kind: {kind!r}")
    note = _unavailable_note()
    if note is not None:
        # An empty page carrying the reason, not an error. This is not a
        # failure -- there is simply no EverOS store to list here -- and a
        # retry button offers an action that cannot help.
        return {"items": [], "total": 0, "page": 1, "page_size": 0, "note": note}
    page = max(1, int(params.get("page") or 1))
    page_size = min(100, max(1, int(params.get("page_size") or 20)))
    q = str(params.get("q") or "").strip()
    base_url, user_id, agent_id = _cfg()
    owner = _owner_body(kind, user_id, agent_id)

    try:
        if q:
            payload = await _post(
                base_url,
                "/api/v1/memory/search",
                owner | {"query": q, "top_k": page_size},
            )
            rows = (payload.get("data") or {}).get(_KIND_FIELD[kind]) or []
            items = [_project(kind, r) for r in rows]
            return {"items": items, "total": len(items), "page": 1, "page_size": page_size, "note": None}
        payload = await _post(
            base_url,
            "/api/v1/memory/get",
            owner | {"memory_type": kind, "page": page, "page_size": page_size},
        )
        data = payload.get("data") or {}
        rows = data.get(_KIND_FIELD[kind]) or []
        items = [_project(kind, r) for r in rows]
        return {
            "items": items,
            "note": None,
            "total": int(data.get("total_count", len(items))),
            "page": page,
            "page_size": page_size,
        }
    except ConfigValidationError:
        raise
    except Exception as e:  # noqa: BLE001 — surface as a typed RPC error
        raise InternalError(f"everos unreachable: {e}") from e


def _memory_backend(agent_loop_factory):
    """The backend this deployment is running, for a call that is not a turn.

    The live loop's own instance when the gateway has one, so a delete goes
    through the object that has the service running; otherwise one built the
    way ``raven doctor`` builds it, since the browser answers in processes
    that never assembled a loop.
    """
    from raven.config.raven import load_raven_config

    loop = agent_loop_factory() if agent_loop_factory is not None else None
    backend = getattr(loop, "backend", None) if loop is not None else None
    if backend is not None:
        return backend

    from raven.config import load_config
    from raven.core.plugin_stack import maybe_build_memory_backend

    return maybe_build_memory_backend(load_config().workspace_path, load_raven_config())


async def memory_delete(params: dict, *, agent_loop_factory=None) -> dict:
    """Remove one memory through the backend that owns it.

    ``kind`` travels to the backend as the opaque string the listing handed
    out. The host does not know how any backend stores a memory, and the one
    time it acted as though it did -- deleting the row out of EverOS's index
    while its markdown, the source of truth, kept the text -- the memory came
    back on the next rebuild of that file, after the user had been told it was
    gone.
    """
    kind = str(params.get("kind") or "")
    mem_id = str(params.get("id") or "")
    if kind not in _KINDS:
        raise ConfigValidationError(f"unknown memory kind: {kind!r}")
    if not mem_id:
        raise ConfigValidationError("id is required")

    backend = _memory_backend(agent_loop_factory)
    if backend is None:
        raise InternalError(
            everos_plugin_missing_note() if not everos_plugin_installed() else "no memory backend is configured"
        )
    try:
        removed = await backend.delete(mem_id, kind=kind)
    except Exception as e:  # noqa: BLE001 - surface as a typed RPC error
        raise InternalError(f"delete failed: {e}") from e
    if not removed:
        raise InternalError(f"this backend cannot delete a {kind} memory")
    logger.info("memory.delete: removed {} {}", kind, mem_id)
    return {"ok": True, "removed": 1}


def register_memory_methods(dispatcher: "Dispatcher", *, agent_loop_factory=None) -> None:
    dispatcher.register("memory.stats", memory_stats)
    dispatcher.register("memory.list", memory_list)

    async def _delete(params: dict) -> dict:
        return await memory_delete(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("memory.delete", _delete)


__all__ = [
    "memory_delete",
    "memory_list",
    "memory_stats",
    "register_memory_methods",
]
