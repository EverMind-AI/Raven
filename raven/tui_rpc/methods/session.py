"""session.* RPC handlers (lifecycle + management).

``session.create`` mints a fresh ``tui:<chat_id>`` key on every call (lazy —
no file is written until the session's first save). ``session.resume`` loads
the stored transcript from disk for a known ``session_id`` and falls back to a
fresh-minted key with empty messages for an unknown or absent id.
``session.close`` flushes any unpersisted messages of the named session.
``session.list`` returns tui-channel sessions sorted by updated_at desc.
``session.delete`` removes a session file and invalidates the cache.
``session.most_recent`` wraps find_most_recent_chat_id("tui").
``session.title`` sets or gets the title field in session metadata (lazy —
title persists on the next save that writes metadata).

Wire shape for session.create/resume: the ``info`` field is the init bundle
consumed by ``ui-tui/src/components/branding.tsx`` (SessionPanel). Requires
``info.skills`` / ``info.tools`` / ``info.model`` — Object.entries(info.skills)
on line 138 will throw if these are missing.

``agent_loop=None`` graceful fallback: empty tools/skills, zero usage,
``lazy=True``. Mirrors ``turn.py``'s factory-exception guard.

Known divergence: ``system.hello`` still advertises ``default_session_key``
``tui:default`` and the ui-tui turn path still hardcodes it.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from raven.config.loader import load_config
from raven.session.export import default_export_path, write_transcript
from raven.session.manager import SessionManager, new_chat_id
from raven.token_wise.pricing import resolve_context_window
from raven.tui_rpc.errors import TurnInProgressError
from raven.tui_rpc.methods import turn as turn_module
from raven.tui_rpc.methods.system import _raven_version

if TYPE_CHECKING:
    from raven.agent.loop.main import AgentLoop
    from raven.config.schema import Config
    from raven.tui_rpc.dispatcher import Dispatcher


AgentLoopFactory = Callable[[], "AgentLoop | None"]


# Cache the package version once at module load — importlib.metadata.version
# walks site-packages dist-info on every call. system._raven_version()
# already guards PackageNotFoundError for source-checkout environments.
_RAVEN_VERSION = _raven_version()


def _safe_invoke_factory(
    factory: "AgentLoopFactory | None",
) -> "AgentLoop | None":
    """Invoke ``factory()`` with the same try/except guard ``turn.py`` uses.

    Boot races, transient construction failures, or any other factory-raises
    path must degrade to ``agent_loop=None`` (lazy bundle) rather than crash
    the banner. Mirrors ``turn.py::turn_send`` lines 103-109.
    """
    if factory is None:
        return None
    try:
        return factory()
    except Exception:
        logger.exception("session.*: agent_loop_factory raised")
        return None


def _enumerate_tools(agent_loop: "AgentLoop | None") -> dict[str, list[str]]:
    """Banner ``info.tools`` subfield — single ``"builtin"`` bucket per handoff §3.4."""
    if agent_loop is None:
        return {}
    return {"builtin": sorted(agent_loop.tools.tool_names)}


def _enumerate_skills(agent_loop: "AgentLoop | None") -> dict[str, list[str]]:
    """Banner ``info.skills`` subfield — group by ``source``.

    ``LocalSkillCatalog.list_skills(filter_unavailable=True)`` returns the
    legacy drop-in shape ``list[dict[str, str]]`` (``{name, path, source}``),
    not :class:`SkillMeta` instances.
    """
    if agent_loop is None:
        return {}
    skills = agent_loop.context.skills.list_skills(filter_unavailable=True)
    grouped: dict[str, list[str]] = {}
    for skill in skills:
        grouped.setdefault(skill["source"], []).append(skill["name"])
    return {source: sorted(names) for source, names in grouped.items()}


def _baseline_usage(
    agent_loop: "AgentLoop | None",
    config: "Config",
) -> dict[str, Any]:
    """Banner ``info.usage`` subfield — boot baseline (no turn has run yet).

    All counters are zero at session.create: a fresh session_key carries no
    prior LLM calls. Each turn's ``message.complete`` event updates them
    post-turn. ``context_max`` is the model's real window — live from the
    provider table when LiteLLM lags (e.g. OpenRouter), else config default.
    Usage starts at zero for a fresh session by design. Resume reuses the
    zero baseline; counters refresh on the next turn.
    """
    context_max = config.agents.defaults.context_window_tokens
    model = getattr(agent_loop, "model", None)
    if model:
        live_window = resolve_context_window(model)
        if live_window:
            context_max = live_window
    return {
        "input": 0,
        "output": 0,
        "cost_usd": 0.0,
        "calls": 0,
        "context_max": context_max,
        "context_used": 0,
        "context_percent": 0,
    }


def _default_session_info(
    agent_loop: "AgentLoop | None",
    config: "Config",
) -> dict[str, Any]:
    """Build the init bundle returned by ``session.create`` / ``session.resume``.

    ``agent_loop=None`` triggers graceful fallback (``tools={}``, ``skills={}``,
    zero usage, ``lazy=True``); version is always real (cached at module load).
    """
    model_id = config.agents.defaults.model
    return {
        "model": model_id,
        "model_id": model_id,
        "provider": config.agents.defaults.provider,
        "context_window": config.agents.defaults.context_window_tokens,
        "lazy": agent_loop is None,
        "skills": _enumerate_skills(agent_loop),
        "tools": _enumerate_tools(agent_loop),
        "usage": _baseline_usage(agent_loop, config),
        "version": _RAVEN_VERSION,
        "cwd": os.getcwd(),
        "mcp_servers": [],
    }


def _get_or_build_manager(config: "Config") -> SessionManager:
    """Return a ``SessionManager`` for the configured workspace.

    Module-level so tests can monkeypatch it to inject a pre-populated manager
    without touching the filesystem (same seam as ``load_config``).
    """
    return SessionManager(config.workspace_path)


def _manager_for(agent_loop: "AgentLoop | None", config: "Config") -> SessionManager:
    """Prefer the loop's shared manager when available; fall back to a fresh one."""
    if agent_loop is not None:
        mgr = getattr(agent_loop, "sessions", None)
        if isinstance(mgr, SessionManager):
            return mgr
    return _get_or_build_manager(config)


def _map_to_wire(messages: list[dict[str, Any]], session_key: str) -> list[dict[str, Any]]:
    """Map stored session messages to the GatewayTranscriptMessage wire shape.

    The TS side (``gatewayTypes.ts:23``) expects ``{role, text?, context?, name?}``.
    Stored messages carry ``content`` (not ``text``) so we rename the field.
    All well-formed stored messages are included (N stored → N wire) — no
    consolidation filter; non-dict or roleless entries are skipped with a
    warning so one corrupt line never bricks resume for the whole session.
    Multimodal user messages store LIST content (text/image blocks); the
    ``text`` fields of ``type == "text"`` blocks are joined and non-text
    blocks dropped. role="tool" entries pass through with name/context; known
    degradation: the TS renderer collapses them into a generic tool trail line
    attached to the next assistant message.
    """
    out = []
    for m in messages:
        if not isinstance(m, dict) or "role" not in m:
            logger.warning("session.resume: skipping malformed stored message in {}", session_key)
            continue
        entry: dict[str, Any] = {"role": m["role"]}
        content = m.get("content", "")
        if isinstance(content, list):
            entry["text"] = " ".join(
                blk.get("text", "") for blk in content if isinstance(blk, dict) and blk.get("type") == "text"
            )
        elif isinstance(content, str):
            entry["text"] = content
        elif content is not None:
            entry["text"] = str(content)
        for extra_key in ("context", "name"):
            if extra_key in m:
                entry[extra_key] = m[extra_key]
        out.append(entry)
    return out


async def session_create(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.create`` — invoke factory (guarded) and build init bundle.

    Zero-factory invocation (``session_create({})``) is the test/demo path
    and degrades to ``agent_loop=None`` fallback bundle. Production wires
    ``agent_loop_factory`` via :func:`register_session_methods`.

    A fresh ``tui:<chat_id>`` key is minted on every call (lazy — no file
    written until the session's first save). An optional ``title`` param
    is accepted and ignored here; clients set titles via ``session.title``.
    """
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    session_id = f"tui:{new_chat_id()}"
    return {
        "session_id": session_id,
        "info": _default_session_info(agent_loop, load_config()),
    }


async def session_close(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.close`` — flush any unpersisted messages for the given session.

    With per-turn saves the session is normally already fully persisted.
    This handler handles the edge case where a message was added after the
    last save. An absent or unknown ``session_id`` param is silently ignored.
    """
    session_key = params.get("session_id")
    if not session_key:
        return {"ok": True}
    config = load_config()
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    mgr = _manager_for(agent_loop, config)
    try:
        mgr.flush(session_key)
    except Exception:
        logger.warning("session.close: failed to flush {}", session_key)
    return {"ok": True}


async def session_resume(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.resume`` — load stored messages and return the resumed session key.

    Uses manager.peek() (consults cache first, then disk, without caching unknown
    keys). An unknown or absent session_id — or any load failure — falls back to
    a fresh-minted key with empty messages.

    Wire shape: raw session.messages so N stored → N wire (not get_history(),
    which slices and drops leading non-user messages).
    """
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    info = _default_session_info(agent_loop, config)
    session_key = params.get("session_id")

    if session_key:
        try:
            mgr = _manager_for(agent_loop, config)
            raw = mgr.peek(session_key)
            if raw is not None:
                return {
                    "session_id": session_key,
                    "info": info,
                    "messages": _map_to_wire(raw.messages, session_key),
                }
        except Exception:
            logger.exception(
                "session.resume: failed to load {}; falling back to fresh mint",
                session_key,
            )

    return {
        "session_id": f"tui:{new_chat_id()}",
        "info": info,
        "messages": [],
    }


def _session_to_list_item(info: dict[str, Any]) -> dict[str, Any]:
    """Convert a list_sessions entry to the SessionListItem wire shape.

    The TS SessionListItem (gatewayTypes.ts:130) requires:
      id, message_count, preview, started_at (unix timestamp), title.
    started_at maps from created_at ISO string; preview is always empty in
    v0.1 — metadata carries no message content, and the TS picker falls back
    to title or "(untitled)".
    """
    key = info.get("key", "")
    created_at_str = info.get("created_at") or ""
    started_at: float = 0.0
    if created_at_str:
        try:
            started_at = datetime.fromisoformat(created_at_str).timestamp()
        except ValueError:
            pass
    meta = info.get("metadata") or {}
    title = meta.get("title") or ""
    return {
        "id": key,
        "message_count": info.get("message_count", 0),
        "preview": "",
        "source": "tui",
        "started_at": started_at,
        "title": title,
    }


async def session_list(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.list`` — list tui-channel sessions sorted by updated_at desc.

    Filters to channel="tui" (this RPC scopes to the TUI surface). An optional
    positive integer ``limit`` slices after the sort (newest sessions win);
    zero, negative, or non-integer limits are ignored.
    Returns the SessionListResponse shape: {sessions: SessionListItem[]}.
    """
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    mgr = _manager_for(agent_loop, config)
    entries = mgr.list_sessions(channel="tui")
    limit = params.get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        entries = entries[:limit]
    return {"sessions": [_session_to_list_item(e) for e in entries]}


async def session_delete(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.delete`` — remove a session file and invalidate its cache entry.

    Returns {deleted: session_id} only when a file was actually removed;
    {deleted: null} otherwise (unknown id, missing param, or removal failure)
    so the UI can tell a typo from a real removal.
    """
    session_key = params.get("session_id", "")
    removed = False
    if session_key:
        agent_loop = _safe_invoke_factory(agent_loop_factory)
        config = load_config()
        mgr = _manager_for(agent_loop, config)
        removed = mgr.delete(session_key)
    return {"deleted": session_key if removed else None}


async def session_most_recent(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.most_recent`` — return the most-recently-updated tui session key.

    Returns the SessionMostRecentResponse shape: {session_id?: string | null, ...}.
    The TS caller (createGatewayEventHandler.ts:242) reads r?.session_id; null
    is the tolerated no-sessions value.
    """
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    mgr = _manager_for(agent_loop, config)
    chat_id = mgr.find_most_recent_chat_id("tui")
    session_id = f"tui:{chat_id}" if chat_id else None
    return {"session_id": session_id}


async def session_title(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.title`` — get or set the title of a session.

    Set path (``title`` param present): the title goes into the session's
    metadata via get_or_create. If the session file already exists on disk,
    it is persisted immediately (metadata-only save) and ``pending`` is
    False; for a never-saved lazy session the title stays in memory
    (``pending`` True — it lands with the session's first save, preserving
    the lazy mint).
    Get path: returns the current title from the cached or disk-loaded
    session.

    Wire shape per SessionTitleResponse (gatewayTypes.ts:154):
      {title?: string, session_key: string, pending: bool}
    """
    session_key = params.get("session_id", "")
    if not session_key:
        return {"title": None, "session_key": "", "pending": False}
    title = params.get("title")
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    mgr = _manager_for(agent_loop, config)

    if title is not None:
        session = mgr.get_or_create(session_key)
        session.metadata["title"] = title
        if mgr.exists(session_key):
            try:
                mgr.save(session)
            except Exception:
                logger.warning("session.title: failed to persist title for {}", session_key)
                return {"title": title, "session_key": session_key, "pending": True}
            return {"title": title, "session_key": session_key, "pending": False}
        return {"title": title, "session_key": session_key, "pending": True}

    raw = mgr.peek(session_key)
    current_title = None
    if raw is not None:
        current_title = (raw.metadata or {}).get("title")
    return {"title": current_title, "session_key": session_key, "pending": False}


async def session_clear(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.clear`` — wipe a session's messages in place, keeping its id.

    Unlike ``session.create`` (which mints a new id), clear preserves the
    session_key so scripts/bookmarks referencing it stay valid. Rejected
    while a turn is in flight (mutating history under a running writer races).
    """
    session_key = params.get("session_id", "")
    if not session_key:
        return {"session_id": "", "cleared": False}
    if turn_module.is_turn_active(session_key):
        raise TurnInProgressError(
            f"session {session_key!r} has an active turn; interrupt it before clearing",
            data={"session_key": session_key},
        )
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    mgr = _manager_for(agent_loop, config)
    session = mgr.get_or_create(session_key)
    session.clear()
    if mgr.exists(session_key):
        try:
            mgr.save(session)
        except Exception:
            logger.warning("session.clear: failed to persist cleared {}", session_key)
    return {"session_id": session_key, "cleared": True}


async def session_undo(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.undo`` — drop the last ``n`` turns (default 1) in place.

    Turn boundaries derive from the role=="user" boundary. Rejected while a
    turn is in flight. ``n`` is reserved for forward-compat; the ui-tui
    ``/undo`` and ``/retry`` commands send no ``n`` (default 1).
    """
    session_key = params.get("session_id", "")
    if not session_key:
        return {"removed": 0}
    if turn_module.is_turn_active(session_key):
        raise TurnInProgressError(
            f"session {session_key!r} has an active turn; interrupt it before undo",
            data={"session_key": session_key},
        )
    n = params.get("n", 1)
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    mgr = _manager_for(agent_loop, config)
    session = mgr.get_or_create(session_key)
    removed = session.undo_last_turn(n)
    if removed and mgr.exists(session_key):
        try:
            mgr.save(session)
        except Exception:
            logger.warning("session.undo: failed to persist undo for {}", session_key)
    return {"removed": removed}


async def session_branch(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.branch`` — fork the named session into a new diverging child.

    Forks ``session_id`` at its head (full-copy) via ``SessionManager.fork``
    and returns the ``SessionBranchResponse`` shape
    ``{session_id, title, message_count}`` the TUI consumes (it switches ``sid``
    to the returned ``session_id`` and reports ``message_count`` carried). The
    optional ``name`` param becomes the child title when non-empty. An unknown
    or empty (zero-message) source yields ``session_id=None`` so the TUI guard
    treats it as a no-op.
    """
    session_key = params.get("session_id", "")
    if not session_key:
        return {"session_id": None, "title": None}
    name = params.get("name")
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    mgr = _manager_for(agent_loop, config)
    child = mgr.fork(session_key, title=(name or None))
    if child is None:
        return {"session_id": None, "title": None}
    return {
        "session_id": child.key,
        "title": child.metadata.get("title"),
        "message_count": len(child.messages),
    }


async def session_export(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.export`` — render a session transcript to a Markdown file.

    Read-only: unlike clear/undo there is no busy-guard. ``session_id`` is
    resolved via the shared cross-channel core; an unresolved value is reported
    as not-found and an ambiguous one returns the candidate keys — neither
    writes a file. On success the rendered Markdown lands at
    ``<workspace>/exports/<sid>.md`` and the absolute path is returned.
    """
    value = params.get("session_id", "")
    if not value:
        return {"exported": False, "path": None, "reason": "not_found"}
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    mgr = _manager_for(agent_loop, config)
    res = mgr.resolve_key(value)
    if res.status == "ambiguous":
        return {
            "exported": False,
            "path": None,
            "reason": "ambiguous",
            "candidates": list(res.candidates),
        }
    session = mgr.peek(res.key) if res.status == "resolved" else None
    if session is None:
        return {"exported": False, "path": None, "reason": "not_found"}
    dest = default_export_path(config.workspace_path, res.key)
    try:
        written = write_transcript(session, dest)
    except OSError:
        logger.warning("session.export: failed to write export for {}", res.key)
        return {"exported": False, "path": None, "reason": "write_failed"}
    return {"exported": True, "path": str(written)}


def register_session_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    """Register the 11 session handlers on a dispatcher.

    Mirrors :func:`raven.tui_rpc.methods.turn.register_turn_methods` —
    wraps the module-level handlers in single-argument closures that pre-bind
    ``agent_loop_factory``, satisfying the dispatcher's ``params -> dict``
    contract.
    """

    async def _create(params: dict) -> dict:
        return await session_create(params, agent_loop_factory=agent_loop_factory)

    async def _close(params: dict) -> dict:
        return await session_close(params, agent_loop_factory=agent_loop_factory)

    async def _resume(params: dict) -> dict:
        return await session_resume(params, agent_loop_factory=agent_loop_factory)

    async def _list(params: dict) -> dict:
        return await session_list(params, agent_loop_factory=agent_loop_factory)

    async def _delete(params: dict) -> dict:
        return await session_delete(params, agent_loop_factory=agent_loop_factory)

    async def _most_recent(params: dict) -> dict:
        return await session_most_recent(params, agent_loop_factory=agent_loop_factory)

    async def _title(params: dict) -> dict:
        return await session_title(params, agent_loop_factory=agent_loop_factory)

    async def _clear(params: dict) -> dict:
        return await session_clear(params, agent_loop_factory=agent_loop_factory)

    async def _undo(params: dict) -> dict:
        return await session_undo(params, agent_loop_factory=agent_loop_factory)

    async def _branch(params: dict) -> dict:
        return await session_branch(params, agent_loop_factory=agent_loop_factory)

    async def _export(params: dict) -> dict:
        return await session_export(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("session.create", _create)
    dispatcher.register("session.close", _close)
    dispatcher.register("session.resume", _resume)
    dispatcher.register("session.list", _list)
    dispatcher.register("session.delete", _delete)
    dispatcher.register("session.most_recent", _most_recent)
    dispatcher.register("session.title", _title)
    dispatcher.register("session.clear", _clear)
    dispatcher.register("session.undo", _undo)
    dispatcher.register("session.branch", _branch)
    dispatcher.register("session.export", _export)


__all__ = [
    "AgentLoopFactory",
    "session_create",
    "session_close",
    "session_resume",
    "session_list",
    "session_delete",
    "session_most_recent",
    "session_title",
    "session_clear",
    "session_undo",
    "session_branch",
    "session_export",
    "register_session_methods",
]
