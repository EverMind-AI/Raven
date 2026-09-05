"""The MCP meta-tools exist only while some server actually serves them.

Five schemas a deploy without MCP should never pay for. Most MCP servers offer
only tools, so the gate is per primitive rather than "is MCP configured at all":
advertising ``read_mcp_resource`` where nothing serves resources spends context
on calls that can only fail.

Gating moves the tool array when a server connects or disconnects, which costs
the prompt-cache prefix -- but that array was already moving at exactly those
moments, because the server's own tools appear and disappear with it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.agent.loop.main import AgentLoop
from raven.config.schema import MCPServerConfig
from raven.contracts.tool import Tool
from raven.mcp.client import Connected
from raven.mcp.naming import MCPToolRef
from raven.mcp.prompts import PROMPT_TOOL_NAMES
from raven.mcp.resources import READ_RESOURCE_NAME, RESOURCE_TOOL_NAMES
from raven.providers.base import LLMProvider

_PATCH = "raven.mcp.manager.connect_mcp_server"


class _Caps:
    """None means "not offered", which is what the gate reads."""

    def __init__(self, *, resources=None, prompts=None) -> None:
        self.resources = resources
        self.prompts = prompts
        self.tools = object()


class _FakeTool(Tool):
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

    async def execute(self, **kwargs):
        return "ran"


class _Provider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(self, messages, **kwargs):  # pragma: no cover - no turn is run here
        raise AssertionError("no completion in these tests")

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _loop(workspace: Path, servers: dict | None = None, disabled: list[str] | None = None) -> AgentLoop:
    return AgentLoop(
        provider=_Provider(),
        workspace=workspace,
        model="stub",
        mcp_servers=servers or {},
        policy=TurnPolicy(max_iterations=1),
        tools=ToolWiring(disabled_tools=disabled or []),
    )


def _connect(caps: _Caps):
    async def fake(name, cfg, registry, stack, executor=None, http_auth=None):
        full = f"mcp_{name}_search"
        registry.register(_FakeTool(full), origin=MCPToolRef(name=full, server=name, tool="search"))
        return Connected(names=[full], session=object(), capabilities=caps)

    return fake


def _cfg(url: str = "https://svc.test/mcp") -> MCPServerConfig:
    return MCPServerConfig(url=url)


class TestNoServerMeansNoMetaTools:
    def test_a_loop_with_no_mcp_carries_none_of_them(self, workspace):
        loop = _loop(workspace)
        for name in RESOURCE_TOOL_NAMES | PROMPT_TOOL_NAMES:
            assert not loop.tools.has(name), name

    async def test_a_tools_only_server_adds_none_of_them(self, workspace):
        # The common case: most servers offer tools and nothing else.
        loop = _loop(workspace)
        with patch(_PATCH, new=_connect(_Caps())):
            await loop.apply_mcp_config({"svc": _cfg()})
        assert loop.tools.has("mcp_svc_search")
        for name in RESOURCE_TOOL_NAMES | PROMPT_TOOL_NAMES:
            assert not loop.tools.has(name), name


class TestTheTwoPrimitivesGateApart:
    async def test_a_resources_server_brings_only_the_resource_tools(self, workspace):
        loop = _loop(workspace)
        with patch(_PATCH, new=_connect(_Caps(resources=object()))):
            await loop.apply_mcp_config({"svc": _cfg()})
        assert all(loop.tools.has(n) for n in RESOURCE_TOOL_NAMES)
        assert not any(loop.tools.has(n) for n in PROMPT_TOOL_NAMES)

    async def test_a_prompts_server_brings_only_the_prompt_tools(self, workspace):
        loop = _loop(workspace)
        with patch(_PATCH, new=_connect(_Caps(prompts=object()))):
            await loop.apply_mcp_config({"svc": _cfg()})
        assert all(loop.tools.has(n) for n in PROMPT_TOOL_NAMES)
        assert not any(loop.tools.has(n) for n in RESOURCE_TOOL_NAMES)

    async def test_a_server_offering_both_brings_all_five(self, workspace):
        loop = _loop(workspace)
        with patch(_PATCH, new=_connect(_Caps(resources=object(), prompts=object()))):
            await loop.apply_mcp_config({"svc": _cfg()})
        assert all(loop.tools.has(n) for n in RESOURCE_TOOL_NAMES | PROMPT_TOOL_NAMES)


class TestTheyLeaveWithTheLastServer:
    async def test_removing_the_only_resources_server_withdraws_them(self, workspace):
        """A detach fires no connect, so the apply has to re-gate on its own."""
        loop = _loop(workspace)
        with patch(_PATCH, new=_connect(_Caps(resources=object()))):
            await loop.apply_mcp_config({"svc": _cfg()})
            assert all(loop.tools.has(n) for n in RESOURCE_TOOL_NAMES)
            await loop.apply_mcp_config({})
        assert not any(loop.tools.has(n) for n in RESOURCE_TOOL_NAMES)

    async def test_disabling_the_only_resources_server_withdraws_them(self, workspace):
        loop = _loop(workspace)
        with patch(_PATCH, new=_connect(_Caps(resources=object()))):
            await loop.apply_mcp_config({"svc": _cfg()})
            await loop.apply_mcp_config({"svc": MCPServerConfig(url="https://svc.test/mcp", enabled=False)})
        assert not any(loop.tools.has(n) for n in RESOURCE_TOOL_NAMES)

    async def test_one_of_two_leaving_keeps_them(self, workspace):
        loop = _loop(workspace)
        with patch(_PATCH, new=_connect(_Caps(resources=object()))):
            await loop.apply_mcp_config({"a": _cfg(), "b": _cfg("https://b.test/mcp")})
            await loop.apply_mcp_config({"a": _cfg()})
        assert all(loop.tools.has(n) for n in RESOURCE_TOOL_NAMES)


class TestGatingIsIdempotent:
    async def test_a_second_apply_that_changes_nothing_registers_nothing_new(self, workspace):
        loop = _loop(workspace)
        cfg = _cfg()
        with patch(_PATCH, new=_connect(_Caps(resources=object()))):
            await loop.apply_mcp_config({"svc": cfg})
            before = sorted(loop.tools.tool_names)
            await loop.apply_mcp_config({"svc": cfg.model_copy()})
        assert sorted(loop.tools.tool_names) == before

    async def test_a_blacklisted_meta_tool_does_not_break_idempotence(self, workspace):
        """``disabled_tools`` cannot reach these five, so it cannot churn them.

        The gate's predicate is ``all(...)`` over the set: one name missing reads
        as "the set is not installed" and the next connect puts the whole set
        back. So an entry here could never switch one off -- it could only turn
        every connect into an unregister-and-re-register of all of them, which
        moves the tool array and costs each live conversation its cached prompt
        prefix. ``_withheld_tool_names`` drops them by name instead.
        """
        loop = _loop(workspace, disabled=[READ_RESOURCE_NAME])
        taken: list[str] = []
        original = loop.tools.unregister

        def spy(name: str) -> None:
            taken.append(name)
            original(name)

        loop.tools.unregister = spy  # type: ignore[method-assign]
        with patch(_PATCH, new=_connect(_Caps(resources=object()))):
            await loop.apply_mcp_config({"a": _cfg()})
            await loop.apply_mcp_config({"a": _cfg(), "b": _cfg("https://b.test/mcp")})

        assert all(loop.tools.has(n) for n in RESOURCE_TOOL_NAMES)
        assert [n for n in taken if n in RESOURCE_TOOL_NAMES] == [], taken

    async def test_the_exemption_is_by_name_not_a_blanket_skip(self, workspace):
        """A server's own tool named in ``disabled_tools`` is withheld; the meta
        tools named beside it are not.

        Asserted on what is *offered* rather than on what is registered, which is
        the whole of the change here: a switched-off tool stays in the registry and
        is left out of the assembled array. Expressing the preference by
        unregistering made it irreversible -- nothing remembered what to put back
        -- so the array is now filtered per assembly instead.
        """
        loop = _loop(workspace, disabled=["mcp_svc_search"])
        with patch(_PATCH, new=_connect(_Caps(resources=object()))):
            await loop.apply_mcp_config({"svc": _cfg()})

        offered = {d["function"]["name"] for d in loop.tools.get_definitions()}
        assert "mcp_svc_search" not in offered
        assert loop.tools.has("mcp_svc_search"), "withheld, not destroyed -- it has to be reversible"
        assert RESOURCE_TOOL_NAMES <= offered

    async def test_the_meta_tools_see_the_manager_that_registered_them(self, workspace):
        # Registered bound to the live manager, so the schema they advertise names
        # the servers actually offering -- not an empty list.
        loop = _loop(workspace)
        with patch(_PATCH, new=_connect(_Caps(resources=object()))):
            await loop.apply_mcp_config({"openseo": _cfg()})
        tool = loop.tools.get("list_mcp_resources")
        assert "openseo" in tool.parameters["properties"]["server"]["description"]


class TestConnectPathAndClose:
    """[F1] The plug.auth path (manager.connect) has no second sync, so the
    post_connect ordering is the only chance the meta-tools get; and close_mcp
    must withdraw the loop-owned five before nulling the manager."""

    async def test_a_sole_resources_server_arriving_via_connect_brings_them(self, workspace):
        loop = _loop(workspace)
        with patch(_PATCH, new=_connect(_Caps())):
            await loop.apply_mcp_config({"plain": _cfg("https://plain.test/mcp")})
        assert loop.tools.get("read_mcp_resource") is None
        with patch(_PATCH, new=_connect(_Caps(resources=object()))):
            await loop._mcp_manager.connect("svc", _cfg())
        assert loop.tools.get("read_mcp_resource") is not None, (
            "post_connect fired before the state flip, so the sync never saw this connect"
        )

    async def test_a_forced_reconnect_of_the_sole_resources_server_keeps_them(self, workspace):
        loop = _loop(workspace)
        with patch(_PATCH, new=_connect(_Caps(resources=object()))):
            await loop.apply_mcp_config({"svc": _cfg()})
            assert loop.tools.get("read_mcp_resource") is not None
            await loop._mcp_manager.connect("svc", _cfg("https://svc.test/mcp2"))
        assert loop.tools.get("read_mcp_resource") is not None, (
            "the blind pre-flip sync withdrew the five and nothing re-registered them"
        )

    async def test_close_mcp_withdraws_them_and_a_reconnect_restores_them(self, workspace):
        loop = _loop(workspace)
        with patch(_PATCH, new=_connect(_Caps(resources=object()))):
            await loop.apply_mcp_config({"svc": _cfg()})
            assert loop.tools.get("read_mcp_resource") is not None
            await loop.close_mcp()
            assert loop.tools.get("read_mcp_resource") is None, (
                "the five are the loop's, not any server's; they must not outlive the manager"
            )
            await loop.apply_mcp_config({"svc": _cfg()})
        assert loop.tools.get("read_mcp_resource") is not None
