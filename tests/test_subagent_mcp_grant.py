"""Per-dispatch MCP grant policy and backend projections."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from raven.agent.subagent.mcp_grant import (
    GrantedServer,
    GrantedTool,
    LiveMcpSource,
    McpDispatchError,
    McpGrant,
    McpServerView,
    acp_target,
    annotate_mcp_failure,
    raven_cli_target,
    raven_loop_target,
    resolve_grant,
)
from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import MCPServerConfig
from raven.mcp.client import MCPToolWrapper


class _Source:
    def __init__(
        self,
        servers: dict[str, McpServerView],
        *,
        tools: dict[str, tuple[GrantedTool, ...]] | None = None,
        disabled_tools: frozenset[str] = frozenset(),
    ) -> None:
        self._servers = servers
        self._tools = tools or {}
        self._disabled_tools = disabled_tools

    def server(self, name: str) -> McpServerView | None:
        return self._servers.get(name)

    def tools(self, name: str) -> tuple[GrantedTool, ...]:
        return self._tools.get(name, ())

    def disabled_tools(self) -> frozenset[str]:
        return self._disabled_tools


def _view(name: str, config: MCPServerConfig, *, state: str | None = None) -> McpServerView:
    return McpServerView(name=name, config=config, state=state)


def _wrapper(server: str, tool: str) -> MCPToolWrapper:
    definition = SimpleNamespace(name=tool, description="", inputSchema={})
    return MCPToolWrapper(SimpleNamespace(), server, definition)


def test_raven_loop_projects_connected_host_wrappers_without_exporting_config() -> None:
    config = MCPServerConfig(
        type="streamableHttp",
        url="https://mcp.example.test",
        headers={"Authorization": "Bearer host-secret"},
        auth="oauth",
    )
    wrapper = _wrapper("docs", "search")
    source = _Source(
        {"docs": _view("docs", config, state="connected")},
        tools={"docs": (GrantedTool(wrapper=wrapper, ref=wrapper.ref),)},
        disabled_tools=frozenset({wrapper.name}),
    )

    grant = resolve_grant(["docs"], source, raven_loop_target())

    assert grant.for_registry() == ((wrapper, wrapper.ref),)
    assert grant.missing == ()
    assert grant.withheld == ()
    assert grant.disabled_tools == ()


def test_live_source_distinguishes_unconfigured_from_not_connected_without_a_manager() -> None:
    configured = {"offline": MCPServerConfig(command="mcp-offline")}
    source = LiveMcpSource(lambda: configured, lambda: None, ToolRegistry(), frozenset)

    grant = resolve_grant(["absent", "offline"], source, raven_loop_target())

    assert [(item.name, item.reason) for item in grant.missing] == [
        ("absent", "not_configured"),
        ("offline", "not_connected"),
    ]


@pytest.mark.asyncio
async def test_two_in_process_children_can_share_one_live_wrapper_concurrently() -> None:
    class _Session:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def call_tool(self, _name: str, *, arguments: dict[str, int]):
            await asyncio.sleep(0)
            self.calls.append(arguments["value"])
            return SimpleNamespace(content=[])

    session = _Session()
    definition = SimpleNamespace(name="search", description="", inputSchema={})
    wrapper = MCPToolWrapper(session, "docs", definition)
    source = _Source(
        {"docs": _view("docs", MCPServerConfig(command="unused"), state="connected")},
        tools={"docs": (GrantedTool(wrapper=wrapper, ref=wrapper.ref),)},
    )
    grant = resolve_grant(["docs"], source, raven_loop_target())
    registries = [ToolRegistry(), ToolRegistry()]
    for registry in registries:
        for shared, origin in grant.for_registry():
            registry.register(shared, origin=origin)

    results = await asyncio.gather(
        registries[0].execute(wrapper.name, {"value": 1}),
        registries[1].execute(wrapper.name, {"value": 2}),
    )

    assert session.calls == [1, 2]
    assert results == ["(no output)", "(no output)"]
    assert registries[0].get(wrapper.name) is registries[1].get(wrapper.name) is wrapper


def test_cli_projects_server_config_and_unions_disabled_tools() -> None:
    config = MCPServerConfig(
        command="mcp-files",
        args=["--root", "/workspace"],
        env={"MCP_TOKEN": "exported-secret"},
        auth="apikey",
    )
    source = _Source(
        {"files": _view("files", config)},
        disabled_tools=frozenset({"mcp_files_delete", "mcp_files_write"}),
    )

    grant = resolve_grant(["files"], source, raven_cli_target(allow_secrets=True))
    child = grant.for_child_config(["mcp_files_write", "exec"])

    assert child["mcpServers"]["files"]["command"] == "mcp-files"
    assert child["mcpServers"]["files"]["env"] == {"MCP_TOKEN": "exported-secret"}
    assert child["disabledTools"] == ["exec", "mcp_files_delete", "mcp_files_write"]


def test_acp_projection_hands_over_a_bridge_command_not_a_server_definition() -> None:
    cfg = MCPServerConfig(command="/usr/local/bin/pg-mcp", args=["--db", "x"], env={"PGPASSWORD": "hunter2"})
    grant = McpGrant(granted=(GrantedServer("pg", "stdio", cfg, socket_path="/tmp/raven-mcp/abc123456789.sock"),))
    [entry] = grant.for_acp(["/usr/local/bin/raven", "mcp", "bridge"])

    assert entry == {
        "name": "pg",
        "command": "/usr/local/bin/raven",
        "args": ["mcp", "bridge", "/tmp/raven-mcp/abc123456789.sock"],
        "env": [],
    }
    # The whole point: neither the real command nor its secret crosses over.
    assert "hunter2" not in json.dumps(entry)
    assert "pg-mcp" not in json.dumps(entry)


def test_an_http_server_projects_to_the_same_stdio_shape() -> None:
    cfg = MCPServerConfig(url="https://mcp.example.com/mcp", headers={"Authorization": "Bearer t"})
    grant = McpGrant(granted=(GrantedServer("remote", "http", cfg, socket_path="/tmp/raven-mcp/deadbeef0000.sock"),))
    [entry] = grant.for_acp(["/usr/local/bin/raven", "mcp", "bridge"])

    assert entry["command"] == "/usr/local/bin/raven"
    assert "url" not in entry
    assert "Bearer t" not in json.dumps(entry)


def test_projecting_before_the_host_opened_an_endpoint_is_a_programming_error() -> None:
    grant = McpGrant(granted=(GrantedServer("pg", "stdio", MCPServerConfig(command="/bin/true")),))

    with pytest.raises(ValueError, match="no endpoint"):
        grant.for_acp(["/usr/local/bin/raven", "mcp", "bridge"])


def test_an_acp_grant_no_longer_depends_on_the_adapters_http_support() -> None:
    # Delivery is stdio whatever the upstream is, so the capability gate that
    # used to withhold an http server has nothing left to decide.
    cfg = MCPServerConfig(type="streamableHttp", url="https://mcp.example.com/mcp")
    source = _Source({"remote": _view("remote", cfg)})
    grant = resolve_grant(["remote"], source, acp_target(allow_secrets=False, stdio_path=None))
    assert [s.name for s in grant.granted] == ["remote"]
    assert grant.withheld == ()


def test_with_endpoints_fills_in_the_socket_each_server_got() -> None:
    cfg = MCPServerConfig(command="/bin/true")
    grant = McpGrant(granted=(GrantedServer("a", "stdio", cfg),))
    filled = grant.with_endpoints({"a": "/tmp/raven-mcp/aaaaaaaaaaaa.sock"})
    assert filled.granted[0].socket_path == "/tmp/raven-mcp/aaaaaaaaaaaa.sock"
    assert grant.granted[0].socket_path is None  # the original is untouched


@pytest.mark.parametrize("allow_secrets", [False, True])
def test_oauth_is_withheld_even_when_secret_export_is_allowed(allow_secrets: bool) -> None:
    config = MCPServerConfig(
        type="streamableHttp",
        url="https://oauth.example.test/mcp",
        headers={"Authorization": "Bearer oauth-secret"},
        auth="oauth",
    )
    source = _Source({"oauth": _view("oauth", config)})

    grant = resolve_grant(["oauth"], source, raven_cli_target(allow_secrets=allow_secrets))

    assert [item.reason for item in grant.withheld] == ["oauth_interaction"]
    assert grant.granted == ()
    assert "oauth-secret" not in grant.note_text()


def test_api_key_export_requires_explicit_permission_and_notes_never_contain_it() -> None:
    config = MCPServerConfig(
        type="streamableHttp",
        url="https://api.example.test/mcp",
        headers={"Authorization": "Bearer api-key-secret"},
        auth="apikey",
    )
    source = _Source({"private": _view("private", config)})

    denied = resolve_grant(["private"], source, raven_cli_target(allow_secrets=False))
    allowed = resolve_grant(["private"], source, raven_cli_target(allow_secrets=True))

    assert [item.reason for item in denied.withheld] == ["secret_export_denied"]
    assert "api-key-secret" not in denied.note_text()
    assert [server.name for server in allowed.granted] == ["private"]


@pytest.mark.parametrize(
    "config",
    [
        MCPServerConfig(command="/bin/sh", env={"TOKEN": "secret"}),
        MCPServerConfig(
            type="streamableHttp",
            url="https://api.example.test/mcp",
            headers={"Authorization": "Bearer secret"},
        ),
    ],
)
def test_cross_process_secret_material_requires_permission_even_when_auth_is_default(
    config: MCPServerConfig,
) -> None:
    # The vendored CLI child only. An ACP receiver is handed a bridge endpoint
    # with an empty env, so no material crosses and there is nothing for the flag
    # to permit -- see test_a_bridged_receiver_gets_a_server_whose_env_holds_a_secret.
    source = _Source({"private": _view("private", config)})

    denied = resolve_grant(["private"], source, raven_cli_target(allow_secrets=False))
    allowed = resolve_grant(["private"], source, raven_cli_target(allow_secrets=True))

    assert [item.reason for item in denied.withheld] == ["secret_export_denied"]
    assert denied.granted == ()
    assert [server.name for server in allowed.granted] == ["private"]


def test_acp_reports_command_not_found_when_no_raven_can_be_handed_over(tmp_path, monkeypatch) -> None:
    # The command that has to be findable is raven, not the server's: the
    # adapter spawns the bridge, and the server itself is spawned on the host
    # side of the socket. Nowhere beside this interpreter and nothing on the
    # adapter's PATH is the only case with nothing to hand over.
    monkeypatch.setattr(sys, "executable", str(tmp_path / "no-bin" / "python"))
    source = _Source(
        {
            "local": _view("local", MCPServerConfig(command="/bin/sh")),
            "remote": _view("remote", MCPServerConfig(type="sse", url="https://mcp.example.test/sse")),
        }
    )

    grant = resolve_grant(["local", "remote"], source, acp_target(allow_secrets=False, stdio_path=str(tmp_path)))

    assert grant.granted == ()
    assert [(item.name, item.reason) for item in grant.missing] == [
        ("local", "command_not_found"),
        ("remote", "command_not_found"),
    ]


def test_acp_grants_name_this_builds_raven_as_the_command(tmp_path, monkeypatch) -> None:
    """The build holding the socket, not whichever raven the PATH names first.

    An older raven on the adapter's PATH has no ``mcp bridge`` subcommand, so it
    exits on the relay and every bridged server for the dispatch shows up as a
    closed connection while the sub-agent answers the turn regardless.
    """
    own = tmp_path / "own"
    own.mkdir()
    (own / "raven").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (own / "raven").chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(own / "python"))

    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / "raven").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stale / "raven").chmod(0o755)

    source = _Source({"local": _view("local", MCPServerConfig(command="mcp-local", args=["serve"]))})

    grant = resolve_grant(["local"], source, acp_target(allow_secrets=False, stdio_path=str(stale)))

    assert [(server.name, server.command) for server in grant.granted] == [("local", str(own / "raven"))]
    assert grant.missing == ()


def test_acp_falls_back_to_the_adapter_path_when_this_build_has_no_script(tmp_path, monkeypatch) -> None:
    """A raven installed where the adapter runs is still worth handing over."""
    monkeypatch.setattr(sys, "executable", str(tmp_path / "no-bin" / "python"))
    on_path = tmp_path / "onpath"
    on_path.mkdir()
    (on_path / "raven").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (on_path / "raven").chmod(0o755)
    source = _Source({"local": _view("local", MCPServerConfig(command="mcp-local", args=["serve"]))})

    grant = resolve_grant(["local"], source, acp_target(allow_secrets=False, stdio_path=str(on_path)))

    assert [(server.name, server.command) for server in grant.granted] == [("local", str(on_path / "raven"))]


def test_degradation_notes_name_only_servers_and_policy_reasons() -> None:
    secret = "never-print-this-secret"
    source = _Source(
        {
            "disabled": _view(
                "disabled",
                MCPServerConfig(command="disabled-command", env={"TOKEN": secret}, enabled=False),
            ),
            "private": _view(
                "private",
                MCPServerConfig(
                    type="streamableHttp",
                    url="https://mcp.example.test",
                    headers={"Authorization": secret},
                    auth="apikey",
                ),
            ),
        }
    )

    grant = resolve_grant(["disabled", "private"], source, raven_cli_target(allow_secrets=False))
    note = grant.note_text()

    assert "disabled" in note
    assert "allowMcpSecrets=true" in note
    assert secret not in note
    assert "Authorization" not in note
    assert "TOKEN" not in note


def test_raven_loop_classifies_each_unavailable_server_without_partial_grants() -> None:
    wrapper = _wrapper("ready", "unused")
    source = _Source(
        {
            "disabled": _view("disabled", MCPServerConfig(command="x", enabled=False)),
            "offline": _view("offline", MCPServerConfig(command="x"), state="disconnected"),
            "auth": _view("auth", MCPServerConfig(command="x"), state="auth_required"),
            "empty": _view("empty", MCPServerConfig(command="x"), state="connected"),
            "ready": _view("ready", MCPServerConfig(command="x"), state="connected"),
        },
        tools={"ready": (GrantedTool(wrapper=wrapper, ref=wrapper.ref),)},
    )

    grant = resolve_grant(
        ["absent", "disabled", "offline", "auth", "empty", "ready", "ready"],
        source,
        raven_loop_target(),
    )

    assert [(item.name, item.reason) for item in grant.missing] == [
        ("absent", "not_configured"),
        ("disabled", "disabled"),
        ("offline", "not_connected"),
        ("auth", "auth_required"),
        ("empty", "no_tools"),
    ]
    assert [server.name for server in grant.granted] == ["ready"]


def test_failed_dispatch_keeps_secret_free_degradation_note() -> None:
    secret = "never-print-this-secret"
    source = _Source(
        {
            "private": _view(
                "private",
                MCPServerConfig(command="mcp-private", env={"TOKEN": secret}, auth="apikey"),
            )
        }
    )
    grant = resolve_grant(["private"], source, raven_cli_target(allow_secrets=False))

    with pytest.raises(RuntimeError) as caught, annotate_mcp_failure(grant):
        raise RuntimeError("dispatch failed")

    message = str(caught.value)
    assert "dispatch failed" in message
    assert "allowMcpSecrets=true" in message
    assert secret not in message


def test_failed_dispatch_wraps_structured_errors_without_mutating_them() -> None:
    source = _Source({})
    grant = resolve_grant(["missing"], source, raven_cli_target(allow_secrets=False))
    original = OSError(2, "No such file", "/tmp/mcp")
    original_args = original.args

    with pytest.raises(McpDispatchError) as caught, annotate_mcp_failure(grant):
        raise original

    assert caught.value.__cause__ is original
    assert original.args == original_args
    assert "MCP server 'missing' was not delivered because it is not configured" in str(caught.value)


def test_an_empty_cli_grant_still_copies_the_live_disabled_tool_snapshot() -> None:
    source = _Source({}, disabled_tools=frozenset({"mcp_docs_write", "exec"}))

    grant = resolve_grant([], source, raven_cli_target(allow_secrets=False))

    assert grant.for_child_config() == {
        "mcpServers": {},
        "disabledTools": ["exec", "mcp_docs_write"],
    }


def test_a_bridged_receiver_gets_an_oauth_server_the_host_has_connected() -> None:
    # The credential never crosses on this path -- for_acp emits an empty env --
    # so the auth label alone decides nothing. What decides is whether the host
    # can actually reach the upstream.
    config = MCPServerConfig(type="streamableHttp", url="https://mcp.example.test", auth="oauth")
    source = _Source({"sentry": _view("sentry", config, state="connected")})
    grant = resolve_grant(["sentry"], source, acp_target(allow_secrets=False, stdio_path=None))

    assert [server.name for server in grant.granted] == ["sentry"]
    assert grant.withheld == ()
    assert grant.missing == ()


def test_a_bridged_receiver_is_told_an_oauth_server_is_still_waiting() -> None:
    config = MCPServerConfig(type="streamableHttp", url="https://mcp.example.test", auth="oauth")
    source = _Source({"sentry": _view("sentry", config, state="auth_required")})
    grant = resolve_grant(["sentry"], source, acp_target(allow_secrets=False, stdio_path=None))

    assert grant.granted == ()
    assert [(item.name, item.reason) for item in grant.missing] == [("sentry", "auth_required")]
    assert "waiting for host authorization" in grant.note_text()


def test_a_bridged_receiver_gets_a_server_whose_env_holds_a_secret() -> None:
    # allow_secrets is about a definition crossing a process boundary, and on a
    # bridged path none does: the host spawns the server, the sub-agent is handed
    # a socket. Requiring the flag here would withhold most stdio servers for a
    # reason that no longer applies.
    config = MCPServerConfig(command="/usr/local/bin/pg-mcp", env={"PGPASSWORD": "hunter2"})
    source = _Source({"pg": _view("pg", config, state="connected")})
    grant = resolve_grant(["pg"], source, acp_target(allow_secrets=False, stdio_path=None))

    assert [server.name for server in grant.granted] == ["pg"]
    assert grant.withheld == ()


def test_the_cli_child_still_pays_for_both_gates() -> None:
    # for_child_config dumps env and headers verbatim into a file the child
    # reads, so on that path the gates are still what they always were.
    oauth = MCPServerConfig(type="streamableHttp", url="https://mcp.example.test", auth="oauth")
    secret = MCPServerConfig(command="/usr/local/bin/pg-mcp", env={"PGPASSWORD": "hunter2"})
    source = _Source(
        {
            "sentry": _view("sentry", oauth, state="connected"),
            "pg": _view("pg", secret, state="connected"),
        }
    )
    grant = resolve_grant(["sentry", "pg"], source, raven_cli_target(allow_secrets=False))

    assert grant.granted == ()
    assert [(item.name, item.reason) for item in grant.withheld] == [
        ("sentry", "oauth_interaction"),
        ("pg", "secret_export_denied"),
    ]


def test_a_bridged_grant_carries_the_hosts_disabled_tools_for_the_endpoint_to_enforce() -> None:
    # A bridged child speaks MCP straight to the real server, so nothing between
    # them withholds a tool by itself. The set travels with the grant and the
    # relay applies it; leaving it out let a child list and call a tool the host
    # had switched off.
    config = MCPServerConfig(command="mcp-files")
    source = _Source(
        {"files": _view("files", config, state="connected")},
        disabled_tools=frozenset({"mcp_files_delete", "exec"}),
    )

    grant = resolve_grant(["files"], source, acp_target(allow_secrets=False, stdio_path=None))

    assert [server.name for server in grant.granted] == ["files"]
    assert grant.disabled_tools == ("exec", "mcp_files_delete")


def test_an_in_process_grant_carries_no_off_switch_because_the_registry_holds_it() -> None:
    config = MCPServerConfig(command="mcp-files")
    wrapper = _wrapper("files", "search")
    source = _Source(
        {"files": _view("files", config, state="connected")},
        tools={"files": (GrantedTool(wrapper=wrapper, ref=wrapper.ref),)},
        disabled_tools=frozenset({"mcp_files_delete"}),
    )

    grant = resolve_grant(["files"], source, raven_loop_target())

    assert grant.disabled_tools == ()
