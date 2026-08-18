"""``plugin`` — the agent's own entry into the plugin market.

Before this tool the agent had no way to reach PlugHub at all: asked to connect
an integration it could only send the user to the plugin panel, or hand-roll an
OAuth flow with ``exec`` -- which produces a token in a temp directory and a
server that was never configured. The engine was already there and already
good; it was only reachable from the RPC surface the panel talks to.

So this is deliberately a thin, honest shell over
:mod:`raven.plughub.connect`: the same install transaction, the same rollback
rule, the same bounded connect kick the panel runs. What it adds is the
conversation's half of the job -- fuzzy lookup, prose the model can act on, and
the authorization link, because a turn cannot wait for a person to click.

Three boundaries this tool does not cross:

* it installs only from the trusted catalog, never from a URL or command line
  the model composed -- entry ids in, nothing else;
* it accepts no credentials. A plugin whose install needs an API key is
  reported with the field's name so the user can enter it where secrets belong,
  rather than pasted through a tool call and into the transcript;
* it opens no browser. A turn answers with the authorization link and lets the
  surface the user is actually looking at offer it -- see
  :class:`raven.agent.tools.mcp_oauth._Flow` for why the host running a turn is
  not necessarily the machine the person who asked is sitting at.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from raven.agent.tools.base import Tool

_ACTIONS = ("find", "connect", "authorize", "list", "remove")

# The connect kick is bounded by ``CONNECT_WAIT`` and an authorization park
# returns early, so the only long pole left is a cold stdio server downloading
# itself. Well under the registry default; a plugin call that runs for minutes
# is a bug, not a slow network.
_TOOL_TIMEOUT = 120.0

_FIND_LIMIT = 8
_NEAR_LIMIT = 5


def _lang() -> str:
    from raven.config.loader import load_config

    try:
        return load_config().language
    except Exception:  # noqa: BLE001 — a catalog listing must not depend on a readable config
        return "en"


def _card(item: dict) -> str:
    """One catalog entry as a line the model can pick an id out of."""
    bits = [f"transport {item.get('transport') or 'unknown'}"]
    mode = item.get("auth_mode") or "none"
    bits.append("no authentication" if mode == "none" else f"authenticates with {mode}")
    if item.get("installed"):
        bits.append("ALREADY INSTALLED")
    summary = (item.get("summary") or "").strip()
    return f"- {item['id']} ({item.get('name') or item['id']}) - {summary} [{', '.join(bits)}]"


def _row(row: dict) -> str:
    """One installed plugin as a status line."""
    bits = [f"state {row['state']}", f"{row['tool_count']} tools"]
    if row.get("awaiting_auth"):
        bits.append("waiting for the user to authorize it in the browser")
    if not row["enabled"]:
        bits.append("disabled")
    if row["origin"] == "manual":
        bits.append("hand-written config entry, not from the catalog")
    if row.get("error"):
        bits.append(f"last error: {row['error']}")
    line = f"- {row['name']} [{', '.join(bits)}]"
    if row.get("tools"):
        line += f"\n  tools: {', '.join(row['tools'])}"
    return line


class PluginTool(Tool):
    """Find, connect, re-authorize and list market plugins from a turn."""

    timeout_seconds = _TOOL_TIMEOUT

    def __init__(self, loop: Any = None) -> None:
        # The agent loop itself: it owns the connection manager, the sandbox
        # executor an stdio server needs, and ``sync_mcp``. Held rather than
        # resolved per call because there is exactly one for the life of a loop,
        # and the tool is registered by that loop's own constructor.
        self._loop = loop

    @property
    def name(self) -> str:
        return "plugin"

    @property
    def description(self) -> str:
        return (
            "Connect third-party integrations (MCP plugins: Asana, Notion, Linear, "
            "GitHub, Stripe, Playwright, ...) from Raven's built-in plugin catalog, "
            "and report what is connected. Use it when the user asks to connect, add, "
            "install, re-authorize or check an integration.\n"
            "Actions:\n"
            "- find: search the catalog. `query` is a name or a description "
            "('asana', 'issue tracker'). Returns each entry's id, what it needs to "
            "authenticate, and whether it is already installed.\n"
            "- connect: install the catalog entry whose id is `name`, and connect it. "
            "For a plugin that uses OAuth this returns straight away with the "
            "provider's authorization URL -- it opens no page and does NOT wait for "
            "the user to finish, so give them the link and stop. If authorization "
            "settles as failed the plugin is not installed at all, and the result says "
            "so.\n"
            "- authorize: mint a fresh authorization link for an installed plugin that "
            "is awaiting it (state auth_required), or retry a connection that failed.\n"
            "- list: every installed plugin with its connection state and how many "
            "tools it registered. Call this before telling the user what is connected; "
            "do not answer that from memory.\n"
            "- remove: uninstall one. Only with confirm=true, and only when the user "
            "asked for that plugin to be removed in their own words -- never as "
            "cleanup of your own initiative.\n"
            "Limits, so you do not try: only catalog entries can be installed -- there "
            "is no way to point this at a URL, a package or a command line, and you "
            "must not compose one. Never put an API key, token, password or account "
            "name in these arguments: a plugin that needs a secret is reported with "
            "the field's name, and the user enters it in the plugin panel. A newly "
            "connected plugin's tools appear in your tool list from the next step on, "
            "not inside this call's result."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": "What to do.",
                },
                "query": {
                    "type": "string",
                    "description": "Search words, for find. A product name or what it is for.",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "For connect: the catalog id exactly as find reported it. For "
                        "authorize and remove: the installed plugin's name."
                    ),
                },
                "confirm": {
                    "type": "boolean",
                    "description": (
                        "Required by remove. Set it only when the user asked for this "
                        "plugin to be removed; never to confirm your own suggestion."
                    ),
                },
            },
            "required": ["action"],
        }

    def display_call(self, args: dict[str, Any]) -> str | None:
        action = str(args.get("action") or "").strip()
        target = str(args.get("name") or args.get("query") or "").strip()
        if action not in _ACTIONS:
            return None
        return f"plugin {action} {target}".strip()

    async def execute(
        self,
        action: str = "",
        query: str = "",
        name: str = "",
        confirm: bool = False,
        **_: Any,
    ) -> str:
        action = (action or "").strip().lower()
        if action not in _ACTIONS:
            return f"Error: 'action' must be one of {', '.join(_ACTIONS)}."
        if action == "find":
            return await self._find(query or name)
        if action == "list":
            return self._list()
        if action == "connect":
            return await self._connect(name or query)
        if action == "authorize":
            return await self._authorize(name)
        return await self._remove(name, confirm)

    # ── actions ────────────────────────────────────────────────────

    async def _find(self, query: str) -> str:
        from raven.plughub.trust import HubTrustError

        query = (query or "").strip()
        try:
            items = await self._lookup(query, _FIND_LIMIT)
        except HubTrustError as e:
            return f"Error: the plugin catalog is misconfigured and was refused: {e}"
        if not items:
            return (
                f"No plugin in the catalog matches {query!r}. Try a shorter word, or "
                f"call plugin(action='find') with no query to see the whole catalog."
            )
        shown = items[:_FIND_LIMIT]
        head = f"{len(items)} catalog match(es)" + (f" for {query!r}" if query else "")
        if len(items) > len(shown):
            head += f"; showing {len(shown)}"
        lines = [_card(it) for it in shown]
        return f"{head}:\n" + "\n".join(lines) + "\nConnect one with plugin(action='connect', name='<id>')."

    def _list(self) -> str:
        from raven.plughub.connect import installed_overview

        rows = installed_overview(self._loop)
        if not rows:
            return "No plugins are installed. plugin(action='find', query='...') searches the catalog."
        awaiting = [r["name"] for r in rows if r.get("awaiting_auth")]
        out = [f"{len(rows)} installed plugin(s):"] + [_row(r) for r in rows]
        if awaiting:
            out.append(
                "Awaiting authorization: "
                + ", ".join(awaiting)
                + ". plugin(action='authorize', name='<name>') mints a fresh authorization link."
            )
        return "\n".join(out)

    async def _connect(self, name: str) -> str:
        from raven.plughub import catalog_detail
        from raven.plughub.connect import (
            PlugConnectError,
            entry_required_fields,
            install_and_connect,
            installed_names,
        )
        from raven.plughub.trust import HubTrustError

        name = (name or "").strip()
        if not name:
            return "Error: 'name' is required for connect - the catalog id of the plugin to install."
        if self._loop is None:
            return "Error: no agent runtime is available, so a plugin cannot be connected right now."

        try:
            entry = await catalog_detail(name)
        except HubTrustError as e:
            return f"Error: the plugin catalog is misconfigured and was refused: {e}"
        if entry is None:
            near = await self._near(name)
            if len(near) == 1:
                it = near[0]
                return (
                    f"Error: no catalog entry has the id {name!r}, but one entry matches that "
                    f"word:\n{_card(it)}\nIf that is what the user meant, connect it with "
                    f"plugin(action='connect', name='{it['id']}')."
                )
            if near:
                return (
                    f"Error: no plugin with id {name!r} is in the catalog. Closest entries:\n"
                    + "\n".join(_card(it) for it in near)
                    + "\nUse one of those ids, or ask the user which they meant."
                )
            return (
                f"Error: no plugin with id {name!r} is in the catalog, and nothing looks close. "
                f"Search with plugin(action='find', query='...')."
            )

        if name in installed_names():
            return (
                f"'{name}' is already installed; nothing was changed.\n"
                + self._list()
                + f"\nIf it is awaiting authorization, plugin(action='authorize', name='{name}')."
            )

        required = entry_required_fields(entry)
        if required:
            fields = ", ".join(required)
            return (
                f"Error: '{name}' cannot be connected from here. It needs the credential "
                f"field(s) {fields}, and this tool does not accept secrets as arguments. "
                f"Ask the user to install '{name}' from the plugin panel, where that field "
                f"is entered and stored; then plugin(action='list') will show it."
            )

        try:
            result = await install_and_connect(name, {}, self._loop)
        except PlugConnectError as e:
            detail = (e.data or {}).get("detail") or e.detail
            return f"Error: connecting '{name}' failed: {detail}"

        return self._connect_report(name, result)

    def _connect_report(self, name: str, result: dict) -> str:
        from raven.agent.tools.mcp_oauth import OAUTH_FLOW_TIMEOUT, pending_url

        snap = result.get("mcp") or {}
        state = snap.get("state") or "unknown"
        if state == "connected":
            tools = self._tools_of(name)
            named = f": {', '.join(tools)}" if tools else ""
            return (
                f"Connected '{name}'. It registered {snap.get('tool_count') or len(tools)} tool(s){named}. "
                f"They are in your tool list from the next step on."
            )

        url = pending_url(name)
        if url:
            logger.info("plugin tool: authorization for '{}' is pending; reporting the link", name)
            return (
                f"'{name}' is installed and now waiting for the user to authorize it. No page was "
                f"opened for them, so the link below is the whole of it.\n"
                f"Authorization URL: {url}\n"
                f"Give the user that link and stop here - this call did not wait for them, and the "
                f"link is good for about {OAUTH_FLOW_TIMEOUT / 60:.0f} minutes. Once they finish, "
                f"'{name}'s tools register on their own and show up on a later turn. If they say it "
                f"failed, plugin(action='authorize', name='{name}')."
            )

        if state == "error":
            # Settled, not in flight: sync() does not retry a server in `error`,
            # so "still coming up" would have the agent tell the user to wait for
            # something nothing is going to finish.
            return (
                f"'{name}' is installed, but its connection failed: {snap.get('error') or 'no detail given'}. "
                f"Nothing retries it on its own - plugin(action='authorize', name='{name}') is the retry, and "
                f"plugin(action='remove', name='{name}', confirm=true) drops it if the user does not want it. "
                f"Report the failure rather than telling them to wait."
            )

        if result.get("pending"):
            return (
                f"'{name}' is installed but not authorized yet (state {state}) and no authorization "
                f"link was minted inside the connect window. Tell the user it needs authorizing, and "
                f"call plugin(action='authorize', name='{name}') to mint the link."
            )
        if state == "connecting":
            return (
                f"'{name}' is installed; its handshake is still running (state connecting). Its tools "
                f"register when it finishes. plugin(action='list') shows where it got to."
            )
        return (
            f"'{name}' is installed, but its connection is not up (state {state}) and this call is not "
            f"waiting for one. plugin(action='authorize', name='{name}') retries it; "
            f"plugin(action='list') shows where it got to."
        )

    async def _authorize(self, name: str) -> str:
        from raven.agent.tools.mcp_oauth import OAUTH_FLOW_TIMEOUT, pending_url
        from raven.plughub.connect import PlugConnectError, authorize

        name = (name or "").strip()
        if not name:
            return "Error: 'name' is required for authorize - the installed plugin to re-authorize."
        if self._loop is None:
            return "Error: no agent runtime is available, so authorization cannot be run right now."
        try:
            out = await authorize(name, self._loop, interactive=False)
        except PlugConnectError as e:
            return f"Error: authorizing '{name}' failed: {e.detail}"

        snap = out.get("mcp") or {}
        state = snap.get("state") or "unknown"
        if state == "connected":
            tools = self._tools_of(name)
            named = f": {', '.join(tools)}" if tools else ""
            return f"'{name}' is authorized and connected, with {snap.get('tool_count') or len(tools)} tool(s){named}."
        url = pending_url(name)
        if url:
            return (
                f"'{name}' is waiting for the user to authorize it. No page was opened for them, so the "
                f"link below is the whole of it.\n"
                f"Authorization URL: {url}\n"
                f"Relay that link and stop; it is good for about {OAUTH_FLOW_TIMEOUT / 60:.0f} minutes and "
                f"this call did not wait for the click."
            )
        if state == "connecting":
            # In flight, not settled: the handshake outlived the connect window,
            # so calling this a refusal would report a failure that has not
            # happened -- and invite an immediate retry that supersedes it.
            return (
                f"'{name}''s handshake is still running (state connecting) and did not settle inside this "
                f"call's window. Tell the user it is still coming up and read plugin(action='list') on a "
                f"later turn rather than authorizing again now."
            )
        return (
            f"'{name}' did not authorize: state {state}"
            + (f", {snap['error']}" if snap.get("error") else "")
            + ". Report that to the user rather than retrying in a loop."
        )

    async def _remove(self, name: str, confirm: bool) -> str:
        from raven.plughub.connect import PlugConnectError, remove

        name = (name or "").strip()
        if not name:
            return "Error: 'name' is required for remove - the installed plugin to uninstall."
        if not confirm:
            return (
                f"Error: refusing to remove '{name}' without confirm=true. Removal deletes its "
                f"config entry and its stored credentials, so it needs the user's own request - "
                f"ask them, then repeat this call with confirm=true."
            )
        try:
            out = await remove(name, self._loop)
        except PlugConnectError as e:
            return f"Error: removing '{name}' failed: {e.detail}"
        origin = out.get("origin") or "market"
        return f"Removed '{name}' ({origin} entry), with its stored credentials. Its tools are gone from your list."

    # ── helpers ────────────────────────────────────────────────────

    async def _lookup(self, query: str, limit: int) -> list[dict]:
        """Catalog entries matching ``query``, best first, marked for installed.

        Both halves, because they answer different questions: ``catalog_search``
        reads the summary too, which is how "jira" reaches the entry the catalog
        files under ``atlassian``, while ``catalog_suggest`` only scores the id
        and the two languages of the name -- good for "asanna", blind to a
        product named after its vendor. A path that keeps the suggest half alone
        tells the user a plugin is absent while it sits one search away.
        """
        from raven.plughub import catalog_search, catalog_suggest
        from raven.plughub.connect import installed_names

        lang = _lang()
        items = await catalog_search(query, "", lang)
        if not items and query:
            items = await catalog_suggest(query, lang, limit=limit)
        installed = installed_names()
        for it in items:
            it["installed"] = it["id"] in installed
        return items

    async def _near(self, name: str) -> list[dict]:
        """What a rejected id might have meant. Trust is already checked by the
        ``catalog_detail`` call that rejected it, so this cannot be the first
        reader of a refused hub."""
        return (await self._lookup(name, _NEAR_LIMIT))[:_NEAR_LIMIT]

    def _tools_of(self, server: str) -> list[str]:
        """Tool names the live registry holds for one server."""
        manager = getattr(self._loop, "mcp_manager", None) if self._loop is not None else None
        if manager is None:
            return []
        return sorted(tool for tool, owner in manager.tool_map().items() if owner == server)


__all__ = ["PluginTool"]
