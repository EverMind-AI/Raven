"""``plughub.*`` / ``plug.*`` RPC handlers — the plugin market surface.

Six methods back the GUI's plugin pages:

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

The transaction itself lives in :mod:`raven.plughub.connect`, shared with the
agent's ``plugin`` tool, which drives the same install/rollback/connect path
in-process from a turn. What is left here is the RPC surface's own job:
JSON-RPC-shaped params in, typed RPC errors out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.plughub.connect import (
    PlugConnectError,
    PlugRuntimeUnavailableError,
    installed_names,
    kick_sync,
    server_name,
)
from raven.rpc.errors import ConfigValidationError, InternalError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


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


def _as_rpc(e: PlugConnectError) -> Exception:
    """Translate an engine refusal into this surface's error vocabulary."""
    if isinstance(e, PlugRuntimeUnavailableError):
        return InternalError(e.detail)
    return ConfigValidationError(e.detail, data=e.data)


# ── plughub.* ──────────────────────────────────────────────────────


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
    installed = installed_names()
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
    return {"item": entry, "installed": entry_id in installed_names()}


# ── plug.* ─────────────────────────────────────────────────────────


async def plug_install(params: dict, *, agent_loop_factory: Any = None) -> dict:
    from raven.plughub.connect import install_and_connect

    try:
        result = await install_and_connect(params.get("id"), params.get("form"), _safe_loop(agent_loop_factory))
    except PlugConnectError as e:
        raise _as_rpc(e) from e
    # `auth_mode` is for the tool's prose; this response model forbids extras.
    return {
        "installed": result["installed"],
        "pending": result["pending"],
        "ledger": result["ledger"],
        "mcp": result["mcp"],
    }


async def plug_remove(params: dict, *, agent_loop_factory: Any = None) -> dict:
    from raven.plughub.connect import remove

    try:
        return await remove(params.get("name"), _safe_loop(agent_loop_factory))
    except PlugConnectError as e:
        raise _as_rpc(e) from e


async def plug_toggle(params: dict, *, agent_loop_factory: Any = None) -> dict:
    from raven.plughub import toggle_server
    from raven.plughub.install import PlugInstallError

    try:
        name = server_name(params.get("name"), "name")
    except PlugConnectError as e:
        raise _as_rpc(e) from e
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
    try:
        snap = await kick_sync(_safe_loop(agent_loop_factory), focus=name)
    except PlugConnectError as e:
        raise _as_rpc(e) from e
    return {"name": name, "enabled": enabled, "mcp": snap}


async def plug_auth(params: dict, *, agent_loop_factory: Any = None) -> dict:
    from raven.plughub.connect import authorize

    try:
        return await authorize(params.get("name"), _safe_loop(agent_loop_factory))
    except PlugConnectError as e:
        raise _as_rpc(e) from e


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
