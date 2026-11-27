"""``skills.manage`` RPC handler -- the TUI's ``/skills`` surface and skills hub.

One method with an ``action`` discriminator, because that is the shape ui-tui
already calls from both ``slash/commands/ops.ts`` and ``components/skillsHub.tsx``.

Two backing sources, and the split matters:

* ``list`` / ``inspect`` read the on-disk :class:`SkillRegistry` -- what this
  agent can actually load this turn, grouped by physical origin.
* ``browse`` / ``install`` are Skill Hub calls, and are only available when a
  hub endpoint is configured. ``search`` spans both, because a user looking for
  a skill by name does not care which side of that line it is on yet.
* ``block`` / ``unblock`` write the operator blocklist. They live here rather
  than behind the generic settings writer because the match is a policy
  decision: the config module trims and casefolds before it compares, and a
  page that reproduced that fold in JavaScript would disagree on names such as
  ``Straße`` and ``STRASSE``.
"""

from __future__ import annotations

import asyncio
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
    """The shared Skill Hub client, or ``None`` while no loop is serving.

    Every loop builds one whatever the switch says; the callers that reach the
    Hub ask the live config before each call, so "off" is a gated client rather
    than an absent one.
    """
    loop = _loop(agent_loop_factory)
    return getattr(loop, "_skill_hub_client", None) if loop is not None else None


def _all_local(agent_loop_factory: "AgentLoopFactory | None") -> list[Any]:
    return list(_registry(agent_loop_factory).list_all())


def _agent_home(agent_loop_factory: "AgentLoopFactory | None") -> Path:
    loop = _loop(agent_loop_factory)
    ws = getattr(loop, "workspace", None) if loop is not None else None
    if ws is not None:
        return Path(ws).resolve()
    from raven.config.loader import load_config

    return load_config().workspace_path.resolve()


def _blocklist(agent_loop_factory: "AgentLoopFactory | None") -> frozenset[str]:
    loop = _loop(agent_loop_factory)
    catalog = getattr(getattr(loop, "context", None), "skills", None) if loop is not None else None
    if catalog is not None and hasattr(catalog, "blocklist"):
        return frozenset(catalog.blocklist)
    from raven.config.loader import get_config_path, read_raw_or_raise
    from raven.skill_hub.policy import normalize_blocklist

    try:
        raw = read_raw_or_raise(get_config_path())
    except Exception:
        return frozenset()
    sf = raw.get("skillForge") if isinstance(raw.get("skillForge"), dict) else raw.get("skill_forge")
    items = sf.get("blocklist") if isinstance(sf, dict) and isinstance(sf.get("blocklist"), list) else []
    return normalize_blocklist([str(x) for x in items if isinstance(x, str)])


def _resolve_project_dir(project_dir: str) -> Path:
    raw = project_dir.strip()
    if not raw:
        raise ConfigValidationError("project_dir is required")
    p = Path(raw).expanduser()
    try:
        if not p.is_dir():
            raise ConfigValidationError(f"project_dir is not an existing directory: {raw}")
        return p.resolve()
    except OSError as exc:
        raise ConfigValidationError(f"project_dir unreadable: {exc}") from exc


def _project_skills_for_dir(project_dir: str, factory: "AgentLoopFactory | None") -> tuple[Path, Any, list[Any]]:
    from raven.memory_engine import project_skill_roots, read_project_skills

    resolved = _resolve_project_dir(project_dir)
    roots = project_skill_roots(resolved, agent_home=_agent_home(factory))
    metas = read_project_skills(roots) if roots is not None else []
    return resolved, roots, metas


def _safe_project_files(skill_dir: Path, roots: Any) -> list[str]:
    if not skill_dir.is_dir():
        return []
    root = skill_dir.resolve()
    out: list[str] = []
    for p in sorted(skill_dir.iterdir(), key=lambda x: x.name):
        if p.name.startswith("."):
            continue
        try:
            resolved = p.resolve()
        except (OSError, RuntimeError):
            continue
        if not resolved.is_file() or not resolved.is_relative_to(root):
            continue
        if roots is not None:
            if not resolved.is_relative_to(roots.boundary):
                continue
            if any(resolved.is_relative_to(g) for g in roots.guarded):
                continue
        out.append(p.name)
    return out


async def _action_project_list(project_dir: str, factory: "AgentLoopFactory | None") -> dict:
    """List project skills discovered under ``.agents/skills`` for a chosen project directory."""
    from raven.config.live import project_skills_on
    from raven.config.loader import get_config_path, read_raw_or_raise
    from raven.skill_hub.policy import is_blocked

    resolved, roots, metas = await asyncio.to_thread(_project_skills_for_dir, project_dir, factory)
    blocked_now = _blocklist(factory)
    try:
        enabled = project_skills_on(read_raw_or_raise(get_config_path()))
    except Exception:
        enabled = False
    return {
        "project_dir": str(resolved),
        "boundary": str(roots.boundary) if roots is not None else str(resolved),
        "roots": [str(r) for r in roots.dirs] if roots is not None else [],
        "project_skills_enabled": enabled,
        "project_skills": [
            {
                "name": m.name,
                "description": (m.description or "")[:200],
                "source": "project",
                "path": str(m.path),
                "always": False,
                "hub": False,
                "hub_id": "",
                "blocked": is_blocked(blocked_now, m.name),
            }
            for m in metas
        ],
    }


async def _action_list(_query: str, _page: int, factory: "AgentLoopFactory | None") -> dict:
    """Skill names grouped by origin -- the panel renders one section per group."""
    grouped: dict[str, list[str]] = {}
    for meta in _all_local(factory):
        grouped.setdefault(meta.source or "local", []).append(meta.name)
    return {"skills": {group: sorted(names) for group, names in sorted(grouped.items())}}


async def _action_inspect(
    query: str,
    _page: int,
    factory: "AgentLoopFactory | None",
    project_dir: str = "",
) -> dict:
    """One skill's metadata.

    An unknown name returns an empty ``info`` rather than an error: the client
    reads a missing ``info.name`` as "unknown skill" and says so itself.
    """
    if project_dir:
        _, roots, metas = await asyncio.to_thread(_project_skills_for_dir, project_dir, factory)
        meta = next((m for m in metas if m.name == query), None)
        if meta is None:
            lowered = query.lower()
            meta = next((m for m in metas if m.name.lower() == lowered), None)
        if meta is None:
            return {"info": {}}
        skill_dir = _skill_dir(meta)
        body_path = Path(meta.path)
        return {
            "info": {
                "name": meta.name,
                "description": meta.description,
                "category": "project",
                "path": str(meta.path),
                "body": body_path.read_text(encoding="utf-8") if body_path.is_file() else "",
                "files": _safe_project_files(skill_dir, roots),
                "always": False,
                "hub": False,
                "hub_id": "",
                "install": None,
            }
        }
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


async def _action_open(
    query: str,
    factory: "AgentLoopFactory | None",
    file: str,
    project_dir: str = "",
) -> dict:
    """Open one file of a skill with the desktop's opener, on the gateway host.

    Only files inside the skill's own directory: the skill name picks the
    directory, ``file`` is relative to it, and a path that resolves outside is
    refused before anything is launched.
    """
    roots = None
    if project_dir:
        _, roots, metas = await asyncio.to_thread(_project_skills_for_dir, project_dir, factory)
        meta = next((m for m in metas if m.name == query), None)
    else:
        registry = _registry(factory)
        meta = registry.get(query)
    if meta is None:
        raise ConfigValidationError(f"unknown skill {query!r}")
    root = _skill_dir(meta).resolve()
    target = (root / (file or "SKILL.md")).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ConfigValidationError("file must be a file inside the skill directory")
    if roots is not None:
        if not target.is_relative_to(roots.boundary) or any(target.is_relative_to(g) for g in roots.guarded):
            raise ConfigValidationError("file must be a file inside the skill directory")
    from raven.rpc.methods import console

    console._open_with_system(target)
    return {"opened": True}


async def _action_block(query: str, blocked: bool) -> dict:
    """Add or remove one name on ``skillForge.blocklist``.

    ``set_skill_blocked`` is the writer the CLI uses, and it is the only place
    that compares names: it trims and casefolds each entry, so ``STRASSE``
    blocks ``Straße`` and an unblock of ``Straße`` removes it. Handing the
    name over as the registry spells it keeps that decision on the server,
    where the runtime reads it, instead of asking every page to reproduce
    Python's casefold. The write is atomic, so two switches flipped at once
    cannot lose each other's change the way a read-modify-write of the whole
    list from the page can.
    """
    if not query:
        raise ConfigValidationError("block needs a skill name")
    from raven.config.update import set_skill_blocked

    try:
        names = await asyncio.to_thread(set_skill_blocked, query, blocked)
    except ValueError as exc:
        raise ConfigValidationError(str(exc)) from exc
    return {"name": query, "blocked": blocked, "blocklist": names}


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
    project_dir = str(params.get("project_dir") or "").strip()
    if action == "project_list":
        return await _action_project_list(project_dir, agent_loop_factory)
    if action == "inspect":
        return await _action_inspect(
            str(params.get("query") or "").strip(),
            1,
            agent_loop_factory,
            project_dir=project_dir,
        )
    if action == "open":
        return await _action_open(
            str(params.get("query") or "").strip(),
            agent_loop_factory,
            str(params.get("file") or ""),
            project_dir=project_dir,
        )
    if action in ("block", "unblock"):
        return await _action_block(str(params.get("query") or "").strip(), action == "block")
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
