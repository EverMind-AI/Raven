"""``memory.*`` RPC handlers -- the GUI's data & memory surface.

Two methods back the memory page:

* ``memory.stats``  — per-kind totals for the four EverOS memory kinds.
* ``memory.list``   — paginated listing (``/get``) or semantic search
  (``/search``) over one kind, projected to card-sized rows.

Both go over EverOS's HTTP API (the same server the memory backend
talks to), so the service boundary stays intact. The page is read-only:
EverOS exposes no memory deletion, and the one destructive operation it
does have is internal reconciliation after a skill rename, not a
retirement path a reader can be offered.

Kind names follow EverOS's ``/get`` contract verbatim: ``episode`` /
``profile`` (user track) and ``agent_case`` / ``agent_skill`` (agent
track). The owner id for each track comes from ``memory.user_id`` /
``memory.agent_id`` in raven's config — the same identities the backend
stamps on stored turns, so the page sees exactly what recall sees.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.config.raven import MEMORY_BACKEND_DISTRIBUTIONS, SHIPPED_DEFAULT_BACKEND
from raven.core.plugin_stack import everos_plugin_installed, everos_plugin_missing_note
from raven.rpc.errors import ConfigValidationError, InternalError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher

_HTTP_TIMEOUT_S = 15.0
# The backend contributions whose store this page reads. Named once: the page
# is EverOS-shaped down to its four tabs, so every check for "is this page
# looking at the right store" has to mean the same thing. Two plugins serve
# that shape -- the local server and EverOS Cloud -- and the page follows
# whichever ``memory.backend`` names; its slice and its package are looked up
# here by that name.
_EVEROS_BACKEND = SHIPPED_DEFAULT_BACKEND
_CLOUD_BACKEND = "everos-cloud"
# Read off the pairing rather than spelled again: a backend name that stops
# being one of its keys fails here at import rather than reporting a plugin
# nobody ships.
_CLOUD_DISTRIBUTION = MEMORY_BACKEND_DISTRIBUTIONS[_CLOUD_BACKEND]

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


def _configured_backend() -> str | None:
    """``memory.backend`` as configured; ``None`` when memory is off."""
    from raven.config.raven import load_raven_config

    return load_raven_config().memory.backend


def _backend_for_page() -> str | None:
    """The configured name, with an unreadable config answering as "not known".

    ``memory.stats`` never raises: a page that cannot name the selected backend
    still draws its counts and its switch, and the note says what went wrong.
    """
    try:
        return _configured_backend()
    except Exception:  # noqa: BLE001 - an unreadable config is not this page's to report
        return None


def _available_backends() -> list[str]:
    """The EverOS backends this install can actually select, local first.

    The page draws one segment per name in ``MEMORY_BACKEND_DISTRIBUTIONS`` and
    disables the absent ones, so a reader sees what the install has rather than
    only the name the file happens to carry. The probe is ``find_spec`` for the
    same reason the roster's is: presence, not an import that would pay the
    engine's startup cost on every page load. A plugin the reader put in
    ``plugins.disabled`` is absent too -- offering it would be a segment whose
    write the same config refuses.
    """
    from importlib.util import find_spec

    from raven.config.raven import MEMORY_BACKEND_DISTRIBUTIONS, load_raven_config

    try:
        disabled = set(load_raven_config().plugins.disabled or [])
    except Exception:  # noqa: BLE001 - an unreadable config is not this page's to report
        disabled = set()

    names: list[str] = []
    if MEMORY_BACKEND_DISTRIBUTIONS[_EVEROS_BACKEND] not in disabled and everos_plugin_installed():
        names.append(_EVEROS_BACKEND)
    if MEMORY_BACKEND_DISTRIBUTIONS[_CLOUD_BACKEND] not in disabled and find_spec("raven_everos_cloud") is not None:
        names.append(_CLOUD_BACKEND)
    return names


def _local_server_running() -> bool:
    """Whether the local EverOS server answers where the local backend reads.

    The page draws its start button off this; the four kind reads below cannot
    tell "server not running" apart from "server up and empty" better than a
    probe can, and a probe is what the start button's own answer is checked
    with.
    """
    try:
        from raven_everos.server import ProbeVerdict, probe_health

        base_url, _user, _agent = _cfg()
        return probe_health(base_url) is ProbeVerdict.OK
    except Exception:  # noqa: BLE001 - a missing plugin or unreadable config is "not running here"
        return False


def _cloud_key_set() -> bool:
    """Whether the hosted backend has a key on file, or in the environment.

    The memory page needs this before its first store read: a selected cloud
    backend with no key answers every read with 401, and drawing that as "the
    service is down" hides the one action that fixes it. The same resolution the
    backend uses at runtime, so the panel and the client cannot disagree.
    """
    try:
        from raven.config.raven import load_raven_config
        from raven_everos_cloud.backend import resolve_api_key

        slice_ = (load_raven_config().plugins.config or {}).get(_CLOUD_DISTRIBUTION, {})
        return bool(resolve_api_key(slice_)[0])
    except Exception:  # noqa: BLE001 - a missing plugin or unreadable config is "no key here"
        return False


def _cfg() -> tuple[str, str, str]:
    """(base_url, user_id, agent_id) from raven's config.

    ``plugins`` / ``memory`` live on the composed :class:`RavenConfig`,
    not the base channels ``Config`` — hence ``load_raven_config``. The slice
    and the default address follow the configured backend.
    """
    from raven.config.raven import load_raven_config

    cfg = load_raven_config()
    user_id = cfg.memory.user_id or "default"
    agent_id = cfg.memory.agent_id or "default"
    if cfg.memory.backend == _CLOUD_BACKEND:
        from raven_everos_cloud.backend import DEFAULT_BASE_URL

        plug = (cfg.plugins.config or {}).get(_CLOUD_DISTRIBUTION, {})
        return str(plug.get("base_url") or DEFAULT_BASE_URL).rstrip("/"), user_id, agent_id
    from raven_everos.server import DEFAULT_EVEROS_BASE_URL

    plug = (cfg.plugins.config or {}).get("everos-memory", {})
    return str(plug.get("base_url") or DEFAULT_EVEROS_BASE_URL).rstrip("/"), user_id, agent_id


def _auth_headers() -> dict[str, str]:
    """The Bearer header for the cloud backend; nothing for the local one."""
    from raven.config.raven import load_raven_config

    cfg = load_raven_config()
    if cfg.memory.backend != _CLOUD_BACKEND:
        return {}
    from raven_everos_cloud.backend import resolve_api_key

    key, _source = resolve_api_key((cfg.plugins.config or {}).get(_CLOUD_DISTRIBUTION, {}))
    return {"Authorization": f"Bearer {key}"} if key else {}


async def _post(base_url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(_HTTP_TIMEOUT_S)) as client:
        r = await client.post(f"{base_url}{path}", json=body, headers=_auth_headers())
        r.raise_for_status()
        return r.json() or {}


async def _search_tuning(base_url: str, kind: str) -> dict[str, Any]:
    """What ``/search`` needs on this server, asked the way the chat adapter
    asks (``_search_tuning`` in ``raven_everos.backend``).

    Keyword when nothing can embed; vector when the agent track has no
    cross-encoder, which is every install without a rerank role -- the LLM
    rerank lane the server offers instead measured 10-12 s a search, past this
    page's own timeout; the profile opted in on its own tab. Without these the
    server refuses the search with 422 and the page reported "everos
    unreachable" over a retry button.
    """
    body: dict[str, Any] = {}
    if _configured_backend() != _CLOUD_BACKEND:
        # The cloud always embeds and reranks and has no OSS ``/health`` to ask;
        # only the local server can lack a role.
        from raven_everos.health import probe_capabilities

        report = await asyncio.to_thread(probe_capabilities, base_url)
        if report.available("embedding") is False:
            body["method"] = "keyword"
        elif kind in _AGENT_KINDS and report.available("rerank") is False:
            body["method"] = "vector"
    if kind == "profile":
        body["include_profile"] = True
    return body


def _everos_error(exc: Exception) -> str:
    """The server's own sentence when it refused, the transport error otherwise.

    A 4xx from EverOS carries ``error.message`` saying what to change; folding
    it into "unreachable" sent people to check a server that was answering. A
    timeout carries no message at all, so it says what it waited for.
    """
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        try:
            message = ((exc.response.json() or {}).get("error") or {}).get("message")
        except ValueError:
            message = None
        if message:
            return f"everos refused the request: {message}"
    if isinstance(exc, httpx.TimeoutException):
        return f"everos did not answer within {_HTTP_TIMEOUT_S:g}s"
    return f"everos unreachable: {str(exc) or type(exc).__name__}"


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
    to render as four zeros or a retry button, which reads as "your memories are
    gone" rather than "this page is not where they are".
    """
    from importlib.util import find_spec

    try:
        backend = _configured_backend()
    except Exception:  # noqa: BLE001 - an unreadable config is not this page's to report
        backend = _EVEROS_BACKEND
    if backend == _CLOUD_BACKEND:
        distribution, package = _CLOUD_DISTRIBUTION, "raven_everos_cloud"
        if find_spec(package) is None:
            return (
                f"the {distribution} distribution is not installed, so the {_CLOUD_BACKEND} memory "
                f"backend has nothing behind it. Install {distribution}, or run raven onboard to pick another backend."
            )
        return None
    if not everos_plugin_installed():
        return everos_plugin_missing_note()
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
    backend = _backend_for_page()
    available = _available_backends()
    cloud_key_set = _cloud_key_set()
    local_running = False
    if backend == _EVEROS_BACKEND:
        local_running = await asyncio.to_thread(_local_server_running)
    if note is None and backend == _CLOUD_BACKEND and not cloud_key_set:
        # A missing key is not an unreachable service: answer here instead of
        # sending four requests that can only come back 401, and leave the
        # page one state to draw -- its key panel.
        note = "EverOS Cloud has no API key yet; add one in Settings > Memory."
    if note is not None:
        # Same shape as an unreachable server plus the one thing that shape
        # could never carry: why. Without it the page says zero and leaves the
        # reader to guess between "not installed", "not the configured
        # backend", and "your memories are gone". The backend fields stay:
        # the switch is exactly what lets a reader leave a backend that is
        # missing or turned off.
        logger.warning("memory.stats: {}", note)
        return {
            "ok": False,
            "note": note,
            "base_url": "",
            "episodes": 0,
            "profiles": 0,
            "agent_cases": 0,
            "agent_skills": 0,
            "backend": backend,
            "available_backends": available,
            "cloud_key_set": cloud_key_set,
            "local_running": local_running,
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
            payload = await _post(base_url, "/api/v2/memory/get", body)
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
        "backend": backend,
        "available_backends": available,
        "cloud_key_set": cloud_key_set,
        "local_running": local_running,
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
    if _backend_for_page() == _CLOUD_BACKEND and not _cloud_key_set():
        # Same reason as stats: nothing to ask, and a 401 would read as the
        # service being down rather than as the step the reader has not taken.
        return {
            "items": [],
            "total": 0,
            "page": 1,
            "page_size": 0,
            "note": "EverOS Cloud has no API key yet; add one in Settings > Memory.",
        }
    page = max(1, int(params.get("page") or 1))
    page_size = min(100, max(1, int(params.get("page_size") or 20)))
    q = str(params.get("q") or "").strip()
    base_url, user_id, agent_id = _cfg()
    owner = _owner_body(kind, user_id, agent_id)

    try:
        if q:
            payload = await _post(
                base_url,
                "/api/v2/memory/search",
                owner | {"query": q, "top_k": page_size} | await _search_tuning(base_url, kind),
            )
            rows = (payload.get("data") or {}).get(_KIND_FIELD[kind]) or []
            items = [_project(kind, r) for r in rows]
            return {"items": items, "total": len(items), "page": 1, "page_size": page_size, "note": None}
        payload = await _post(
            base_url,
            "/api/v2/memory/get",
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
        raise InternalError(_everos_error(e)) from e


async def memory_start_local(params: dict) -> dict:
    """``memory.startLocal`` — start the local EverOS server from the page.

    The backend starts the same server when it boots; this is the path for a
    page reading a local store whose server is not up (stopped, killed, or
    never started), so the reader can fix it without restarting Raven.
    """
    del params
    try:
        from raven_everos.server import (
            DEFAULT_EVEROS_BASE_URL,
            ProbeVerdict,
            ensure_everos_server,
            probe_health,
        )
    except Exception as e:  # noqa: BLE001 - a missing/broken plugin is the answer, not a traceback
        return {
            "ok": False,
            "running": False,
            "base_url": "",
            "detail": f"the everos-memory distribution is not available: {e}",
        }

    from raven.config.raven import load_raven_config

    try:
        cfg = load_raven_config()
        plug = (cfg.plugins.config or {}).get("everos-memory", {})
        base_url = str(plug.get("base_url") or DEFAULT_EVEROS_BASE_URL).rstrip("/")
    except Exception as e:  # noqa: BLE001 - the page shows the detail verbatim
        return {"ok": False, "running": False, "base_url": "", "detail": f"config unreadable: {e}"}

    try:
        await ensure_everos_server(base_url, timeout=15.0)
    except Exception as e:  # noqa: BLE001 - the page shows the detail verbatim
        return {"ok": False, "running": False, "base_url": base_url, "detail": str(e)}

    verdict = await asyncio.to_thread(probe_health, base_url)
    running = verdict is ProbeVerdict.OK
    return {
        "ok": running,
        "running": running,
        "base_url": base_url,
        "detail": None if running else verdict.value,
    }


async def memory_apply_runtime(params: dict, *, agent_loop_factory: Any = None) -> dict:
    """``memory.applyRuntime`` — point the running loop at the configured backend.

    A generation swap rebuilds the whole runtime and closes the page's RPC
    socket; the page drew that as "not connected" on every switch. The memory
    backend is a live reference inside the loop, so replacing it in place keeps
    the socket and the prompt layout untouched. The new backend is started
    before the old one stops, so a switch to a local server whose process is
    down brings it up here rather than showing a start button afterwards.
    """
    del params
    from raven.config.loader import load_config
    from raven.config.raven import load_raven_config
    from raven.core.plugin_stack import maybe_build_memory_backend

    try:
        loop = agent_loop_factory() if agent_loop_factory is not None else None
    except Exception as e:  # noqa: BLE001 - the page shows the detail
        return {"ok": False, "backend": None, "detail": f"the running loop is not available: {e}"}
    if loop is None:
        return {"ok": False, "backend": None, "detail": "no running agent loop answered"}

    try:
        cfg = load_raven_config()
        backend = maybe_build_memory_backend(load_config().workspace_path, cfg)
        if cfg.memory.backend is not None and backend is None:
            return {
                "ok": False,
                "backend": cfg.memory.backend,
                "detail": f"the {cfg.memory.backend} backend could not be built",
            }
        await loop.rebind_memory_backend(
            backend,
            name=cfg.memory.backend,
            user_id=cfg.memory.user_id,
            memory_top_k=cfg.memory.memory_top_k,
        )
    except Exception as e:  # noqa: BLE001 - the page shows the detail
        return {"ok": False, "backend": None, "detail": str(e)}
    return {"ok": True, "backend": cfg.memory.backend, "detail": None}


def register_memory_methods(dispatcher: "Dispatcher", *, agent_loop_factory: Any = None) -> None:
    async def _apply(params: dict) -> dict:
        return await memory_apply_runtime(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("memory.stats", memory_stats)
    dispatcher.register("memory.list", memory_list)
    dispatcher.register("memory.startLocal", memory_start_local)
    dispatcher.register("memory.applyRuntime", _apply)


__all__ = [
    "memory_list",
    "memory_stats",
    "register_memory_methods",
]
