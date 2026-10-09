"""The ``plugin`` agent tool — the conversation's entry into the plugin market.

What these pin, beyond "it returns a string": every answer the model acts on has
to be distinguishable from every other one. A pending authorization must not
read like a success, a settled auth failure must say the plugin is *not*
installed, and a plugin whose install needs a secret must be refused by name
rather than by asking for the secret.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from raven.agent.loop.bundles import TurnPolicy
from raven.agent.tools.plughub import PluginTool
from raven.agent.tools.registry import ToolRegistry
from raven.contracts.tool import Tool
from raven.market import install as install_mod
from raven.market import ledger as ledger_mod
from raven.market.ledger import read_ledger


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(install_mod, "_config_path", lambda: cfg_path)
    monkeypatch.setattr(ledger_mod, "_plugins_dir", lambda: tmp_path / "plugins")
    (tmp_path / "plugins").mkdir()
    import raven.mcp.oauth as oauth

    monkeypatch.setattr(oauth, "delete_credentials", lambda server: None)
    monkeypatch.setattr(oauth, "_PENDING", {})

    monkeypatch.setattr("raven.home._current_config_path", cfg_path)
    yield {"cfg_path": cfg_path}


def _entry(auth_mode: str = "none", entry_id: str = "svc", fields: list | None = None) -> dict:
    contrib = {
        "kind": "mcp",
        "connection": {"type": "streamableHttp", "url": "https://svc.example/mcp"},
        "auth": {"mode": auth_mode},
    }
    if fields is not None:
        contrib["auth"]["fields"] = fields
    return {
        "id": entry_id,
        "version": "1.0.0",
        "name": {"en": entry_id.title()},
        "summary": {"en": "a service"},
        "contributes": [contrib],
    }


def _patch_catalog(monkeypatch, *entries):
    by_id = {e["id"]: e for e in entries}

    async def detail(entry_id: str):
        return by_id.get(entry_id)

    monkeypatch.setattr("raven.market.catalog_detail", detail)
    return by_id


class _FakeManager:
    """The manager surface the tool and the engine reach for."""

    def __init__(self, name: str, state: str, tools: list[str] | None = None):
        self.name = name
        self.state = state
        self.tools = tools or []
        self.dropped: list[str] = []
        self.interactive: bool | None = None
        self.offers: set[str] = set()

    def status(self) -> list[dict]:
        return [
            {
                "name": self.name,
                "transport": "streamableHttp",
                "state": self.state,
                "connected": self.state == "connected",
                "tool_count": len(self.tools) if self.state == "connected" else 0,
                "error": "boom" if self.state == "error" else None,
                "enabled": True,
            }
        ]

    def tool_map(self) -> dict[str, str]:
        return {t: self.name for t in self.tools} if self.state == "connected" else {}

    def servers_offering(self, primitive: str) -> list[str]:
        return [self.name] if self.state == "connected" and primitive in self.offers else []

    async def disconnect(self, name: str, *, drop: bool = False) -> None:
        self.dropped.append(name)

    async def connect(self, name, cfg, *, executor_provider=None, interactive: bool = True):
        self.interactive = interactive
        self.state = "connected"
        return self.status()[0]


class _FakeLoop:
    def __init__(self, name: str, state: str, tools: list[str] | None = None):
        self.mcp_manager = _FakeManager(name, state, tools)

    async def apply_mcp_config(self, servers) -> None:
        pass

    async def mcp_executor_provider(self):
        return None


def test_the_fakes_do_not_invent_a_contract() -> None:
    """Same guard as the RPC suite: a fake with a method the real thing lacks
    turns this whole file green while the tool raises AttributeError live."""
    from raven.agent.loop.main import AgentLoop
    from raven.mcp.manager import MCPConnectionManager

    for attr in [k for k in vars(_FakeLoop) if not k.startswith("__")]:
        assert hasattr(AgentLoop, attr), f"_FakeLoop.{attr} is not on AgentLoop"
    for attr in [k for k, v in vars(_FakeManager).items() if not k.startswith("__") and callable(v)]:
        assert hasattr(MCPConnectionManager, attr), f"_FakeManager.{attr} is not on MCPConnectionManager"


# ── shape ──────────────────────────────────────────────────────────


def test_the_schema_offers_no_way_to_pass_a_secret_or_a_url() -> None:
    """The security boundary is the argument list, not the prose: a field for a
    token or an endpoint is one the model will eventually fill in."""
    props = PluginTool().parameters["properties"]
    assert set(props) == {"action", "query", "name", "confirm"}
    text = json.dumps(props).lower()
    for banned in ("token", "api_key", "apikey", "secret", "password", "url", "command", "headers", "env"):
        assert banned not in text, f"the schema exposes a {banned!r} argument"


def test_the_description_states_both_refusals() -> None:
    desc = PluginTool().description.lower()
    assert "catalog" in desc
    assert "never put an api key" in desc


async def test_an_unknown_action_is_refused_without_touching_anything() -> None:
    out = await PluginTool().execute(action="publish")
    assert out.startswith("Error")
    assert "find" in out


# ── find ───────────────────────────────────────────────────────────


async def test_find_lists_ids_with_what_they_need() -> None:
    """Driven over the bundled catalog, so this needs no network."""
    out = await PluginTool().execute(action="find", query="asana")
    assert "asana" in out
    assert "authenticates with oauth" in out


async def test_find_falls_back_to_near_matches_on_a_typo() -> None:
    out = await PluginTool().execute(action="find", query="asanna")
    assert "asana" in out, out


async def test_find_says_so_when_nothing_is_close() -> None:
    out = await PluginTool().execute(action="find", query="zzzqqq-not-a-product")
    assert "No plugin in the catalog matches" in out


async def test_find_names_an_agent_or_a_channel_that_is_not_a_plugin() -> None:
    """Seen live: "connect openclaw" searched the plugin catalog ten times --
    down to single letters -- because "claw" matched firecrawl and nothing said
    OpenClaw is an agent Raven dispatches to."""
    agent = await PluginTool().execute(action="find", query="openclaw")
    assert "No plugin is called 'openclaw'" in agent
    assert 'raven_config add subagents {"preset": "openclaw"}' in agent
    channel = await PluginTool().execute(action="find", query="wechat")
    assert "raven_config describe channels.weixin" in channel
    assert "raven_config" not in await PluginTool().execute(action="find", query="asana")


async def test_find_for_a_generation_tool_says_raven_has_its_own_and_hides_nothing() -> None:
    """Asked whether it could generate images, the agent searched the plugin catalog twice before looking
    at its own image tool. The pointer is added, never swapped in: a plugin may
    carry such a tool too."""
    image = await PluginTool().execute(action="find", query="image generation")
    assert "tools.media.<kind>.model" in image and "catalog match" in image
    brave = await PluginTool().execute(action="find", query="brave search")
    assert "brave-search" in brave and "raven_config" not in brave


# ── connect ────────────────────────────────────────────────────────


async def test_connect_reports_the_tools_a_no_auth_plugin_registered(monkeypatch) -> None:
    _patch_catalog(monkeypatch, _entry("none"))
    loop = _FakeLoop("svc", "connected", ["mcp_svc_a", "mcp_svc_b"])

    out = await PluginTool(loop=loop).execute(action="connect", name="svc")

    assert "Connected 'svc'" in out
    assert "mcp_svc_a, mcp_svc_b" in out
    assert read_ledger("svc") is not None


async def test_connect_returns_the_authorization_url_without_waiting(monkeypatch) -> None:
    """The whole point: an OAuth connect parks on a person, and the tool has to
    come back with the link instead of holding the turn for 15 minutes."""
    import raven.mcp.oauth as oauth

    _patch_catalog(monkeypatch, _entry("oauth"))
    loop = _FakeLoop("svc", "auth_required")
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    oauth._PENDING["st"] = oauth._Pending("svc", fut, "https://idp.example/authorize?state=st")

    out = await PluginTool(loop=loop).execute(action="connect", name="svc")

    assert "waiting for the user to authorize" in out
    assert "https://idp.example/authorize?state=st" in out
    # Parked is not failed: the transaction stands, so the completed
    # authorization has a config entry to land against.
    assert read_ledger("svc") is not None
    fut.cancel()


async def test_connect_takes_nobodys_screen(monkeypatch) -> None:
    """A turn's connect opens no browser, and does not claim it did.

    The install runs through ``sync``, whose flow deliberately did not open the
    page -- and on a gateway serving an IM channel the browser this host could
    open is the operator's, not the screen of whoever asked. The link in the
    answer is the whole delivery.
    """
    import webbrowser

    import raven.mcp.oauth as oauth

    _patch_catalog(monkeypatch, _entry("oauth"))
    loop = _FakeLoop("svc", "auth_required")
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    oauth._PENDING["st"] = oauth._Pending("svc", fut, "https://idp.example/authorize?state=st")
    monkeypatch.setattr(oauth, "open_browser", lambda *a, **k: pytest.fail("the tool opened a browser"))
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("the tool reached webbrowser"))

    out = await PluginTool(loop=loop).execute(action="connect", name="svc")

    assert "No page was opened" in out
    assert "opened in the user's browser" not in out
    fut.cancel()


async def test_connect_reports_a_settled_failure_as_settled(monkeypatch) -> None:
    """A no-auth entry stays installed when its handshake fails, and `sync` does
    not retry a server in `error` -- so the catch-all's "still coming up" told
    the user to wait for something that had already stopped."""
    _patch_catalog(monkeypatch, _entry("none"))
    loop = _FakeLoop("svc", "error")

    out = await PluginTool(loop=loop).execute(action="connect", name="svc")

    assert "connection failed: boom" in out
    assert "still coming up" not in out
    assert "Nothing retries it on its own" in out
    assert "plugin(action='authorize', name='svc')" in out
    # Installed either way: a plugin that needs no credential is not rolled back
    # by a handshake failure.
    assert read_ledger("svc") is not None


async def test_connect_reports_a_running_handshake_as_in_flight(monkeypatch) -> None:
    """The other half of the same distinction: `connecting` really is in flight,
    so this one may say to wait."""
    _patch_catalog(monkeypatch, _entry("none"))
    loop = _FakeLoop("svc", "connecting")

    out = await PluginTool(loop=loop).execute(action="connect", name="svc")

    assert "handshake is still running" in out
    assert "connection failed" not in out


async def test_connect_says_not_installed_when_authentication_settles_as_failed(monkeypatch) -> None:
    _patch_catalog(monkeypatch, _entry("oauth"))
    loop = _FakeLoop("svc", "error")

    out = await PluginTool(loop=loop).execute(action="connect", name="svc")

    assert out.startswith("Error")
    assert "was not installed" in out
    assert read_ledger("svc") is None, "the rollback must reach the ledger"
    assert loop.mcp_manager.dropped == ["svc"]


async def test_connect_refuses_a_plugin_whose_install_needs_a_secret(monkeypatch) -> None:
    _patch_catalog(monkeypatch, _entry("apikey", fields=[{"key": "SVC_TOKEN", "into": "env.SVC_TOKEN"}]))
    loop = _FakeLoop("svc", "connected")

    out = await PluginTool(loop=loop).execute(action="connect", name="svc")

    assert out.startswith("Error")
    assert "SVC_TOKEN" in out
    assert "plugin panel" in out
    assert read_ledger("svc") is None, "nothing may be written for a plugin that cannot be finished"


async def test_connect_offers_near_matches_for_an_unknown_id(monkeypatch) -> None:
    out = await PluginTool(loop=_FakeLoop("x", "connected")).execute(action="connect", name="asanna")
    assert out.startswith("Error")
    assert "asana" in out


async def test_connect_finds_an_entry_the_user_named_by_its_product() -> None:
    """ "connect jira" is the phrasing a user actually types, and the catalog files
    Jira under `atlassian` -- named in the summary, which the fuzzy scorer never
    reads. Consulting suggest alone answered that Jira is absent and offered two
    unrelated products, with the real entry one search away."""
    out = await PluginTool(loop=_FakeLoop("x", "connected")).execute(action="connect", name="jira")

    assert out.startswith("Error")
    assert "atlassian" in out
    assert "plugin(action='connect', name='atlassian')" in out
    assert "airtable" not in out, "the fuzzy near-misses displaced the entry that matches"


async def test_connect_does_not_reinstall_something_already_installed(monkeypatch) -> None:
    _patch_catalog(monkeypatch, _entry("none"))
    loop = _FakeLoop("svc", "connected", ["mcp_svc_a"])
    tool = PluginTool(loop=loop)
    await tool.execute(action="connect", name="svc")

    out = await tool.execute(action="connect", name="svc")

    assert "already installed" in out
    assert "nothing was changed" in out


# ── list / authorize / remove ──────────────────────────────────────


async def test_list_reports_state_and_tool_count(_isolated, monkeypatch) -> None:
    _isolated["cfg_path"].write_text(
        json.dumps({"tools": {"mcpServers": {"svc": {"type": "streamableHttp", "url": "https://svc.example/mcp"}}}})
    )
    loop = _FakeLoop("svc", "connected", ["mcp_svc_a"])

    out = await PluginTool(loop=loop).execute(action="list")

    assert "svc" in out
    assert "state connected" in out
    assert "1 tools" in out
    assert "mcp_svc_a" in out
    assert "not connected: tell the user" in out


async def test_an_empty_list_tells_the_agent_to_say_so_rather_than_work_around_it(_isolated, monkeypatch) -> None:
    """Seen live: told only "no plugins", the agent read a GitHub PR through the
    browser and never mentioned that GitHub was not connected."""
    monkeypatch.setattr("raven.market.connect.installed_overview", lambda loop: [])
    out = await PluginTool(loop=_FakeLoop("svc", "connected", [])).execute(action="list")

    assert out.startswith("No plugins are installed.")
    assert "tell the user so and offer to connect it" in out


async def test_list_points_at_authorize_for_a_parked_plugin(_isolated) -> None:
    _isolated["cfg_path"].write_text(
        json.dumps({"tools": {"mcpServers": {"svc": {"type": "streamableHttp", "url": "https://svc.example/mcp"}}}})
    )
    out = await PluginTool(loop=_FakeLoop("svc", "auth_required")).execute(action="list")
    assert "Awaiting authorization: svc" in out


async def test_list_sees_a_browser_wait_the_state_alone_does_not_show(_isolated, monkeypatch) -> None:
    """An interactive connect parked at the browser stays `connecting`, so the
    state alone reads as a slow handshake when someone has a consent page open."""
    _isolated["cfg_path"].write_text(
        json.dumps({"tools": {"mcpServers": {"svc": {"type": "streamableHttp", "url": "https://svc.example/mcp"}}}})
    )
    monkeypatch.setattr("raven.mcp.oauth.auth_wait_servers", lambda: {"svc"})

    out = await PluginTool(loop=_FakeLoop("svc", "connecting")).execute(action="list")

    assert "waiting for the user to authorize it in the browser" in out
    assert "Awaiting authorization: svc" in out


async def test_authorize_reconnects_and_names_the_tools(_isolated) -> None:
    _isolated["cfg_path"].write_text(
        json.dumps({"tools": {"mcpServers": {"svc": {"type": "streamableHttp", "url": "https://svc.example/mcp"}}}})
    )
    loop = _FakeLoop("svc", "auth_required", ["mcp_svc_a"])

    out = await PluginTool(loop=loop).execute(action="authorize", name="svc")

    assert "authorized and connected" in out
    assert "mcp_svc_a" in out


class _Named(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "stub"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return "ran"


def _offered(reg: ToolRegistry) -> set[str]:
    return {d["function"]["name"] for d in reg.get_definitions()}


@pytest.mark.parametrize("action", ["connect", "authorize"])
async def test_the_tools_a_call_connects_are_usable_in_the_same_turn(_isolated, monkeypatch, action) -> None:
    """The result says "from the next step on", and the turn freeze used to make
    that false: the agent saw the tools named and could not call them until the
    user sent another message."""
    _patch_catalog(monkeypatch, _entry("none"))
    if action == "authorize":
        _isolated["cfg_path"].write_text(
            json.dumps({"tools": {"mcpServers": {"svc": {"type": "streamableHttp", "url": "https://svc.example/mcp"}}}})
        )
    reg = ToolRegistry()
    loop = _FakeLoop("svc", "connected" if action == "connect" else "auth_required", ["mcp_svc_a"])
    tool = PluginTool(loop=loop, registry=reg)
    reg.register(tool)

    with reg.turn_scope():
        reg.register(_Named("mcp_svc_a"))
        assert "mcp_svc_a" not in _offered(reg)
        out = await reg.execute("plugin", {"action": action, "name": "svc"})
        assert "mcp_svc_a" in out
        assert "mcp_svc_a" in _offered(reg)
        assert await reg.execute("mcp_svc_a", {}) == "ran"


@pytest.mark.parametrize("offers", [set(), {"resources"}, {"prompts"}])
async def test_a_connect_admits_the_meta_tools_its_server_brought(monkeypatch, offers) -> None:
    """The resource and prompt meta-tools register without an origin, so a
    server that is the first to offer resources would otherwise be connected
    with its resources unreachable for the rest of the turn."""
    from raven.mcp.prompts import PROMPT_TOOL_NAMES
    from raven.mcp.resources import RESOURCE_TOOL_NAMES

    _patch_catalog(monkeypatch, _entry("none"))
    reg = ToolRegistry()
    loop = _FakeLoop("svc", "connected", ["mcp_svc_a"])
    loop.mcp_manager.offers = offers
    reg.register(PluginTool(loop=loop, registry=reg))
    meta = set(RESOURCE_TOOL_NAMES | PROMPT_TOOL_NAMES)

    with reg.turn_scope():
        for name in ["mcp_svc_a", *sorted(meta)]:
            reg.register(_Named(name))
        await reg.execute("plugin", {"action": "connect", "name": "svc"})
        expected = set()
        if "resources" in offers:
            expected |= RESOURCE_TOOL_NAMES
        if "prompts" in offers:
            expected |= PROMPT_TOOL_NAMES
        assert _offered(reg) & meta == expected


async def test_authorize_from_a_turn_does_not_take_this_hosts_browser(_isolated) -> None:
    """``interactive`` is what permits the OAuth flow to open a page, and a turn
    is not it: the person who asked is at the far end of a channel, which on a
    gateway is not this machine."""
    _isolated["cfg_path"].write_text(
        json.dumps({"tools": {"mcpServers": {"svc": {"type": "streamableHttp", "url": "https://svc.example/mcp"}}}})
    )
    loop = _FakeLoop("svc", "auth_required", ["mcp_svc_a"])

    await PluginTool(loop=loop).execute(action="authorize", name="svc")

    assert loop.mcp_manager.interactive is False


async def test_authorize_reports_a_slow_handshake_as_in_flight(_isolated) -> None:
    """`connecting` outliving the window is not a refusal. Reporting it as one
    invites an immediate retry, which supersedes the attempt still running."""
    _isolated["cfg_path"].write_text(
        json.dumps({"tools": {"mcpServers": {"svc": {"type": "streamableHttp", "url": "https://svc.example/mcp"}}}})
    )
    loop = _FakeLoop("svc", "connecting")

    async def still_connecting(name, cfg, *, executor_provider=None, interactive: bool = True):
        return loop.mcp_manager.status()[0]

    loop.mcp_manager.connect = still_connecting

    out = await PluginTool(loop=loop).execute(action="authorize", name="svc")

    assert "handshake is still running" in out
    assert "did not authorize" not in out


async def test_authorize_refuses_a_server_that_is_not_configured(_isolated) -> None:
    _isolated["cfg_path"].write_text("{}")
    out = await PluginTool(loop=_FakeLoop("svc", "disconnected")).execute(action="authorize", name="nope")
    assert out.startswith("Error")
    assert "no such MCP server" in out


async def test_remove_without_confirm_changes_nothing(monkeypatch) -> None:
    _patch_catalog(monkeypatch, _entry("none"))
    loop = _FakeLoop("svc", "connected")
    await PluginTool(loop=loop).execute(action="connect", name="svc")

    out = await PluginTool(loop=loop).execute(action="remove", name="svc")

    assert out.startswith("Error")
    assert "confirm=true" in out
    assert read_ledger("svc") is not None, "an unconfirmed removal must not remove"


async def test_remove_with_confirm_uninstalls(monkeypatch) -> None:
    _patch_catalog(monkeypatch, _entry("none"))
    loop = _FakeLoop("svc", "connected")
    tool = PluginTool(loop=loop)
    await tool.execute(action="connect", name="svc")

    out = await tool.execute(action="remove", name="svc", confirm=True)

    assert "Removed 'svc'" in out
    assert read_ledger("svc") is None


# ── registration ───────────────────────────────────────────────────


def test_a_real_loop_registers_the_tool(tmp_path) -> None:
    """A tool nobody registers is a tool the agent does not have -- which was the
    entire gap this closes."""
    from raven.agent.loop.main import AgentLoop
    from raven.providers.base import LLMProvider

    class _Stub(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")

        async def chat(self, messages, **kwargs):  # pragma: no cover - no turn is run
            raise AssertionError("no completion here")

        def get_default_model(self) -> str:
            return "stub"

    loop = AgentLoop(provider=_Stub(), workspace=tmp_path, model="stub", policy=TurnPolicy(max_iterations=1))
    tool = loop.tools.get("plugin")
    assert isinstance(tool, PluginTool)
    assert tool._registry is loop.tools, "a connect could not let its tools into the running turn"
    assert tool._loop is loop
    assert tool.blocking_interaction is False, "a tool that waits on a human would hold the turn open"
