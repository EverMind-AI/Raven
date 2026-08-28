"""``plug.*`` handlers — install auth gate: an auth plugin only counts as
installed once its connection authenticates; settled failures roll back."""

from __future__ import annotations

import asyncio
import json

import pytest

from raven.market import install as install_mod
from raven.market import ledger as ledger_mod
from raven.market.ledger import read_ledger
from raven.rpc.errors import ConfigValidationError, InternalError
from raven.rpc.methods import plughub as rpc_plughub


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(install_mod, "_config_path", lambda: cfg_path)
    monkeypatch.setattr(ledger_mod, "_plugins_dir", lambda: tmp_path / "plugins")
    (tmp_path / "plugins").mkdir()
    import raven.mcp.oauth as oauth

    monkeypatch.setattr(oauth, "delete_credentials", lambda server: None)
    # The handlers read the loader's own config path (language, configured
    # servers), which is a module global -- point it at the fixture and put it
    # back, or a later test in this process reads a directory that has gone.
    import raven.config.loader as loader

    monkeypatch.setattr(loader, "_current_config_path", cfg_path)
    yield {"cfg_path": cfg_path}


def _entry(auth_mode: str, entry_id: str = "svc") -> dict:
    contrib = {
        "kind": "mcp",
        "connection": {"type": "streamableHttp", "url": "https://svc.example/mcp"},
        "auth": {"mode": auth_mode},
    }
    if auth_mode == "apikey":
        contrib["auth"]["fields"] = [{"key": "K", "into": "env.K", "secret": True}]
    return {"id": entry_id, "version": "1.0.0", "contributes": [contrib]}


class _FakeManager:
    def __init__(self, name: str, state: str):
        self.name = name
        self.state = state
        self.dropped: list[str] = []

    def status(self) -> list[dict]:
        # Every key `MCPConnectionManager._snapshot` returns. Three of them were
        # missing, and no test noticed: the guard below compares *attribute
        # names* against the real class, not the shape of what they return, and
        # the handler tests assert on individual keys rather than validating the
        # payload. `McpSnapshot` requires transport, connected and tool_count,
        # so the fake was producing a snapshot the contract forbids.
        return [
            {
                "name": self.name,
                "transport": "streamableHttp",
                "state": self.state,
                "connected": self.state == "connected",
                "tool_count": 1 if self.state == "connected" else 0,
                "error": "boom" if self.state == "error" else None,
                "enabled": True,
            }
        ]

    async def disconnect(self, name: str, *, drop: bool = False) -> None:
        self.dropped.append(name)


class _FakeLoop:
    """Stands in for the agent loop, with the surface `plug.*` actually uses.

    ``test_the_fake_loop_does_not_invent_a_contract`` keeps this honest: a fake
    that grows an attribute the real loop lacks turns every test in this file
    green while the handler raises AttributeError in production.
    """

    def __init__(self, name: str, state: str):
        self.mcp_manager = _FakeManager(name, state)

    async def apply_mcp_config(self, servers) -> None:
        pass

    async def _mcp_executor(self):
        return None


def _factory(loop):
    return lambda: loop


def _own_attrs(obj) -> list[str]:
    """Everything the fake itself defines: instance attributes *and* the methods
    on its class. The original bug shipped as fake *methods* (`apply_mcp_config`,
    `_mcp_executor`), so an instance-only check would have missed it."""
    from_class = [k for k, v in vars(type(obj)).items() if not k.startswith("__")]
    return sorted(set(vars(obj)) | set(from_class))


def test_the_fake_loop_does_not_invent_a_contract() -> None:
    from raven.agent.loop.main import AgentLoop

    fake = _FakeLoop("svc", "connected")
    for attr in _own_attrs(fake):
        assert hasattr(AgentLoop, attr), f"_FakeLoop.{attr} does not exist on AgentLoop"

    for attr in ("apply_mcp_config", "_mcp_executor", "mcp_manager"):
        assert hasattr(AgentLoop, attr), f"AgentLoop is missing {attr}"


def test_the_fake_manager_does_not_invent_a_contract() -> None:
    """Same guard one level down: the handlers reach through the loop into the
    manager, so a fake manager that grows a method the real one lacks reopens the
    identical hole.

    Methods only -- a fake's instance attributes are its own bookkeeping (what it
    recorded, what it was told to answer), which the real manager has no reason
    to carry."""
    from raven.mcp.manager import MCPConnectionManager

    methods = [k for k, v in vars(_FakeManager).items() if not k.startswith("__") and callable(v)]
    assert methods, "the guard would pass vacuously"
    for attr in methods:
        assert hasattr(MCPConnectionManager, attr), f"_FakeManager.{attr} is not on MCPConnectionManager"


def _patch_catalog(monkeypatch, entry):
    async def detail(entry_id: str):
        return entry if entry_id == entry["id"] else None

    monkeypatch.setattr("raven.market.catalog_detail", detail)


def _server_in_cfg(fixture, name: str) -> bool:
    try:
        cfg = json.loads(fixture["cfg_path"].read_text())
    except FileNotFoundError:
        return False
    return name in cfg.get("tools", {}).get("mcpServers", {})


async def test_oauth_connected_counts_installed(monkeypatch, _isolated):
    entry = _entry("oauth")
    _patch_catalog(monkeypatch, entry)
    loop = _FakeLoop("svc", "connected")
    r = await rpc_plughub.plug_install({"id": "svc"}, agent_loop_factory=_factory(loop))
    assert r["installed"] is True
    assert r["pending"] is False
    assert read_ledger("svc") is not None


async def test_oauth_still_connecting_reports_pending(monkeypatch, _isolated):
    entry = _entry("oauth")
    _patch_catalog(monkeypatch, entry)
    loop = _FakeLoop("svc", "connecting")
    r = await rpc_plughub.plug_install({"id": "svc"}, agent_loop_factory=_factory(loop))
    assert r["installed"] is False
    assert r["pending"] is True
    assert read_ledger("svc") is not None


async def test_a_connect_parked_at_the_browser_is_pending_not_a_failure(monkeypatch, _isolated):
    """`auth_required` means two different things, and only one of them is a
    failure.

    The manager moves a server there the moment its connect reaches the
    browser-authorization step -- that is how every poller learns who the wait
    is on -- so the state alone cannot tell a pending consent page from a
    rejected credential. Reading it as failure rolled the transaction back while
    the user's browser was still on the provider's consent screen: they got
    "authentication failed" and no plugin, and completing the authorization then
    had nothing to land against. The pending map is what separates the two.
    """
    entry = _entry("oauth")
    _patch_catalog(monkeypatch, entry)
    monkeypatch.setattr("raven.mcp.oauth.auth_wait_servers", lambda: {"svc"})
    loop = _FakeLoop("svc", "auth_required")

    r = await rpc_plughub.plug_install({"id": "svc"}, agent_loop_factory=_factory(loop))

    assert r["pending"] is True
    assert r["installed"] is False
    assert read_ledger("svc") is not None, "a pending authorization must keep its transaction"
    assert _server_in_cfg(_isolated, "svc")
    assert loop.mcp_manager.dropped == []


async def test_oauth_auth_failure_rolls_back(monkeypatch, _isolated):
    entry = _entry("oauth")
    _patch_catalog(monkeypatch, entry)
    loop = _FakeLoop("svc", "auth_required")
    with pytest.raises(ConfigValidationError, match="authentication failed"):
        await rpc_plughub.plug_install({"id": "svc"}, agent_loop_factory=_factory(loop))
    assert read_ledger("svc") is None
    assert not _server_in_cfg(_isolated, "svc")
    assert loop.mcp_manager.dropped == ["svc"]


async def test_apikey_error_rolls_back(monkeypatch, _isolated):
    entry = _entry("apikey")
    _patch_catalog(monkeypatch, entry)
    loop = _FakeLoop("svc", "error")
    with pytest.raises(ConfigValidationError, match="authentication failed"):
        await rpc_plughub.plug_install({"id": "svc", "form": {"K": "v"}}, agent_loop_factory=_factory(loop))
    assert read_ledger("svc") is None
    assert not _server_in_cfg(_isolated, "svc")


async def test_no_auth_error_still_installs(monkeypatch, _isolated):
    entry = _entry("none")
    _patch_catalog(monkeypatch, entry)
    loop = _FakeLoop("svc", "error")
    r = await rpc_plughub.plug_install({"id": "svc"}, agent_loop_factory=_factory(loop))
    assert r["installed"] is True
    assert r["pending"] is False
    assert read_ledger("svc") is not None


async def test_a_hostile_name_is_refused_as_a_validation_error(tmp_path, monkeypatch) -> None:
    """The ledger refuses the id either way; this pins *how the caller hears it*.

    Without validation at the handler boundary the refusal surfaces as
    ``-32603 internal_error`` with a traceback tail, which reads as a server
    fault rather than a bad argument -- and leaves a caller no field to blame.
    """
    cfg = tmp_path / "config.json"
    cfg.write_text('{"language": "en"}')

    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.plughub import register_plughub_methods

    d = Dispatcher()
    register_plughub_methods(d, agent_loop_factory=lambda: None)
    resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "plug.remove", "params": {"name": "../config"}})

    assert resp["error"]["code"] == -32011, resp
    assert resp["error"]["data"]["field"] == "name"
    assert cfg.exists(), "the config file must survive a hostile uninstall"


async def test_auth_reconnects_one_server(_isolated, monkeypatch) -> None:
    """plug.auth is the GUI's re-authorize button, and had no test at all."""
    _isolated["cfg_path"].write_text(
        json.dumps({"tools": {"mcpServers": {"svc": {"type": "streamableHttp", "url": "https://svc.example/mcp"}}}})
    )
    connected: list[tuple] = []

    class _Manager(_FakeManager):
        async def connect(self, name, cfg, *, executor_provider=None, interactive: bool = True):
            connected.append((name, executor_provider, interactive))
            self.state = "connected"
            return {"name": name, "state": "connected", "tool_count": 3, "error": None}

    loop = _FakeLoop("svc", "error")
    loop.mcp_manager = _Manager("svc", "error")

    out = await rpc_plughub.plug_auth({"name": "svc"}, agent_loop_factory=_factory(loop))

    assert out["mcp"]["state"] == "connected"
    assert connected[0][0] == "svc"
    # The executor provider has to be passed through, or a stdio server in a
    # sandbox connects without one.
    assert connected[0][1] is not None
    # The panel's button is the one caller whose screen this is, so it keeps the
    # browser -- the tool's own authorize passes False (test_plughub_tool.py).
    assert connected[0][2] is True


async def test_auth_returns_as_soon_as_the_flow_reaches_the_browser(_isolated, monkeypatch) -> None:
    """Re-authorize used to sit out its whole 8s window on a browser round-trip
    whose URL had already been published -- the answer the caller needed was
    ready and withheld. Both kicks now end on the same park check."""
    _isolated["cfg_path"].write_text(
        json.dumps({"tools": {"mcpServers": {"svc": {"type": "streamableHttp", "url": "https://svc.example/mcp"}}}})
    )
    parked: list[str] = []
    monkeypatch.setattr(
        "raven.mcp.oauth.pending_url",
        lambda server: parked[0] if parked else None,
    )

    class _Manager(_FakeManager):
        async def connect(self, name, cfg, *, executor_provider=None, interactive: bool = True):
            parked.append("https://idp.example/authorize?state=new")
            await asyncio.sleep(60)  # a real OAuth connect waits on the person
            raise AssertionError("unreachable in this test")

    loop = _FakeLoop("svc", "error")
    loop.mcp_manager = _Manager("svc", "connecting")

    t0 = asyncio.get_running_loop().time()
    out = await rpc_plughub.plug_auth({"name": "svc"}, agent_loop_factory=_factory(loop))
    elapsed = asyncio.get_running_loop().time() - t0

    assert elapsed < 1.0, f"held the caller for {elapsed:.1f}s while parked on the user"
    assert out["mcp"]["state"] == "connecting"


async def test_auth_does_not_answer_with_the_link_it_is_about_to_supersede(_isolated, monkeypatch) -> None:
    """Re-authorizing a server that is already parked mints a *new* link and kills
    the old one. A wait that ends on "some authorization is pending" returned
    before the new attempt had even taken the lock, so the caller got the link
    that was seconds from refusing to redeem."""
    _isolated["cfg_path"].write_text(
        json.dumps({"tools": {"mcpServers": {"svc": {"type": "streamableHttp", "url": "https://svc.example/mcp"}}}})
    )
    urls = ["https://idp.example/authorize?state=old"]
    monkeypatch.setattr("raven.mcp.oauth.pending_url", lambda server: urls[-1] if urls else None)

    class _Manager(_FakeManager):
        async def connect(self, name, cfg, *, executor_provider=None, interactive: bool = True):
            await asyncio.sleep(0.2)  # the handshake gets to the browser step
            urls.append("https://idp.example/authorize?state=new")
            await asyncio.sleep(60)
            raise AssertionError("unreachable in this test")

    loop = _FakeLoop("svc", "auth_required")
    loop.mcp_manager = _Manager("svc", "auth_required")

    await rpc_plughub.plug_auth({"name": "svc"}, agent_loop_factory=_factory(loop))

    from raven.mcp.oauth import pending_url

    assert pending_url("svc").endswith("state=new"), "returned on the superseded link"


async def test_auth_refuses_a_disabled_server(_isolated) -> None:
    _isolated["cfg_path"].write_text(
        json.dumps(
            {"tools": {"mcpServers": {"svc": {"type": "streamableHttp", "url": "https://x/mcp", "enabled": False}}}}
        )
    )
    loop = _FakeLoop("svc", "disconnected")

    with pytest.raises(ConfigValidationError, match="disabled"):
        await rpc_plughub.plug_auth({"name": "svc"}, agent_loop_factory=_factory(loop))


async def test_auth_without_a_running_loop_is_a_clean_refusal(_isolated) -> None:
    _isolated["cfg_path"].write_text(json.dumps({"tools": {"mcpServers": {"svc": {"url": "https://x/mcp"}}}}))

    with pytest.raises(InternalError):
        await rpc_plughub.plug_auth({"name": "svc"}, agent_loop_factory=None)


async def test_toggle_waits_for_the_disconnect_it_asked_for(_isolated) -> None:
    """The snapshot must describe the state after the sync, not before it.

    Disabling a connected server is the case where "does this look settled?" is
    the wrong question: `connected` is still true the instant the sync is kicked
    off, so an unguarded wait returns immediately and the caller shows a server
    it just switched off as running.
    """
    _isolated["cfg_path"].write_text(
        json.dumps({"tools": {"mcpServers": {"svc": {"type": "streamableHttp", "url": "https://svc.example/mcp"}}}})
    )

    class _Loop(_FakeLoop):
        async def apply_mcp_config(self, servers) -> None:
            await asyncio.sleep(0.15)  # the disconnect lands well after the kick
            self.mcp_manager.state = "disconnected"

    loop = _Loop("svc", "connected")

    out = await rpc_plughub.plug_toggle({"name": "svc", "enabled": False}, agent_loop_factory=_factory(loop))

    assert out["mcp"]["state"] == "disconnected", out


async def test_a_config_that_cannot_be_written_is_a_refusal(_isolated, monkeypatch) -> None:
    """A read-only home is a condition of the machine, not a raven fault: the
    transaction rolls back and the caller hears why."""

    def _boom(payload):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(install_mod, "_write_config_raw", _boom)
    _patch_catalog(monkeypatch, _entry("none", "svc"))

    with pytest.raises(ConfigValidationError, match="could not be written"):
        await rpc_plughub.plug_install({"id": "svc"}, agent_loop_factory=None)


async def test_a_hand_edited_stanza_that_is_not_an_object_is_a_refusal(_isolated) -> None:
    _isolated["cfg_path"].write_text(json.dumps({"tools": {"mcpServers": {"svc": "https://x/mcp"}}}))

    with pytest.raises(ConfigValidationError, match="cannot be toggled"):
        await rpc_plughub.plug_toggle({"name": "svc", "enabled": False}, agent_loop_factory=None)


# ---------------------------------------------------------------------------
# a hand-written server is a server, not a catalogue id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["_dev", "my server", "x" * 80])
async def test_toggle_and_remove_accept_a_hand_written_server_name(_isolated, monkeypatch, name) -> None:
    """`_installed_names` unions the ledgers with every key in `tools.mcpServers`,
    so the panel lists hand-written servers as installed. They were then refused
    by the catalogue-id rule -- which exists to stop an id naming a file, and a
    server name is never used as one. The user was told their own server name
    was "not a usable catalog id", with no way to act on it.
    """
    import raven.market as plughub_pkg

    toggled: list = []
    removed: list = []
    monkeypatch.setattr(plughub_pkg, "toggle_server", lambda n, e: toggled.append((n, e)))

    async def _uninstall(n):
        removed.append(n)
        return {"removed": True, "origin": "manual"}

    monkeypatch.setattr(plughub_pkg, "uninstall_plugin", _uninstall)

    await rpc_plughub.plug_toggle({"name": name, "enabled": False})
    assert toggled == [(name, False)]

    await rpc_plughub.plug_remove({"name": name})
    assert removed == [name]


@pytest.mark.parametrize("name", ["_dev", "my server"])
async def test_auth_accepts_a_hand_written_server_name(_isolated, name) -> None:
    """The third handler the same rule refused. Every existing auth test uses
    "svc", which the old catalogue-id rule already accepted, so none of them
    would notice this one being reverted.

    The refusal here is about the *state* (disconnected), which is proof the
    name got through: under the old rule it never reached the config lookup.
    """
    _isolated["cfg_path"].write_text(
        json.dumps({"tools": {"mcpServers": {name: {"url": "https://x/mcp", "enabled": False}}}})
    )
    loop = _FakeLoop(name, "disconnected")

    with pytest.raises(ConfigValidationError, match="disabled"):
        await rpc_plughub.plug_auth({"name": name}, agent_loop_factory=_factory(loop))


async def test_an_empty_server_name_is_still_refused(_isolated) -> None:
    """Relaxing the rule must not mean accepting nothing at all."""
    from raven.rpc.errors import ConfigValidationError

    with pytest.raises(ConfigValidationError):
        await rpc_plughub.plug_toggle({"name": "  ", "enabled": False})


def test_a_ledger_read_answers_for_an_unnameable_id() -> None:
    """An id that cannot name a file provably has no ledger, so the read is a
    None rather than a raise -- which is what lets the handlers above ask the
    question at all."""
    from raven.market.ledger import read_ledger

    assert read_ledger("_dev") is None
    assert read_ledger("my server") is None


# the declared shape, against what these handlers really return
# ---------------------------------------------------------------------------
# `test_rpc_contract_shapes` skips this group, on the grounds that it is
# covered here. It was not: the tests above drive the real handlers but assert
# on individual keys, and never validate a payload against `METHOD_MODELS`. So
# ten declarations -- the whole market group -- had nothing checking them
# against the code. The models are `extra="forbid"`, which is the direction a
# hand-written contract drifts: a key gets added to a response and nobody opens
# models.py.


def _shape(method: str, payload: dict):
    from raven.rpc.models import METHOD_MODELS

    _, model = METHOD_MODELS[method]
    return model.model_validate(payload)


async def test_search_and_detail_match_their_declared_models(_isolated) -> None:
    """Driven over the bundled catalogue, so this needs no network."""
    search = await rpc_plughub.plughub_search({})
    _shape("plughub.search", search)
    assert search["items"], "the bundled catalogue should not be empty"

    detail = await rpc_plughub.plughub_detail({"id": search["items"][0]["id"]})
    _shape("plughub.detail", detail)


async def test_install_and_remove_match_their_declared_models(monkeypatch, _isolated) -> None:
    entry = _entry("none")
    _patch_catalog(monkeypatch, entry)
    loop = _FakeLoop("svc", "connected")

    _shape("plug.install", await rpc_plughub.plug_install({"id": "svc"}, agent_loop_factory=_factory(loop)))
    _shape(
        "plug.toggle",
        # `name`, not `id` -- toggle addresses the configured server, not the
        # catalogue entry it came from.
        await rpc_plughub.plug_toggle({"name": "svc", "enabled": False}, agent_loop_factory=_factory(loop)),
    )
    _shape("plug.remove", await rpc_plughub.plug_remove({"name": "svc"}, agent_loop_factory=_factory(loop)))
