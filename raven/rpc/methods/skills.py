"""``skills.manage`` RPC handler -- the TUI's ``/skills`` surface and skills hub.

One method with an ``action`` discriminator, because that is the shape ui-tui
already calls from both ``slash/commands/ops.ts`` and ``components/skillsHub.tsx``.

Two backing sources, and the split matters:

* ``list`` / ``inspect`` read the on-disk :class:`SkillRegistry` -- what this
  agent can actually load this turn, grouped by physical origin.
* ``browse`` / ``install`` are Skill Hub calls, and are only available when a
  hub endpoint is configured. ``search`` spans both, because a user looking for
  a skill by name does not care which side of that line it is on yet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.rpc.errors import ConfigValidationError, InternalError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory


# The hub search endpoint takes a limit but no offset, so paging is done here
# over one bounded fetch. The ceiling keeps a "browse page 40" from pulling the
# whole catalogue; `total` reports what was actually fetched, never a claim
# about the catalogue's real size.
_PAGE_SIZE = 20
_MAX_FETCH = 200


def _loop(agent_loop_factory: "AgentLoopFactory | None") -> Any:
    return agent_loop_factory() if agent_loop_factory is not None else None


def _registry(agent_loop_factory: "AgentLoopFactory | None") -> Any:
    """The live on-disk skill registry, or a typed error naming why there is none."""
    loop = _loop(agent_loop_factory)
    skills = getattr(getattr(loop, "context", None), "skills", None) if loop is not None else None
    registry = getattr(skills, "registry", None) if skills is not None else None
    if registry is None:
        raise InternalError("no live skill registry")
    return registry


def _hub(agent_loop_factory: "AgentLoopFactory | None") -> Any:
    """The shared Skill Hub client, or ``None`` when no endpoint is configured.

    Unconfigured is a normal state, not a fault: the caller decides whether its
    action can degrade (``search``) or has to report it (``browse``/``install``).
    """
    loop = _loop(agent_loop_factory)
    return getattr(loop, "_skill_hub_client", None) if loop is not None else None


def _all_local(agent_loop_factory: "AgentLoopFactory | None") -> list[Any]:
    return list(_registry(agent_loop_factory).list_all())


async def _action_list(_query: str, _page: int, factory: "AgentLoopFactory | None") -> dict:
    """Skill names grouped by origin -- the panel renders one section per group."""
    grouped: dict[str, list[str]] = {}
    for meta in _all_local(factory):
        grouped.setdefault(meta.source or "local", []).append(meta.name)
    return {"skills": {group: sorted(names) for group, names in sorted(grouped.items())}}


async def _action_inspect(query: str, _page: int, factory: "AgentLoopFactory | None") -> dict:
    """One skill's metadata.

    An unknown name returns an empty ``info`` rather than an error: the client
    reads a missing ``info.name`` as "unknown skill" and says so itself.
    """
    registry = _registry(factory)
    meta = registry.get(query)
    if meta is None:
        # ``get`` is exact; a user typing a name off the list panel may have the
        # case wrong, which is not the same as the skill not existing.
        lowered = query.lower()
        meta = next((m for m in registry.list_all() if m.name.lower() == lowered), None)
    if meta is None:
        return {"info": {}}
    skill_dir = _skill_dir(meta)
    hub, hub_id = _hub_marker(skill_dir)
    body_path = skill_dir / "SKILL.md"
    return {
        "info": {
            "name": meta.name,
            "description": meta.description,
            "category": meta.source,
            "path": str(meta.path),
            "body": body_path.read_text(encoding="utf-8") if body_path.is_file() else "",
            "files": sorted(p.name for p in skill_dir.iterdir() if p.is_file() and not p.name.startswith("."))
            if skill_dir.is_dir()
            else [],
            "always": bool(getattr(meta, "always", False)),
            "hub": hub,
            "hub_id": hub_id,
            "install": _install_meta(skill_dir),
        }
    }


def _skill_dir(meta: Any) -> Path:
    """The directory a skill lives in; ``meta.path`` names its SKILL.md."""
    p = Path(meta.path)
    return p.parent if p.suffix == ".md" or p.is_file() else p


def _hub_marker(skill_dir: Path) -> tuple[bool, str]:
    """Whether the hub installed this skill, and the hub's id for it.

    The market is an optional install; without it nothing is hub-installed.
    """
    try:
        from raven.skill_hub.hub import MARKER
    except ImportError:
        return False, ""
    marker = skill_dir / MARKER
    if not marker.is_file():
        return False, ""
    try:
        return True, str(json.loads(marker.read_text(encoding="utf-8")).get("id") or "")
    except (OSError, ValueError):
        return True, ""


def _install_meta(skill_dir: Path) -> dict | None:
    """The install stamp the hub leaves, or None for a skill nobody installed."""
    path = skill_dir / ".install-meta.json"
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {
        "installed_at": record.get("installed_at"),
        "version": str(record.get("version") or ""),
        "trigger": str(record.get("trigger") or ""),
        "source": str(record.get("source") or ""),
        "score_safety": record.get("score_safety"),
    }


async def _action_open(query: str, factory: "AgentLoopFactory | None", file: str) -> dict:
    """Open one file of a skill with the desktop's opener, on the gateway host.

    Only files inside the skill's own directory: the skill name picks the
    directory, ``file`` is relative to it, and a path that resolves outside is
    refused before anything is launched.
    """
    registry = _registry(factory)
    meta = registry.get(query)
    if meta is None:
        raise ConfigValidationError(f"unknown skill {query!r}")
    root = _skill_dir(meta).resolve()
    target = (root / (file or "SKILL.md")).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ConfigValidationError("file must be a file inside the skill directory")
    from raven.rpc.methods import console

    console._open_with_system(target)
    return {"opened": True}


async def _action_search(query: str, _page: int, factory: "AgentLoopFactory | None") -> dict:
    """Name/description matches, local first, then hub hits not already installed."""
    needle = query.lower()
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    for meta in _all_local(factory):
        if needle in meta.name.lower() or needle in (meta.description or "").lower():
            results.append({"name": meta.name, "description": meta.description})
            seen.add(meta.name.lower())

    hub = _hub(factory)
    if hub is not None and query:
        try:
            for item in await hub.search(query, limit=_PAGE_SIZE):
                name = str(item.get("slug") or item.get("name") or "")
                if name and name.lower() not in seen:
                    results.append({"name": name, "description": str(item.get("description") or "")})
                    seen.add(name.lower())
        except Exception as exc:
            # A hub that is down must not take the local half of the answer with
            # it -- the local results above are already usable.
            logger.debug("skills.manage search: hub unreachable: {}", exc)

    return {"results": results}


async def _action_browse(_query: str, page: int, factory: "AgentLoopFactory | None") -> dict:
    """One page of the hub catalogue."""
    hub = _hub(factory)
    if hub is None:
        raise InternalError("no skill hub configured")
    try:
        items = await hub.search("", limit=_MAX_FETCH)
    except Exception as exc:
        raise InternalError(f"skill hub unreachable: {exc}") from exc

    total = len(items)
    total_pages = max(1, -(-total // _PAGE_SIZE))
    page = min(max(page, 1), total_pages)
    start = (page - 1) * _PAGE_SIZE
    return {
        "items": [
            {
                "name": str(item.get("slug") or item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "source": str(item.get("source") or "hub"),
                "trust": str(item.get("trust") or ""),
            }
            for item in items[start : start + _PAGE_SIZE]
        ],
        "page": page,
        "total": total,
        "total_pages": total_pages,
    }


async def _action_install(query: str, _page: int, factory: "AgentLoopFactory | None") -> dict:
    """Fetch a hub skill into the workspace skill tree."""
    hub = _hub(factory)
    if hub is None:
        raise InternalError("no skill hub configured")
    try:
        installed = await hub.install(query)
    except Exception as exc:
        raise InternalError(f"install failed: {exc}") from exc

    # The registry caches its scan, so a freshly extracted bundle stays
    # invisible until the cache is dropped -- installing something the agent
    # then cannot load is the whole failure this avoids.
    try:
        _registry(factory).invalidate_cache()
    except InternalError:
        pass
    return {"installed": True, "name": str(installed.get("slug") or query)}


_ACTIONS = {
    "list": _action_list,
    "inspect": _action_inspect,
    "search": _action_search,
    "browse": _action_browse,
    "install": _action_install,
}


async def skills_manage(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``skills.manage`` -- dispatch on ``action``."""
    action = str(params.get("action") or "").strip().lower()
    if action == "open":
        return await _action_open(
            str(params.get("query") or "").strip(), agent_loop_factory, str(params.get("file") or "")
        )
    handler = _ACTIONS.get(action)
    if handler is None:
        raise InternalError(f"unknown skills action: {action or '(missing)'}")
    page = params.get("page")
    return await handler(
        str(params.get("query") or "").strip(),
        page if isinstance(page, int) and not isinstance(page, bool) else 1,
        agent_loop_factory,
    )


def register_skills_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    """Register ``skills.manage`` on a dispatcher instance."""

    async def _manage(params: dict) -> dict:
        return await skills_manage(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("skills.manage", _manage)


__all__ = ["skills_manage", "register_skills_methods"]
