"""``plughub.*`` / ``plug.*`` RPC handlers — the plugin market surface.

Five methods back the GUI's plugin pages:

* ``plughub.search`` — catalog cards (+ installed flags), category list.
* ``plughub.detail`` — one full catalog entry.
* ``plug.install``  — disk transaction, then a bounded connect kick. For
  entries that need authentication (oauth / apikey) the install only
  counts once the connection authenticates: a settled auth failure inside
  the connect window rolls the transaction back and errors, and a still
  pending browser round-trip is reported as ``pending`` (the GUI keeps
  the entry out of "installed" and resolves it from later events).
* ``plug.remove``   — disconnect + ledger replay (market) / config removal (manual).
* ``plug.toggle``   — enabled flag + live sync.
* ``plug.auth``     — force-reconnect one server (retry / re-authorize).

Connect kicks are bounded by ``_CONNECT_WAIT``: an OAuth connect
legitimately parks in the browser for minutes, so mutating calls return
the current snapshot after a few seconds and the ``mcp.status`` /
``oauth.pending`` events carry the rest of the story to the GUI.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.rpc.errors import ConfigValidationError, InternalError
from raven.sandbox import SandboxInitError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher

_CONNECT_WAIT = 8.0
# How long a *change* is waited for when the focus server is already settled. Long
# enough for a sync's own disconnect/reconnect of that server to land, short
# enough that a no-op toggle answers immediately.
_SETTLE_GRACE = 1.5


def _safe_loop(factory: Any) -> Any:
    if factory is None:
        return None
    try:
        return factory()
    except Exception:
        return None


def _lang() -> str:
    from raven.config.loader import load_config

    try:
        return load_config().language
    except Exception:  # noqa: BLE001
        return "en"


def _entry_auth_mode(entry: dict) -> str:
    for piece in entry.get("contributes") or []:
        if piece.get("kind") == "mcp":
            return str((piece.get("auth") or {}).get("mode") or "none")
    return "none"


def _installed_names() -> set[str]:
    from raven.config.loader import load_config
    from raven.plughub import read_ledgers

    names = set(read_ledgers())
    try:
        names.update(load_config().tools.mcp_servers)
    except Exception:  # noqa: BLE001
        pass
    return names


def _log_sync_outcome(task: "asyncio.Task") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("plug: sync failed: {}", exc)


async def _kick_sync(loop: Any, focus: str | None = None) -> dict | None:
    """Reconcile live connections with the fresh config, waiting at most
    ``_CONNECT_WAIT`` — slow (OAuth) connects continue in the background
    and stream their progress over events.

    With a ``focus`` server the wait is opportunistic: it ends the moment
    that server settles or parks at the browser-authorization step —
    from then on the connect blocks on the user, and holding the RPC
    open for the rest of the window would only make the UI feel stuck.
    """
    manager = getattr(loop, "mcp_manager", None)
    if loop is None or not hasattr(loop, "sync_mcp") or manager is None:
        return None
    from raven.config.loader import load_config

    def _state_of(name: str) -> tuple[dict | None, str | None]:
        snap = next((s for s in manager.status() if s["name"] == name), None)
        return snap, (snap or {}).get("state")

    # Read the pre-sync state first: the wait below ends on a *change*, not on
    # the state happening to look settled. Disabling a connected server would
    # otherwise return "connected" the instant it is asked, because that is still
    # true at that moment -- and the caller would show a server it just switched
    # off as running.
    before = _state_of(focus)[1] if focus is not None else None

    try:
        servers = load_config().tools.mcp_servers
    except (ValueError, OSError) as e:
        # Same reason as plug.auth: a config this build rejects must not surface
        # as an internal error from every install and toggle.
        raise ConfigValidationError(f"config could not be read: {e}") from e
    task = asyncio.create_task(loop.sync_mcp(servers))
    task.add_done_callback(_log_sync_outcome)

    if focus is None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_CONNECT_WAIT)
        except asyncio.TimeoutError:
            pass
        except Exception:  # noqa: BLE001 — logged by the done callback
            pass
        return None

    from raven.agent.tools.mcp_oauth import auth_wait_servers

    # A focus that is already settled may have nothing to transition to (a toggle
    # that re-sends the current value, a server parked in error that sync skips).
    # Waiting the full window on those would hold the RPC open for as long as some
    # unrelated cold stdio server takes to download itself.
    window = _SETTLE_GRACE if before in ("connected", "error", "auth_required", "disconnected") else _CONNECT_WAIT
    deadline = asyncio.get_running_loop().time() + window
    while True:
        snap, state = _state_of(focus)
        if state != before and state in ("connected", "error", "auth_required", "disconnected"):
            return snap
        if focus in auth_wait_servers():
            # From here the connect blocks on the user in their browser; holding
            # the RPC open for the rest of the window only makes the UI feel stuck.
            return snap
        if task.done() or asyncio.get_running_loop().time() >= deadline:
            return snap
        await asyncio.sleep(0.05)


# ── plughub.* ──────────────────────────────────────────────────────


def _server_name(raw: Any, field: str) -> str:
    """A configured server's name, which is not a catalogue id.

    `_installed_names` lists every key in `tools.mcpServers`, hand-written ones
    included, so the panel offers them for removal, toggling and re-auth.
    Pushing those through the catalogue-id rule refused any name that could not
    be a filename -- a leading underscore, a space, over 64 characters -- and
    told the user their own server name was "not a usable catalog id", a term
    they never used and could not act on. The filename rule belongs to
    `plug.install`, where the id really does become `plugins/<id>.json`; here
    the config and the ledger are what answer.
    """
    name = str(raw or "").strip()
    if not name:
        raise ConfigValidationError(f"{field} is required", data={"field": field})
    return name


def _validated_id(raw: Any, field: str = "id") -> str:
    """A catalogue id that is safe to use as a filename, or a typed refusal.

    The ledger enforces this too -- this exists so the refusal reaches the caller
    as a validation error naming the field, instead of surfacing as
    internal_error from a path join three modules down.
    """
    from raven.plughub.ledger import LedgerIdError, validate_catalog_id

    try:
        return validate_catalog_id(str(raw or ""))
    except LedgerIdError as e:
        raise ConfigValidationError(str(e), data={"field": field, "value": raw}) from e


async def plughub_search(params: dict) -> dict:
    from raven.plughub import catalog_categories, catalog_search
    from raven.plughub.trust import HubTrustError

    q = str(params.get("q") or "").strip()
    category = str(params.get("category") or "").strip()
    try:
        items = await catalog_search(q, category, _lang())
        categories = await catalog_categories()
    except HubTrustError as e:
        # A refused hub override is a misconfiguration the operator can fix, so
        # it reaches the page as a validation error rather than internal_error.
        raise ConfigValidationError(str(e)) from e
    installed = _installed_names()
    for it in items:
        it["installed"] = it["id"] in installed
    return {"items": items, "categories": categories}


async def plughub_detail(params: dict) -> dict:
    from raven.plughub import catalog_detail
    from raven.plughub.trust import HubTrustError

    entry_id = str(params.get("id") or "")
    try:
        entry = await catalog_detail(entry_id)
    except HubTrustError as e:
        raise ConfigValidationError(str(e)) from e
    if entry is None:
        raise ConfigValidationError("no such catalog entry", data={"id": entry_id})
    return {"item": entry, "installed": entry_id in _installed_names()}


# ── plug.* ─────────────────────────────────────────────────────────


async def plug_install(params: dict, *, agent_loop_factory: Any = None) -> dict:
    from raven.plughub import catalog_detail, install_plugin, uninstall_plugin
    from raven.plughub.install import PlugInstallError
    from raven.plughub.trust import HubTrustError

    entry_id = _validated_id(params.get("id"))
    form = params.get("form") or {}
    if not isinstance(form, dict):
        raise ConfigValidationError("form must be an object of field values")
    try:
        entry = await catalog_detail(entry_id)
    except HubTrustError as e:
        raise ConfigValidationError(str(e)) from e
    if entry is None:
        raise ConfigValidationError("no such catalog entry", data={"id": entry_id})

    try:
        ledger = await install_plugin(entry, {str(k): str(v) for k, v in form.items()})
    except PlugInstallError as e:
        raise ConfigValidationError(str(e), data={"id": entry_id}) from e
    except OSError as e:
        # A read-only home or a full disk: the transaction already rolled itself
        # back, and the caller can act on this. Reporting it as internal_error
        # with a traceback would read as a raven bug.
        raise ConfigValidationError(f"the config could not be written: {e}", data={"id": entry_id}) from e

    loop = _safe_loop(agent_loop_factory)
    snap = await _kick_sync(loop, focus=entry_id)
    mode = _entry_auth_mode(entry)
    state = (snap or {}).get("state")

    if mode != "none" and state in ("error", "auth_required"):
        # Auth settled as failed inside the connect window: an unauthenticated
        # plugin is not installed, so undo the whole transaction.
        if loop is not None and hasattr(loop, "mcp_manager"):
            try:
                await loop.mcp_manager.disconnect(entry_id, drop=True)
            except Exception as e:  # noqa: BLE001 — rollback must not stop on teardown noise
                logger.warning("plug.install rollback: disconnect of '{}' failed: {}", entry_id, e)
        try:
            await uninstall_plugin(entry_id)
        except PlugInstallError as e:
            logger.warning("plug.install rollback: uninstall of '{}' failed: {}", entry_id, e)
        detail = (snap or {}).get("error") or state
        raise ConfigValidationError(
            "authentication failed; the plugin was not installed",
            data={"id": entry_id, "detail": f"authentication failed ({detail}); {entry_id} was not installed"},
        )

    pending = mode != "none" and state != "connected"
    return {"installed": not pending, "pending": pending, "ledger": ledger, "mcp": snap}


async def plug_remove(params: dict, *, agent_loop_factory: Any = None) -> dict:
    from raven.plughub import uninstall_plugin
    from raven.plughub.install import PlugInstallError

    name = _server_name(params.get("name"), "name")
    loop = _safe_loop(agent_loop_factory)
    if loop is not None and hasattr(loop, "mcp_manager"):
        try:
            await loop.mcp_manager.disconnect(name, drop=True)
        except Exception as e:  # noqa: BLE001 — a broken teardown must not block the uninstall
            logger.warning("plug.remove: disconnect of '{}' failed: {}", name, e)
    try:
        result = await uninstall_plugin(name)
    except PlugInstallError as e:
        raise ConfigValidationError(str(e), data={"field": "name", "name": name}) from e
    await _kick_sync(loop)
    return result


async def plug_toggle(params: dict, *, agent_loop_factory: Any = None) -> dict:
    from raven.plughub import toggle_server
    from raven.plughub.install import PlugInstallError

    name = _server_name(params.get("name"), "name")
    enabled = bool(params.get("enabled"))
    try:
        toggle_server(name, enabled)
    except PlugInstallError as e:
        raise ConfigValidationError(str(e), data={"field": "name", "name": name}) from e
    except (OSError, TypeError, AttributeError) as e:
        # TypeError/AttributeError: a hand-edited config where this server's
        # stanza is a string or a list rather than an object. The user's own file,
        # so the message has to name the problem rather than show a traceback.
        raise ConfigValidationError(f"'{name}' cannot be toggled: {e}", data={"field": "name", "name": name}) from e
    snap = await _kick_sync(_safe_loop(agent_loop_factory), focus=name)
    return {"name": name, "enabled": enabled, "mcp": snap}


async def plug_auth(params: dict, *, agent_loop_factory: Any = None) -> dict:
    from raven.config.loader import load_config

    name = _server_name(params.get("name"), "name")
    loop = _safe_loop(agent_loop_factory)
    if loop is None or not hasattr(loop, "mcp_manager"):
        raise InternalError("agent loop unavailable; cannot run authorization")
    try:
        cfg = load_config().tools.mcp_servers.get(name)
    except (ValueError, OSError) as e:
        # A config the running build rejects would otherwise turn every mutating
        # call in this module into internal_error with a traceback.
        raise ConfigValidationError(f"config could not be read: {e}") from e
    if cfg is None:
        raise ConfigValidationError("no such MCP server", data={"field": "name", "name": name})
    if not cfg.enabled:
        raise ConfigValidationError("server is disabled; enable it first", data={"field": "name", "name": name})

    task = asyncio.create_task(loop.mcp_manager.connect(name, cfg, executor_provider=loop._mcp_executor))
    try:
        snap = await asyncio.wait_for(asyncio.shield(task), timeout=_CONNECT_WAIT)
    except SandboxInitError as e:
        # The sandbox could not start, so a stdio server has nowhere to run. That
        # is a condition of this machine the caller can act on, not a raven fault
        # to report with a traceback tail.
        raise ConfigValidationError(f"the sandbox could not start: {e}", data={"field": "name", "name": name}) from e
    except asyncio.TimeoutError:
        snap = next((s for s in loop.mcp_manager.status() if s["name"] == name), None)
    except asyncio.CancelledError:
        # A connect task the transport killed from within must not read as
        # "this RPC was cancelled" -- answer with the server's snapshot.
        if not task.cancelled():
            raise
        snap = next((s for s in loop.mcp_manager.status() if s["name"] == name), None)
    logger.debug("plug.auth: returning for '{}' with state {}", name, (snap or {}).get("state"))
    return {"name": name, "mcp": snap}


def register_plughub_methods(dispatcher: "Dispatcher", *, agent_loop_factory: Any = None) -> None:
    """Register the plugin-market handlers on a dispatcher instance."""

    def bind(fn):
        async def _h(params: dict) -> dict:
            return await fn(params, agent_loop_factory=agent_loop_factory)

        return _h

    dispatcher.register("plughub.search", plughub_search)
    dispatcher.register("plughub.detail", plughub_detail)
    dispatcher.register("plug.install", bind(plug_install))
    dispatcher.register("plug.remove", bind(plug_remove))
    dispatcher.register("plug.toggle", bind(plug_toggle))
    dispatcher.register("plug.auth", bind(plug_auth))


__all__ = [
    "plug_auth",
    "plug_install",
    "plug_remove",
    "plug_toggle",
    "plughub_detail",
    "plughub_search",
    "register_plughub_methods",
]
