"""Install-and-connect: the one transaction behind every plugin surface.

The disk transaction lives in :mod:`raven.plughub.install`; what lives here is
everything that has to happen *around* it -- the catalog lookup, the id rules,
the bounded connect kick, the rollback rule ("authentication failed => the
plugin was not installed"), the re-authorization retry and the removal.

It moved out of ``raven/rpc/methods/plughub.py`` when it grew a second caller.
The panel drives it over RPC; the agent's own ``plugin`` tool drives it
in-process from a turn. A rule with two implementations is two rules, and this
one decides whether a user's config keeps a server they never authorized.

Refusals leave as :class:`PlugConnectError` carrying the structured ``data``
the RPC layer echoes into ``error.data``; that layer translates them to its own
error type and the tool renders them as text.

Connect kicks are bounded by :data:`CONNECT_WAIT`: an OAuth connect legitimately
parks in the browser for minutes, so a mutating call returns the current
snapshot after a few seconds and the ``mcp.status`` / ``oauth.pending`` events
carry the rest of the story.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

CONNECT_WAIT = 8.0
# How long a *change* is waited for when the focus server is already settled. Long
# enough for a sync's own disconnect/reconnect of that server to land, short
# enough that a no-op toggle answers immediately.
_SETTLE_GRACE = 1.5

_SETTLED = ("connected", "error", "auth_required", "disconnected")


class PlugConnectError(Exception):
    """A user-visible refusal from an install / authorize / remove.

    ``data`` is the structured context the caller can act on (which field, which
    id), kept alongside the message so both surfaces report the same thing.
    """

    def __init__(self, detail: str, data: dict | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.data = data


class PlugRuntimeUnavailableError(PlugConnectError):
    """No live agent loop, so there is nothing that could run a connection.

    Separate from the rest because it is not the caller's argument being wrong:
    the RPC surface reports it as an internal error, not a validation one.
    """


def entry_auth_mode(entry: dict) -> str:
    """How a catalog entry's MCP contribution authenticates: oauth/apikey/none."""
    for piece in entry.get("contributes") or []:
        if piece.get("kind") == "mcp":
            return str((piece.get("auth") or {}).get("mode") or "none")
    return "none"


def entry_required_fields(entry: dict) -> list[str]:
    """Keys of the form fields an entry cannot install without.

    Every one of them is a credential the user types (a PAT, an API key), which
    is why a caller that cannot collect secrets has to check this *before*
    starting a transaction that would only roll back on the missing field.
    """
    for piece in entry.get("contributes") or []:
        if piece.get("kind") == "mcp":
            fields = (piece.get("auth") or {}).get("fields") or []
            return [str(f.get("key") or "") for f in fields if not f.get("optional")]
    return []


def installed_names() -> set[str]:
    """Every plugin/server name this install knows: ledgers plus config."""
    from raven.config.loader import load_config
    from raven.plughub import read_ledgers

    names = set(read_ledgers())
    try:
        names.update(load_config().tools.mcp_servers)
    except Exception:  # noqa: BLE001
        pass
    return names


def validated_catalog_id(raw: Any, field: str = "id") -> str:
    """A catalog id that is safe to use as a filename, or a typed refusal.

    The ledger enforces this too -- this exists so the refusal reaches the caller
    as a validation error naming the field, instead of surfacing as
    internal_error from a path join three modules down.
    """
    from raven.plughub.ledger import LedgerIdError, validate_catalog_id

    try:
        return validate_catalog_id(str(raw or ""))
    except LedgerIdError as e:
        raise PlugConnectError(str(e), data={"field": field, "value": raw}) from e


def server_name(raw: Any, field: str = "name") -> str:
    """A configured server's name, which is not a catalog id.

    :func:`installed_names` lists every key in ``tools.mcpServers``, hand-written
    ones included, so the panel offers them for removal, toggling and re-auth.
    Pushing those through the catalog-id rule refused any name that could not be
    a filename -- a leading underscore, a space, over 64 characters -- and told
    the user their own server name was "not a usable catalog id", a term they
    never used and could not act on. The filename rule belongs to the install,
    where the id really does become ``plugins/<id>.json``; here the config and
    the ledger are what answer.
    """
    name = str(raw or "").strip()
    if not name:
        raise PlugConnectError(f"{field} is required", data={"field": field})
    return name


def _log_sync_outcome(task: "asyncio.Task") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("plug: sync failed: {}", exc)


def _log_connect_outcome(task: "asyncio.Task") -> None:
    """Report a connect that outlived the caller waiting on it."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("plug: a connect left running behind an authorization failed: {}", exc)


def _state_of(manager: Any, name: str) -> tuple[dict | None, str | None]:
    snap = next((s for s in manager.status() if s["name"] == name), None)
    return snap, (snap or {}).get("state")


def _park_baseline(name: str | None) -> str | None:
    """The authorization link this server is parked on right now, if any."""
    from raven.mcp.oauth import pending_url

    return pending_url(name) if name else None


async def _await_focus(
    manager: Any,
    focus: str,
    *,
    before: str | None,
    window: float,
    task: "asyncio.Task",
    park_baseline: str | None = None,
) -> dict | None:
    """The one wait policy behind every connect kick.

    Ends on the first of: ``focus`` changing to a settled state, ``focus``
    parking at the browser-authorization step, the kicking task finishing, or
    ``window`` elapsing. Returns whatever snapshot is true at that moment.

    It ends on a *change*, not on the state happening to look settled: disabling
    a connected server would otherwise answer "connected" the instant it is
    asked, because that is still true then -- and the caller would show a server
    it just switched off as running.

    The park check is what keeps a human off the critical path. There is one
    policy rather than a per-caller one because the alternative was measured:
    the re-authorize path waited its whole window on a browser round-trip that
    had already published its URL, so the answer the caller needed sat ready for
    eight seconds before it was handed over.

    ``park_baseline`` is the authorization URL this server was *already* parked
    on when the caller kicked, and it is why the park check compares links rather
    than asking whether one exists. Re-authorizing a server that is already
    parked supersedes its link -- so without the baseline the wait returned on
    the old attempt's URL before the new attempt had even taken the lock, and
    handed the user a link that was about to stop redeeming.
    """
    from raven.mcp.oauth import pending_url

    deadline = asyncio.get_running_loop().time() + window
    while True:
        snap, state = _state_of(manager, focus)
        if state != before and state in _SETTLED:
            return snap
        url = pending_url(focus)
        if url is not None and url != park_baseline:
            # From here the connect blocks on the user in their browser; holding
            # the caller open for the rest of the window only makes it feel stuck.
            return snap
        if task.done() or asyncio.get_running_loop().time() >= deadline:
            return snap
        await asyncio.sleep(0.05)


async def kick_sync(loop: Any, focus: str | None = None) -> dict | None:
    """Reconcile live connections with the fresh config, waiting at most
    :data:`CONNECT_WAIT` -- slow (OAuth) connects continue in the background and
    stream their progress over events.

    With a ``focus`` server the wait is opportunistic: it ends the moment that
    server settles or parks at the browser-authorization step -- from then on the
    connect blocks on the user, and holding the caller open for the rest of the
    window would only make it feel stuck.
    """
    manager = getattr(loop, "mcp_manager", None)
    if loop is None or not hasattr(loop, "apply_mcp_config") or manager is None:
        return None
    from raven.config.loader import load_config

    # Read the pre-sync state first: the wait below ends on a *change*, not on
    # the state happening to look settled. Disabling a connected server would
    # otherwise return "connected" the instant it is asked, because that is still
    # true at that moment -- and the caller would show a server it just switched
    # off as running.
    before = _state_of(manager, focus)[1] if focus is not None else None
    baseline = _park_baseline(focus)

    try:
        servers = load_config().tools.mcp_servers
    except (ValueError, OSError) as e:
        # Same reason as authorize(): a config this build rejects must not
        # surface as an internal error from every install and toggle.
        raise PlugConnectError(f"config could not be read: {e}") from e
    task = asyncio.create_task(loop.apply_mcp_config(servers))
    task.add_done_callback(_log_sync_outcome)

    if focus is None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=CONNECT_WAIT)
        except asyncio.TimeoutError:
            pass
        except Exception:  # noqa: BLE001 — logged by the done callback
            pass
        return None

    # A focus that is already settled may have nothing to transition to (a toggle
    # that re-sends the current value, a server parked in error that sync skips).
    # Waiting the full window on those would hold the caller open for as long as
    # some unrelated cold stdio server takes to download itself.
    window = _SETTLE_GRACE if before in _SETTLED else CONNECT_WAIT
    return await _await_focus(manager, focus, before=before, window=window, task=task, park_baseline=baseline)


async def install_and_connect(entry_id: Any, form: Any, loop: Any) -> dict:
    """Install one catalog entry and kick its connection.

    Returns ``{"installed", "pending", "ledger", "mcp", "auth_mode"}``. For an
    entry that authenticates, the install only counts once the connection
    authenticates: a *settled* auth failure inside the connect window rolls the
    whole transaction back and raises, while a browser round-trip that is merely
    still pending is reported as ``pending`` (the caller keeps the entry out of
    "installed" and resolves it from later events).
    """
    from raven.mcp.oauth import auth_wait_servers
    from raven.plughub import catalog_detail, install_plugin, uninstall_plugin
    from raven.plughub.install import PlugInstallError
    from raven.plughub.trust import HubTrustError

    entry_id = validated_catalog_id(entry_id)
    form = form or {}
    if not isinstance(form, dict):
        raise PlugConnectError("form must be an object of field values")
    try:
        entry = await catalog_detail(entry_id)
    except HubTrustError as e:
        raise PlugConnectError(str(e)) from e
    if entry is None:
        raise PlugConnectError("no such catalog entry", data={"id": entry_id})

    try:
        ledger = await install_plugin(entry, {str(k): str(v) for k, v in form.items()})
    except PlugInstallError as e:
        raise PlugConnectError(str(e), data={"id": entry_id}) from e
    except OSError as e:
        # A read-only home or a full disk: the transaction already rolled itself
        # back, and the caller can act on this. Reporting it as an internal error
        # with a traceback would read as a raven bug.
        raise PlugConnectError(f"the config could not be written: {e}", data={"id": entry_id}) from e

    snap = await kick_sync(loop, focus=entry_id)
    mode = entry_auth_mode(entry)
    state = (snap or {}).get("state")
    # A connect parked at the browser step is *also* reported as `auth_required`
    # by the manager (that state is how every poller learns who the wait is on),
    # so the state alone cannot tell a pending authorization from a failed one.
    # Rolling back on it would mean an OAuth plugin could never be installed at
    # all: the user gets "authentication failed" and an uninstalled plugin at the
    # same moment their browser opens the consent page. The pending map is what
    # separates the two.
    parked = entry_id in auth_wait_servers()

    if mode != "none" and not parked and state in ("error", "auth_required"):
        # Auth settled as failed inside the connect window: an unauthenticated
        # plugin is not installed, so undo the whole transaction.
        if loop is not None and hasattr(loop, "mcp_manager"):
            try:
                await loop.mcp_manager.disconnect(entry_id, drop=True)
            except Exception as e:  # noqa: BLE001 — rollback must not stop on teardown noise
                logger.warning("plug install rollback: disconnect of '{}' failed: {}", entry_id, e)
        try:
            await uninstall_plugin(entry_id)
        except PlugInstallError as e:
            logger.warning("plug install rollback: uninstall of '{}' failed: {}", entry_id, e)
        detail = (snap or {}).get("error") or state
        raise PlugConnectError(
            "authentication failed; the plugin was not installed",
            data={"id": entry_id, "detail": f"authentication failed ({detail}); {entry_id} was not installed"},
        )

    pending = mode != "none" and state != "connected"
    return {
        "installed": not pending,
        "pending": pending,
        "ledger": ledger,
        "mcp": snap,
        "auth_mode": mode,
    }


async def remove(name: Any, loop: Any) -> dict:
    """Disconnect and uninstall one plugin or hand-written server."""
    from raven.plughub import uninstall_plugin
    from raven.plughub.install import PlugInstallError

    name = server_name(name)
    if loop is not None and hasattr(loop, "mcp_manager"):
        try:
            await loop.mcp_manager.disconnect(name, drop=True)
        except Exception as e:  # noqa: BLE001 — a broken teardown must not block the uninstall
            logger.warning("plug remove: disconnect of '{}' failed: {}", name, e)
    try:
        result = await uninstall_plugin(name)
    except PlugInstallError as e:
        raise PlugConnectError(str(e), data={"field": "name", "name": name}) from e
    await kick_sync(loop)
    return result


async def authorize(name: Any, loop: Any, *, interactive: bool = True) -> dict:
    """Force-reconnect one configured server: the explicit (re-)authorize path.

    This is the retry entry point of the connection manager, which is the only
    one that re-attempts a server parked in ``auth_required`` or ``error``.

    ``interactive`` is whether this attempt may take a browser on the host raven
    is running on. The panel's ``plug.auth`` keeps it, unchanged. The agent's
    ``plugin`` tool passes False: the person who asked is at the far end of a
    turn, which on a gateway is not this host at all, so that path answers with
    the link instead.
    """
    from raven.config.loader import load_config
    from raven.sandbox import SandboxInitError

    name = server_name(name)
    if loop is None or not hasattr(loop, "mcp_manager"):
        raise PlugRuntimeUnavailableError("agent loop unavailable; cannot run authorization")
    try:
        cfg = load_config().tools.mcp_servers.get(name)
    except (ValueError, OSError) as e:
        # A config the running build rejects would otherwise turn every mutating
        # call here into an internal error with a traceback.
        raise PlugConnectError(f"config could not be read: {e}") from e
    if cfg is None:
        raise PlugConnectError("no such MCP server", data={"field": "name", "name": name})
    if not cfg.enabled:
        raise PlugConnectError("server is disabled; enable it first", data={"field": "name", "name": name})

    manager = loop.mcp_manager
    before = _state_of(manager, name)[1]
    baseline = _park_baseline(name)
    task = asyncio.create_task(
        manager.connect(name, cfg, executor_provider=loop._mcp_executor, interactive=interactive)
    )
    snap = await _await_focus(manager, name, before=before, window=CONNECT_WAIT, task=task, park_baseline=baseline)
    if task.done() and not task.cancelled():
        # A connect that finished inside the window may have finished by raising.
        # Retrieving it here is not optional: unretrieved, the sandbox failure
        # below is only an asyncio warning on shutdown, and the caller is told
        # the server merely did not come up.
        exc = task.exception()
        if isinstance(exc, SandboxInitError):
            # The sandbox could not start, so a stdio server has nowhere to run.
            # That is a condition of this machine the caller can act on, not a
            # raven fault to report with a traceback tail.
            raise PlugConnectError(f"the sandbox could not start: {exc}", data={"field": "name", "name": name}) from exc
        if exc is not None:
            raise exc
    elif not task.done():
        # Still running: it is parked on the user, or on a slow handshake. Nobody
        # awaits it now, so it has to report for itself.
        task.add_done_callback(_log_connect_outcome)
    logger.debug("plug authorize: returning for '{}' with state {}", name, (snap or {}).get("state"))
    return {"name": name, "mcp": snap}


def installed_overview(loop: Any) -> list[dict]:
    """Every installed plugin with its live connection state, stable by name.

    Reads the config and the ledgers -- not just the connection manager -- so a
    plugin that is installed but has never connected is still listed. ``origin``
    is the ledger's provenance answer: "market" for a catalog install, "manual"
    for a server someone wrote into the config by hand.

    ``awaiting_auth`` comes from the pending map rather than the state, because
    the state cannot answer it alone: an *interactive* connect parked at the
    browser stays ``connecting`` (nothing flips it -- the caller that asked is
    assumed to be watching), so a later reader sees a slow handshake where the
    truth is that someone has a consent page open.
    """
    from raven.config.loader import load_config
    from raven.mcp.oauth import auth_wait_servers
    from raven.plughub import read_ledger

    manager = getattr(loop, "mcp_manager", None) if loop is not None else None
    live = {s["name"]: s for s in manager.status()} if manager is not None else {}
    tools_by_server: dict[str, list[str]] = {}
    if manager is not None:
        for tool, server in manager.tool_map().items():
            tools_by_server.setdefault(server, []).append(tool)

    try:
        configured = dict(load_config().tools.mcp_servers)
    except Exception:  # noqa: BLE001 — a broken config must still let the caller list what it can
        configured = {}

    parked = auth_wait_servers()
    rows = []
    for name in sorted(set(configured) | set(live)):
        cfg = configured.get(name)
        snap = live.get(name) or {}
        rows.append(
            {
                "name": name,
                "awaiting_auth": name in parked or snap.get("state") == "auth_required",
                "origin": "market" if read_ledger(name) is not None else "manual",
                "enabled": bool(getattr(cfg, "enabled", True)) if cfg is not None else snap.get("enabled", True),
                "auth": str(getattr(cfg, "auth", "none") or "none") if cfg is not None else "none",
                "state": snap.get("state") or "disconnected",
                "tool_count": snap.get("tool_count") or 0,
                "error": snap.get("error"),
                "tools": sorted(tools_by_server.get(name, [])),
            }
        )
    return rows


__all__ = [
    "CONNECT_WAIT",
    "PlugConnectError",
    "PlugRuntimeUnavailableError",
    "authorize",
    "entry_auth_mode",
    "entry_required_fields",
    "install_and_connect",
    "installed_names",
    "installed_overview",
    "kick_sync",
    "remove",
    "server_name",
    "validated_catalog_id",
]
