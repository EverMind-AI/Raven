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

import asyncio
import json
import os
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from raven.cli.update_notice import update_notice
from raven.config.loader import drain_migration_notices, load_config
from raven.providers.rates import resolve_context_window
from raven.rpc.errors import ConfigValidationError, TurnInProgressError
from raven.rpc.methods import turn as turn_module
from raven.rpc.methods.system import _raven_version
from raven.session.export import default_export_path, write_transcript
from raven.session.manager import SessionManager, new_chat_id
from raven.utils.helpers import estimate_prompt_tokens

if TYPE_CHECKING:
    from raven.agent.loop.main import AgentLoop
    from raven.config.schema import Config
    from raven.rpc.dispatcher import Dispatcher


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


async def _baseline_usage(
    agent_loop: "AgentLoop | None",
    config: "Config",
) -> dict[str, Any]:
    """Banner ``info.usage`` subfield — boot baseline (no turn has run yet).

    All counters are zero at session.create: a fresh session_key carries no
    prior LLM calls. Each turn's ``message.complete`` event updates them
    post-turn. ``context_max`` follows the same ladder ``AgentLoop`` uses: a
    pinned ``context_window_tokens`` wins outright; otherwise the model's real
    window (live from the provider table when LiteLLM lags, e.g. OpenRouter),
    or 0 when neither is known — the UI's empty state, not a borrowed number.
    Usage starts at zero for a fresh session by design. Resume reuses the
    zero baseline; counters refresh on the next turn.

    Cost is the exception: on a subscription there is no per-token figure, so the
    banner says so rather than opening at $0.00. Zero here read as free until the
    first turn replaced it, which is the answer this session will never have.

    ``resolve_context_window`` defaults to ``allow_fetch=True``, so a cold
    OpenRouter model can reach for a synchronous 10s HTTP call; this handler
    runs on the event loop (an RPC method), so that call is pushed to a
    thread rather than blocking every other session in flight.
    """
    from raven.providers.rates import is_plan_billed

    model = getattr(agent_loop, "model", None)
    configured = config.agents.defaults.context_window_tokens
    if configured:
        context_max = configured
    elif model:
        context_max = await asyncio.to_thread(resolve_context_window, model) or 0
    else:
        context_max = 0
    return {
        "input": 0,
        "output": 0,
        "cost_usd": None if model and is_plan_billed(str(model)) else 0.0,
        "calls": 0,
        "context_max": context_max,
        "context_used": 0,
        "context_percent": 0,
    }


async def _default_session_info(
    agent_loop: "AgentLoop | None",
    config: "Config",
) -> dict[str, Any]:
    """Build the init bundle returned by ``session.create`` / ``session.resume``.

    ``agent_loop=None`` triggers graceful fallback (``tools={}``, ``skills={}``,
    zero usage, ``lazy=True``); version is always real (cached at module load).
    """
    model_id = config.agents.defaults.model
    usage = await _baseline_usage(agent_loop, config)
    info: dict[str, Any] = {
        "model": model_id,
        "model_id": model_id,
        "provider": config.agents.defaults.provider,
        "context_window": usage["context_max"],
        "lazy": agent_loop is None,
        "skills": _enumerate_skills(agent_loop),
        "tools": _enumerate_tools(agent_loop),
        "usage": usage,
        "version": _RAVEN_VERSION,
        "cwd": os.getcwd(),
        "mcp_servers": [],
        # Which of a multi-endpoint provider's endpoints this session is on.
        # None for every single-endpoint provider -- there is one address and it
        # carries no label worth showing.
        "endpoint": getattr(getattr(agent_loop, "provider", None), "active_endpoint_label", None),
    }

    # Nudge the status bar to run `raven upgrade` when the cached latest release
    # is newer. Reading the cache is pure/fast; the cache is refreshed once per
    # launch from the `raven tui` entrypoint (see cli/tui_commands.py), so a
    # freshly published release shows up on the next launch.
    notice = update_notice(_RAVEN_VERSION)
    if notice is not None:
        info["update_available"], info["update_command"] = notice

    # Anything a config migration changed on the user's behalf while this
    # backend booted. The CLI prints these itself; a TUI/served-page user never
    # sees that terminal, so the first session of the launch carries them into
    # the transcript instead. Drained, so a later resume does not repeat them.
    migrated = drain_migration_notices()
    if migrated:
        info["config_notices"] = migrated

    return info


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

    Beyond that shape, three stored fields ride along so a resumed transcript
    can be drawn with the same detail as a live one (the GUI restores thought
    folds, per-call targets and answer times from them; clients that do not
    read them are unaffected):

    * ``reasoning_content`` — the assistant's thought for that message;
    * ``tool_calls`` — flattened to ``[{id, name, arguments}]``, matched to a
      later ``role="tool"`` entry through its ``tool_call_id``. The provider
      shape is rebuilt rather than forwarded so a live-cache message holding
      provider objects still serialises;
    * ``timestamp`` — the stored ISO wall clock;
    * ``diff`` — a file tool's unified diff of the change it made, on its
      ``role="tool"`` entry. The one record with real line numbers, which the
      arguments alone can never reconstruct.
    * ``reasoning_ms`` / ``duration_ms`` — how long the thought on that
      assistant entry took, and how long the call that ``role="tool"`` entry
      answers ran. Absent on anything written before they were recorded, and
      absent means unknown: a client must draw the bare header rather than a
      zero, which would claim the turn thought for no time at all.
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
        for extra_key in (
            "context",
            "name",
            "tool_call_id",
            "timestamp",
            "diff",
            "turn_ended",
            "notice",
            "origin",
            "delegated",
            "reasoning_ms",
            "duration_ms",
        ):
            if extra_key in m:
                entry[extra_key] = m[extra_key]
        reasoning = m.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            entry["reasoning_content"] = reasoning
        calls = _wire_tool_calls(m.get("tool_calls"))
        if calls:
            entry["tool_calls"] = calls
        out.append(entry)
    return out


def _wire_tool_calls(raw: Any) -> list[dict[str, str]]:
    """Flatten stored ``tool_calls`` to the ``[{id, name, arguments}]`` wire shape.

    Entries that carry neither a name nor arguments are dropped: a row with
    nothing to say about the call it made is worse than no row.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for call in raw:
        if isinstance(call, dict):
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            cid, name, args = call.get("id"), fn.get("name"), fn.get("arguments")
        else:
            fn = getattr(call, "function", None)
            cid, name, args = getattr(call, "id", None), getattr(fn, "name", None), getattr(fn, "arguments", None)
        if not name and not args:
            continue
        out.append(
            {
                "id": str(cid or ""),
                "name": str(name or ""),
                "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, default=str),
            }
        )
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

    An optional ``workdir`` (absolute path) pins where this session's turns
    run: it lands in the cached session's metadata as the workdir override
    ``WorkdirResolver`` already honors, and persists with the first save —
    as lazy as the mint itself. This is how a client attached to a shared
    gateway keeps its own launch directory instead of inheriting the
    gateway's. An unusable path (relative, or inside the agent home) is a
    ``ConfigValidationError``.
    """
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    session_id = f"tui:{new_chat_id()}"
    info = await _default_session_info(agent_loop, config)
    workdir = params.get("workdir")
    if workdir:
        from raven.agent.workdir import validate_override

        try:
            resolved = validate_override(workdir, config.workspace_path)
        except ValueError as e:
            raise ConfigValidationError(str(e), data={"field": "workdir"}) from e
        _manager_for(agent_loop, config).get_or_create(session_id).metadata["workdir"] = str(resolved)
        info["cwd"] = str(resolved)
    return {
        "session_id": session_id,
        "info": info,
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

    ``info.usage.context_used`` is filled in from the loaded transcript. The
    zero baseline is right for a brand-new session and wrong for a resumed one:
    a client that shows how full the window is would read 0% on a session
    already carrying 40 messages, until the next turn happened to report real
    usage. The number is a tiktoken estimate of what the next call will send, so
    ``context_estimated`` marks it as such.
    """
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    info = await _default_session_info(agent_loop, config)
    session_key = params.get("session_id")

    if session_key:
        try:
            mgr = _manager_for(agent_loop, config)
            raw = mgr.peek(session_key)
            if raw is not None:
                _fill_resumed_context(info, raw)
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


def _fill_resumed_context(info: dict[str, Any], session: Any) -> None:
    """Estimate how full the context window is for a session being resumed.

    Estimation, not measurement: no provider has been called yet in this
    process, so the only honest number available is what the next call would
    cost to send. Failures leave the zero baseline alone rather than guessing --
    a wrong denominator is worse than an unknown one.

    Measured over ``get_history(max_messages=0)``, not over the stored
    transcript. Those are different lists: history slices at
    ``last_consolidated``, and the runtime consolidates on every user-inbound
    turn, so estimating over everything stored reported the size of a context
    that was already archived. A session a quarter full read as 100%. That call
    -- argument included -- is the one the next turn makes, which is what the
    number claims to be.
    """
    usage = info.get("usage")
    if not isinstance(usage, dict) or session is None:
        return
    try:
        # max_messages=0 is "all of it", which is what both places that build or
        # measure the real prompt pass (loop/main.py and the consolidator's own
        # estimator). The default caps at the last 500, and the tail is bounded
        # in tokens rather than in messages -- consolidation fires on the window
        # -- so a chatty session on a large-window model sits well past 500 and
        # the meter would go quiet exactly as it started to matter.
        messages = session.get_history(max_messages=0)
    except Exception:
        logger.exception("session.resume: could not read the session history")
        return
    if not messages:
        return
    try:
        used = estimate_prompt_tokens(messages)
    except Exception:
        logger.exception("session.resume: context estimate failed")
        return
    context_max = usage.get("context_max") or 0
    usage["context_used"] = used
    usage["context_percent"] = round(100 * used / context_max) if context_max else 0
    usage["context_estimated"] = True


def _session_to_list_item(info: dict[str, Any]) -> dict[str, Any]:
    """Convert a list_sessions entry to the SessionListItem wire shape.

    The TS SessionListItem (gatewayTypes.ts:130) requires:
      id, message_count, preview, started_at (unix timestamp), title.
    started_at maps from created_at ISO string; preview is always empty in
    v0.1 — metadata carries no message content, and the TS picker falls back
    to title or "(untitled)".

    ``updated_at`` rides along beside it: the list is already ordered by last
    activity, so a row that shows when the session was *created* is ordered by
    one clock and labelled with another. It is the newest message's own stamp --
    i.e. when the model last finished answering -- because the metadata record's
    updated_at only moves when a save writes metadata and can lag the transcript
    by a turn. Falls back through the metadata stamp to started_at, so a row
    always has a time.
    """
    key = info.get("key", "")

    def _ts(value: Any) -> float:
        if not value:
            return 0.0
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except ValueError:
            return 0.0

    started_at = _ts(info.get("created_at"))
    meta = info.get("metadata") or {}
    title = meta.get("title") or ""
    return {
        "id": key,
        "message_count": info.get("message_count", 0),
        # The user's first message: what an untitled session gets titled with.
        "preview": info.get("first_user_message", ""),
        "source": key.partition(":")[0] or "tui",
        "started_at": started_at,
        "updated_at": _ts(info.get("last_user_message_at")) or _ts(info.get("updated_at")) or started_at,
        "title": title,
        "pinned": bool(meta.get("pinned")),
    }


async def session_list(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.list`` — list sessions sorted by updated_at desc.

    ``channels`` (optional list of channel names) picks which session
    channels to include; it defaults to ["tui"] so existing pickers are
    unchanged — the GUI passes ["tui", "cron"] to also show scheduled runs
    (each item's ``source`` carries its channel). An optional positive
    integer ``limit`` slices after the sort (newest sessions win); zero,
    negative, or non-integer limits are ignored.
    Returns the SessionListResponse shape: {sessions: SessionListItem[]}.
    """
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    mgr = _manager_for(agent_loop, config)
    channels = params.get("channels")
    if not isinstance(channels, list) or not channels:
        channels = ["tui"]
    entries: list[dict] = []
    for channel in channels:
        if isinstance(channel, str) and channel:
            entries.extend(mgr.list_sessions(channel=channel))
    entries.sort(key=lambda x: x.get("last_user_message_at") or x.get("updated_at") or "", reverse=True)
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

    Scoped to this checkout: the TUI offers this session to reopen, and one
    started in another project is not the one the user left.
    """
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    mgr = _manager_for(agent_loop, config)
    chat_id = mgr.find_most_recent_chat_id("tui", this_project_only=True)
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
        session.set_title(title)
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


async def session_pin(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.pin`` — pin or unpin a session in the picker.

    The flag lives in session metadata, exactly like the title, so it survives
    a page reload and is shared by every client reading the same session pool.
    Same lazy-session contract as ``session.title``: a never-saved session
    keeps the flag in memory (``pending`` True) until its first save.
    """
    session_key = params.get("session_id", "")
    pinned = bool(params.get("pinned"))
    if not session_key:
        return {"pinned": pinned, "session_key": "", "pending": False}
    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    mgr = _manager_for(agent_loop, config)
    session = mgr.get_or_create(session_key)
    if pinned:
        session.metadata["pinned"] = True
    else:
        session.metadata.pop("pinned", None)
    if mgr.exists(session_key):
        try:
            mgr.save(session)
        except Exception:
            logger.warning("session.pin: failed to persist pin for {}", session_key)
            return {"pinned": pinned, "session_key": session_key, "pending": True}
        return {"pinned": pinned, "session_key": session_key, "pending": False}
    return {"pinned": pinned, "session_key": session_key, "pending": True}


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
    if turn_module.is_session_busy(session_key):
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
    if turn_module.is_session_busy(session_key):
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


async def session_compress(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``session.compress`` — archive old messages now instead of at the window.

    The runtime already compacts on its own once a prompt outgrows the context
    window (``maybe_consolidate_by_tokens`` on every user-inbound turn). This is
    the deliberate version: it forces the same loop early, which is what a user
    asks for when they know the earlier exploration is dead weight.

    Returns the TUI's ``SessionCompressResponse`` subset — before/after token
    estimates, message counts, and a ``summary`` the clients print verbatim.
    ``noop`` marks the cases where nothing moved: no consolidator (the Curator
    context engine owns compaction), an empty session, or no safe boundary.
    """
    session_key = params.get("session_id", "")
    if not session_key:
        raise ConfigValidationError(
            "session.compress requires params.session_id",
            data={"field": "session_id"},
        )
    if turn_module.is_session_busy(session_key):
        raise TurnInProgressError(
            f"session {session_key!r} has an active turn; interrupt it before compressing",
            data={"session_key": session_key},
        )

    agent_loop = _safe_invoke_factory(agent_loop_factory)
    config = load_config()
    mgr = _manager_for(agent_loop, config)
    session = mgr.get_or_create(session_key)
    before_messages = len(session.messages)

    consolidator = getattr(agent_loop, "memory_consolidator", None)
    owns = bool(getattr(getattr(agent_loop, "context_engine", None), "owns_compaction", False))
    if consolidator is None or owns:
        note = "the context engine manages context on its own" if owns else "no memory consolidator in this runtime"
        return {
            "before_messages": before_messages,
            "after_messages": before_messages,
            "before_tokens": 0,
            "after_tokens": 0,
            "removed": 0,
            "summary": {"headline": "nothing to compress", "noop": True, "note": note},
        }

    stats = await consolidator.maybe_consolidate_by_tokens(session, force=True)
    before_tokens = int(stats.get("before_tokens", 0))
    after_tokens = int(stats.get("after_tokens", before_tokens))
    archived = int(stats.get("archived", 0))
    if archived and mgr.exists(session_key):
        try:
            mgr.save(session)
        except Exception:
            logger.warning("session.compress: failed to persist {}", session_key)

    # Consolidation annotates and advances ``last_consolidated``; it never
    # removes anything from the list. So the survivors are the slice past that
    # boundary, and the count comes from the same slice the payload does --
    # deriving it as ``before - archived`` let the number and the messages
    # beside it describe two different lists.
    survivors = session.messages[session.last_consolidated :]
    headline = f"archived {archived} messages" if archived else "nothing to compress"
    result: dict[str, Any] = {
        "before_messages": before_messages,
        "after_messages": len(survivors),
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "removed": archived,
        "summary": {
            "headline": headline,
            "noop": archived == 0,
            "token_line": f"{before_tokens} -> {after_tokens} tokens",
        },
    }
    if archived:
        # A caller that just archived half the transcript is looking at messages
        # that no longer exist. Returning the survivors plus refreshed info and
        # usage is what lets it redraw instead of reporting a compaction while
        # still showing what was compacted -- the same three fields
        # ``session.resume`` hands back, produced the same way.
        #
        # Best-effort on purpose: the archive is the operation and it has already
        # committed. Failing the whole call because a redraw aid could not be
        # assembled would report failure for work that succeeded, and would leave
        # the caller with neither the new transcript nor the knowledge that its
        # old one is stale.
        try:
            info = await _default_session_info(agent_loop, config)
            _fill_resumed_context(info, session)
            result["info"] = info
            result["messages"] = _map_to_wire(survivors, session_key)
            usage = info.get("usage")
            if isinstance(usage, dict):
                result["usage"] = usage
        except Exception:
            logger.warning("session.compress: archived {} but could not build the redraw payload", session_key)
    return result


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
    """Register the 12 session handlers on a dispatcher.

    Mirrors :func:`raven.rpc.methods.turn.register_turn_methods` —
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

    async def _pin(params: dict) -> dict:
        return await session_pin(params, agent_loop_factory=agent_loop_factory)

    async def _clear(params: dict) -> dict:
        return await session_clear(params, agent_loop_factory=agent_loop_factory)

    async def _undo(params: dict) -> dict:
        return await session_undo(params, agent_loop_factory=agent_loop_factory)

    async def _compress(params: dict) -> dict:
        return await session_compress(params, agent_loop_factory=agent_loop_factory)

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
    dispatcher.register("session.pin", _pin)
    dispatcher.register("session.clear", _clear)
    dispatcher.register("session.undo", _undo)
    dispatcher.register("session.compress", _compress)
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
    "session_pin",
    "session_title",
    "session_clear",
    "session_undo",
    "session_compress",
    "session_branch",
    "session_export",
    "register_session_methods",
]
