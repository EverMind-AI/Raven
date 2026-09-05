"""The MCP servers one playbook run may reach, and the pre-flight that opens them.

Two holes are closed here, and they are the same hole seen from both ends.

A playbook's ``mcps: [local-pg]`` is a bare local short name resolved against the
receiving machine's ``tools.mcpServers``, so a distributed playbook delivered
nothing unless that machine happened to have a server of that name. The spec's
own ``mcpServers`` section (:attr:`~raven.playbook.types.PlaybookSpec.mcp_servers`)
now travels with the file, and :func:`playbook_mcp_servers` fills its parameter
references in. **Precedence is realized at the MCP source, not in the grant
resolver**: playbook definitions on top, so the single ``source.server(name)``
lookup already answers playbook-first. A resolver branch would be a second
place for the same fact to be decided.

This pre-flight merges the two mappings into one before connecting, which the
conversational path deliberately does not (see ``LiveMcpSource``): here the
merged mapping is also what the manager dials, so every definition the source
can answer with is one this run connected, and a shadowed name has no host
connection left to be confused with.

And ``raven playbook run`` wired no MCP source at all, so every declared server
resolved to ``not_connected`` however well the machine was configured.
:func:`preflight_mcp_source` is what it wires: one short-lived
``MCPConnectionManager`` over a throwaway registry, connected once before the
graph starts.

The pre-flight is also where authorization happens, and it **must not wait on a
person**: a server whose OAuth flow parks on the browser is already marked
``auth_required`` by the manager's own oauth hook and detached from the sync (see
``MCPConnectionManager._attempt_or_detach``), so the connect that cannot finish
costs the run nothing and the server is simply not delivered -- with
``McpGrant.note_text()`` saying why. Nothing here may re-introduce a wait on it.

The connections stay open for the run rather than closing after the handshake:
an in-process (``raven-loop``) node is granted the host's *live* tool wrappers,
so closing the manager first would deliver nothing to it. A bridged sub-agent
opens its own upstream per dispatch and does not read them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.config.schema import MCPServerConfig
from raven.playbook.params import fill_param_refs, has_param_ref

if TYPE_CHECKING:
    from raven.agent.subagent.mcp_grant import McpSource
    from raven.playbook.types import PlaybookSpec


def playbook_mcp_servers(spec: "PlaybookSpec", values: Mapping[str, str]) -> dict[str, MCPServerConfig]:
    """The spec's own server definitions with their parameter references filled in.

    Copies, never in place: the spec is the file, and the file keeps the
    reference. An ``env`` / ``headers`` entry whose reference resolved to nothing
    is dropped rather than passed on empty -- "the user did not supply a
    password" and "the password is the empty string" are different states, and a
    server told the second one fails in a way that reads like a broken server.
    """
    out: dict[str, MCPServerConfig] = {}
    for name, cfg in (spec.mcp_servers or {}).items():
        out[name] = cfg.model_copy(
            update={
                "env": _filled(cfg.env, values),
                "headers": _filled(cfg.headers, values),
            }
        )
    return out


def _filled(mapping: Mapping[str, str] | None, values: Mapping[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, raw in (mapping or {}).items():
        filled = fill_param_refs(raw, values)
        if not filled and has_param_ref(raw):
            continue
        out[key] = filled
    return out


def declared_mcp_names(
    spec: "PlaybookSpec",
    fills: Mapping[str, Mapping[str, Any]] | None = None,
) -> set[str] | None:
    """Every server this run could ask for, or ``None`` when that is unknowable.

    ``None`` is prompt mode: the graph is composed after the playbook loads, so
    which servers its nodes will name cannot be read off the file, and the
    pre-flight has to cover everything on offer instead.

    ``fills`` counts because ``mcps`` is fillable: a node the author left open
    can be handed servers by the caller, and a pre-flight that ignored them would
    connect everything except what this run actually asked for.
    """
    if spec.mode != "dag" or spec.nodes is None:
        return None
    names = set(spec.mcp_servers or {})
    for node in spec.nodes:
        names.update(node.mcps or ())
    for patch in (fills or {}).values():
        entry = (patch or {}).get("mcps")
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, Iterable):
            names.update(str(item) for item in entry)
    return names


@asynccontextmanager
async def preflight_mcp_source(
    *,
    host_servers: Mapping[str, MCPServerConfig],
    playbook_servers: Mapping[str, MCPServerConfig] | None = None,
    declared: set[str] | None = None,
    disabled_tools: Callable[[], frozenset[str]] = frozenset,
    sandbox_config: Any = None,
    workspace: Path,
    secret_values: Iterable[str] = (),
) -> AsyncIterator["McpSource"]:
    """Connect this run's MCP servers once, and yield the source grants read.

    The merged mapping is the whole of the precedence rule: ``playbook_servers``
    over ``host_servers``, and it is the same mapping the manager connects and
    the source resolves against, so what a grant can name and what was actually
    dialled cannot drift.

    A connect failure degrades rather than aborting: the server is left in its
    failed state, the grant reports it, and the graph runs without it. Aborting
    would make one unreachable server cost the whole run.
    """
    from raven.agent.subagent.mcp_grant import LiveMcpSource
    from raven.agent.tools.registry import ToolRegistry
    from raven.mcp.manager import MCPConnectionManager
    from raven.sandbox import build_executor

    servers: dict[str, MCPServerConfig] = {**host_servers, **(playbook_servers or {})}
    if declared is not None:
        servers = {name: cfg for name, cfg in servers.items() if name in declared}
    registry = ToolRegistry()
    manager = MCPConnectionManager(registry, allow_auth_park=False)
    sandbox = build_executor(sandbox_config, workspace)

    async def executor_provider():
        return sandbox

    # The provider rides on the source because a bridged upstream is spawned by
    # the endpoint rather than by this manager, and it has to land in the same
    # confinement the pre-flight dialled its own connections inside.
    source = LiveMcpSource(lambda: servers, lambda: manager, registry, disabled_tools, executor_provider)
    async with sandbox:
        try:
            if servers:
                logger.info("playbook MCP pre-flight: connecting {}", sorted(servers))
                try:
                    await manager.apply_config(servers, executor_provider=executor_provider)
                except Exception as exc:  # noqa: BLE001 - an unreachable server must not cost the run
                    logger.warning("playbook MCP pre-flight did not finish: {}", _scrub(str(exc), secret_values))
            yield source
        finally:
            try:
                await manager.aclose()
            except (RuntimeError, BaseExceptionGroup):
                # MCP SDK cancel-scope cleanup is noisy and harmless; the same
                # two are swallowed wherever a manager is closed.
                pass


def _scrub(text: str, secrets: Iterable[str]) -> str:
    """``text`` with every supplied secret value replaced.

    The pre-flight is the one log site holding configs whose ``env`` came from a
    ``secret`` param, and a transport error quoting the failing command is
    exactly the kind of message that carries one along.
    """
    for value in secrets:
        if value:
            text = text.replace(value, "***")
    return text


def unusable_servers(source: "McpSource", names: Iterable[str]) -> list[tuple[str, str]]:
    """``(name, state)`` for every named server the pre-flight did not bring up.

    For reporting, not for deciding: whether a node is granted a server is
    ``resolve_grant``'s answer and ``McpGrant.note_text()`` is the wording of it.
    ``auth_required`` is the state worth naming out loud here -- nothing is wrong
    with the machine, somebody has to authorize once -- and the pre-flight is the
    only moment a person is still around to be told.
    """
    out: list[tuple[str, str]] = []
    for name in sorted(set(names)):
        view = source.server(name)
        if view is None:
            out.append((name, "not_configured"))
        elif view.state not in (None, "connected"):
            out.append((name, str(view.state)))
    return out


__all__ = [
    "declared_mcp_names",
    "playbook_mcp_servers",
    "preflight_mcp_source",
    "unusable_servers",
]
