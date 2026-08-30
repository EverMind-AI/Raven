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

from raven.rpc.errors import ConfigValidationError, InternalError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher

_HTTP_TIMEOUT_S = 15.0
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


async def memory_stats(params: dict) -> dict:
    """``memory.stats`` — never raises; the page opens even when EverOS is down."""
    del params
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
            return {"items": items, "total": len(items), "page": 1, "page_size": page_size}
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
            "total": int(data.get("total_count", len(items))),
            "page": page,
            "page_size": page_size,
        }
    except ConfigValidationError:
        raise
    except Exception as e:  # noqa: BLE001 — surface as a typed RPC error
        raise InternalError(f"everos unreachable: {e}") from e


def _esc(value: str) -> str:
    return value.replace("'", "''")


async def _delete_in_process(kind: str, mem_id: str) -> int:
    """Delete one row via EverOS's own repository layer (see module doc)."""
    from raven.config.update_everos import configure_everos_env, ensure_everos_home

    configure_everos_env()
    ensure_everos_home()
    from everos.infra.persistence.lancedb import (
        agent_case_repo,
        agent_skill_repo,
        atomic_fact_repo,
        episode_repo,
        foresight_repo,
        user_profile_repo,
    )

    repo = {
        "episode": episode_repo,
        "profile": user_profile_repo,
        "agent_case": agent_case_repo,
        "agent_skill": agent_skill_repo,
    }[kind]

    removed = 1
    if kind == "episode":
        # One store → one memcell → episode / facts / foresight family.
        # Removing the episode alone would leave orphaned derived rows that
        # recall can still surface, so the family goes together.
        row = await repo.get_by_id(mem_id)
        parent_id = getattr(row, "parent_id", None) if row else None
        await repo.delete(f"id = '{_esc(mem_id)}'")
        if parent_id:
            for child in (atomic_fact_repo, foresight_repo):
                try:
                    await child.delete(f"parent_id = '{_esc(parent_id)}'")
                except Exception as e:  # noqa: BLE001 — family cleanup is best-effort
                    logger.warning("memory.delete: cascade on {} failed: {}", child, e)
    else:
        await repo.delete(f"id = '{_esc(mem_id)}'")
    return removed


async def memory_delete(params: dict) -> dict:
    kind = str(params.get("kind") or "")
    mem_id = str(params.get("id") or "")
    if kind not in _KINDS:
        raise ConfigValidationError(f"unknown memory kind: {kind!r}")
    if not mem_id:
        raise ConfigValidationError("id is required")
    try:
        removed = await _delete_in_process(kind, mem_id)
    except Exception as e:  # noqa: BLE001 — surface as a typed RPC error
        raise InternalError(f"delete failed: {e}") from e
    logger.info("memory.delete: removed {} {}", kind, mem_id)
    return {"ok": True, "removed": removed}


def register_memory_methods(dispatcher: "Dispatcher") -> None:
    dispatcher.register("memory.stats", memory_stats)
    dispatcher.register("memory.list", memory_list)
    dispatcher.register("memory.delete", memory_delete)


__all__ = [
    "memory_delete",
    "memory_list",
    "memory_stats",
    "register_memory_methods",
]
