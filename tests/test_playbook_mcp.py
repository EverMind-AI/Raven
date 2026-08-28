"""The MCP servers one playbook run may reach, and the pre-flight that opens them.

Three properties are load-bearing and each has its own group below: a playbook's
own server definitions travel with the file while their secrets do not, the
pre-flight resolves playbook-first without a branch in the grant resolver, and
the pre-flight never waits on a person.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from raven.agent.subagent.mcp_grant import raven_cli_target, resolve_grant
from raven.config.schema import MCPServerConfig
from raven.playbook.mcp import (
    declared_mcp_names,
    playbook_mcp_servers,
    preflight_mcp_source,
    unusable_servers,
)
from raven.playbook.types import NodeSpec, ParamSpec, PlaybookSpec, Triggers

_CONNECT = "raven.mcp.manager.connect_mcp_server"


def _spec(**over: Any) -> PlaybookSpec:
    base: dict[str, Any] = dict(
        name="audit",
        description="audit the analytics database",
        task_summary="run the analytics audit and report back",
        mode="dag",
        triggers=Triggers(keywords=["audit"]),
        params={"PG_PASSWORD": ParamSpec(type="secret", description="Postgres password for the analytics server")},
        mcp_servers={
            "local-pg": MCPServerConfig(
                command="pg-mcp",
                args=["--db", "analytics"],
                env={"PGPASSWORD": "{{ params.PG_PASSWORD }}"},
            )
        },
        nodes=[
            NodeSpec(
                id="audit",
                subagent="raven",
                node_summary="audit the tables",
                prompt_template="audit every table",
                mcps=["local-pg", "deepwiki"],
            )
        ],
    )
    base.update(over)
    return PlaybookSpec(**base)


# ------------------------------------------------- the definition travels, the value does not


def test_a_reference_is_filled_in_a_copy_and_the_spec_keeps_the_reference():
    spec = _spec()
    filled = playbook_mcp_servers(spec, {"PG_PASSWORD": "hunter2"})

    assert filled["local-pg"].env == {"PGPASSWORD": "hunter2"}
    # The spec is the file. A value written back would be a credential in git.
    assert spec.mcp_servers["local-pg"].env == {"PGPASSWORD": "{{ params.PG_PASSWORD }}"}
    assert "hunter2" not in str(spec.block_dump())


def test_both_reference_spellings_are_one_mechanism():
    spec = _spec(
        mcp_servers={
            "local-pg": MCPServerConfig(
                command="pg-mcp", env={"A": "{{ params.PG_PASSWORD }}", "B": "${params.PG_PASSWORD}"}
            )
        }
    )
    assert playbook_mcp_servers(spec, {"PG_PASSWORD": "hunter2"})["local-pg"].env == {"A": "hunter2", "B": "hunter2"}


def test_an_unsupplied_reference_drops_the_entry_instead_of_emptying_it():
    """ "no password" and "the password is empty" are different states, and a
    server told the second fails in a way that reads like a broken server."""
    filled = playbook_mcp_servers(_spec(), {"PG_PASSWORD": ""})
    assert filled["local-pg"].env == {}

    # A deliberately empty literal is not a dropped reference.
    spec = _spec(mcp_servers={"local-pg": MCPServerConfig(command="pg-mcp", env={"QUIET": ""})})
    assert playbook_mcp_servers(spec, {})["local-pg"].env == {"QUIET": ""}


def test_a_playbook_definition_carrying_a_secret_still_needs_allow_mcp_secrets():
    """The definition's new provenance does not widen the export boundary: a
    config handed to a vendored CLI child is a file on disk, so ``env`` reaching
    it is gated exactly as a host-configured server's is."""
    spec = _spec()
    servers = playbook_mcp_servers(spec, {"PG_PASSWORD": "hunter2"})
    source = _StaticSource(servers)

    denied = resolve_grant(["local-pg"], source, raven_cli_target(allow_secrets=False))
    assert [(w.name, w.reason) for w in denied.withheld] == [("local-pg", "secret_export_denied")]
    assert "hunter2" not in denied.note_text()

    allowed = resolve_grant(["local-pg"], source, raven_cli_target(allow_secrets=True))
    assert allowed.for_child_config()["mcpServers"]["local-pg"]["env"] == {"PGPASSWORD": "hunter2"}


# ------------------------------------------------- what a run declares


def test_declared_names_cover_the_playbook_its_nodes_and_the_callers_fills():
    spec = _spec()
    assert declared_mcp_names(spec) == {"local-pg", "deepwiki"}
    # ``mcps`` is fillable, so a caller can add servers the file never named.
    assert declared_mcp_names(spec, {"audit": {"mcps": ["sentry"]}}) == {"local-pg", "deepwiki", "sentry"}


def test_prompt_mode_cannot_declare_ahead_of_composition():
    """The graph is composed after the load, so the pre-flight has to cover
    everything on offer rather than a set read off the file.

    ``mcp_servers={}`` because a prompt-mode playbook may not carry the section at
    all -- validation refuses that combination, so a spec holding both is one no
    entry point can produce.
    """
    spec = _spec(mode="prompt", nodes=None, mcp_servers={}, prompts="one node that audits the tables")
    assert declared_mcp_names(spec) is None


# ------------------------------------------------- the pre-flight


def _wrapper(server: str, tool: str):
    """A real ``MCPToolWrapper``: ``LiveMcpSource.tools`` filters on the type,
    so a stand-in tool is invisible to it and would prove nothing."""
    from types import SimpleNamespace

    from raven.mcp.client import MCPToolWrapper

    return MCPToolWrapper(SimpleNamespace(), server, SimpleNamespace(name=tool, description="", inputSchema={}))


class _StaticSource:
    """An McpSource over a fixed mapping, for the projection tests."""

    def __init__(self, servers: dict[str, MCPServerConfig]) -> None:
        self._servers = servers

    def server(self, name: str):
        from raven.agent.subagent.mcp_grant import McpServerView

        cfg = self._servers.get(name)
        return None if cfg is None else McpServerView(name=name, config=cfg, state=None)

    def tools(self, name: str) -> tuple:
        return ()

    def disabled_tools(self) -> frozenset[str]:
        return frozenset()


class _Caps:
    """Minimal stand-in for the SDK's ``ServerCapabilities`` -- tools only."""

    def __init__(self) -> None:
        self.resources = None
        self.prompts = None
        self.tools = object()


def _connected(names: list[str]):
    """The shape ``connect_mcp_server`` returns, for a patched stub to hand back."""
    from raven.mcp.client import Connected

    return Connected(names=list(names), session=object(), capabilities=_Caps())


async def _noop_connect(name, cfg, registry, stack, executor=None, http_auth=None):
    return _connected([])


def _preflight(**over: Any):
    kwargs: dict[str, Any] = dict(host_servers={}, workspace=Path("."))
    kwargs.update(over)
    return preflight_mcp_source(**kwargs)


@pytest.mark.asyncio
async def test_the_playbook_definition_wins_over_a_host_server_of_the_same_name():
    """Precedence lives at the config seam: one merged mapping is what the
    manager dials and what the grant resolver reads, so the two cannot drift."""
    host = {"local-pg": MCPServerConfig(command="host-pg")}
    playbook = {"local-pg": MCPServerConfig(command="playbook-pg")}
    with patch(_CONNECT, new=_noop_connect):
        async with _preflight(host_servers=host, playbook_servers=playbook, declared={"local-pg"}) as source:
            assert source.server("local-pg").config.command == "playbook-pg"
            grant = resolve_grant(["local-pg"], source, raven_cli_target(allow_secrets=True))
            assert grant.for_child_config()["mcpServers"]["local-pg"]["command"] == "playbook-pg"


@pytest.mark.asyncio
async def test_only_the_declared_servers_are_dialled():
    dialled: list[str] = []

    async def record(name, cfg, registry, stack, executor=None, http_auth=None):
        dialled.append(name)
        return _connected([])

    host = {"wanted": MCPServerConfig(command="a"), "unwanted": MCPServerConfig(command="b")}
    with patch(_CONNECT, new=record):
        async with _preflight(host_servers=host, declared={"wanted"}) as source:
            assert dialled == ["wanted"]
            # And what was not dialled is not resolvable either, so a grant
            # cannot promise a server the pre-flight never touched.
            assert source.server("unwanted") is None


@pytest.mark.asyncio
async def test_a_server_that_fails_to_connect_does_not_cost_the_run():
    async def boom(name, cfg, registry, stack, executor=None, http_auth=None):
        raise RuntimeError("upstream is down")

    with patch(_CONNECT, new=boom):
        async with _preflight(host_servers={"broken": MCPServerConfig(command="a")}, declared={"broken"}) as source:
            assert source.server("broken").state == "error"
            assert unusable_servers(source, ["broken"]) == [("broken", "error")]


@pytest.mark.asyncio
async def test_the_preflight_registers_what_came_up_so_an_in_process_node_can_be_granted():
    async def connects(name, cfg, registry, stack, executor=None, http_auth=None):
        wrapper = _wrapper(name, "query")
        registry.register(wrapper, origin=wrapper.ref)
        return _connected([wrapper.name])

    with patch(_CONNECT, new=connects):
        async with _preflight(host_servers={"local-pg": MCPServerConfig(command="a")}, declared={"local-pg"}) as source:
            assert source.server("local-pg").state == "connected"
            assert [t.ref.tool for t in source.tools("local-pg")] == ["query"]


@pytest.mark.asyncio
async def test_the_preflight_returns_while_a_server_is_still_waiting_on_a_browser(monkeypatch):
    """The hard requirement: authorization that parks on a person must not hold
    the run. The manager marks the server ``auth_required`` from its oauth hook
    and detaches the attempt; nothing in the pre-flight may await it.

    A pre-flight that waited would sit here for the 90s handshake bound, so the
    5s budget is the assertion.
    """
    from raven.mcp import oauth as mcp_oauth

    captured: dict[str, Any] = {}

    async def fake_provider_for(server, cfg, notify=None, interactive=False, can_park=True):
        captured["notify"] = notify
        captured["can_park"] = can_park
        return None

    monkeypatch.setattr(mcp_oauth, "provider_for", fake_provider_for)
    released = asyncio.Event()

    async def parks(name, cfg, registry, stack, executor=None, http_auth=None):
        captured["notify"]("oauth.pending", {"server": name, "url": "https://idp.example/a"})
        await released.wait()
        return _connected([])

    servers = {"sentry": MCPServerConfig(url="https://sentry.example/mcp", auth="oauth")}
    try:
        with patch(_CONNECT, new=parks):
            started = time.monotonic()

            async def enter_and_check():
                async with _preflight(host_servers=servers, declared={"sentry"}) as source:
                    return source.server("sentry").state, unusable_servers(source, ["sentry"])

            state, unusable = await asyncio.wait_for(enter_and_check(), timeout=5)
            elapsed = time.monotonic() - started

        assert state == "auth_required"
        assert unusable == [("sentry", "auth_required")]
        assert elapsed < 2, f"the pre-flight waited {elapsed:.1f}s on a browser authorization"
        # And it said so at the OAuth seam rather than relying on the watchdog:
        # a parked flow the pre-flight is allowed to wait on holds the caller for
        # the whole flow timeout, which is how a run stalled 15 minutes a server.
        assert captured["can_park"] is False
    finally:
        # Never leave the parked attempt running past the test.
        released.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_a_server_waiting_on_authorization_is_not_delivered(monkeypatch):
    """The other half of the same requirement: not blocking must not mean
    delivering anyway. An in-process node is refused with the reason named."""
    from raven.agent.subagent.mcp_grant import raven_loop_target
    from raven.mcp import oauth as mcp_oauth

    captured: dict[str, Any] = {}

    async def fake_provider_for(server, cfg, notify=None, interactive=False, can_park=True):
        captured["notify"] = notify
        return None

    monkeypatch.setattr(mcp_oauth, "provider_for", fake_provider_for)
    released = asyncio.Event()

    async def parks(name, cfg, registry, stack, executor=None, http_auth=None):
        captured["notify"]("oauth.pending", {"server": name, "url": "https://idp.example/a"})
        await released.wait()
        return _connected([])

    servers = {"sentry": MCPServerConfig(url="https://sentry.example/mcp", auth="oauth")}
    try:
        with patch(_CONNECT, new=parks):

            async def enter_and_resolve():
                async with _preflight(host_servers=servers, declared={"sentry"}) as source:
                    return resolve_grant(["sentry"], source, raven_loop_target())

            # Bounded for the same reason the case above is: a pre-flight that
            # waited on the browser would otherwise sit here for the 90s
            # handshake bound before this assertion got its turn.
            grant = await asyncio.wait_for(enter_and_resolve(), timeout=10)
        assert grant.granted == ()
        assert [(m.name, m.reason) for m in grant.missing] == [("sentry", "auth_required")]
        assert "waiting for host authorization" in grant.note_text()
    finally:
        released.set()
        await asyncio.sleep(0.05)
