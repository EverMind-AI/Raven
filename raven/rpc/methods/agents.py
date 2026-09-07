"""JSON-RPC access to the serve-owned peer identity table."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from pydantic import ValidationError

from raven.agent.registry.identity import IdentityRegistry
from raven.contracts.terminal import RuntimeInfo, TerminalError, TerminalRecord
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.errors import RpcError


class IdentityRpcError(RpcError):
    MESSAGE = "agent_identity_error"


def register_agents_methods(
    dispatcher: Dispatcher,
    *,
    registry: IdentityRegistry | None = None,
    terminal_show: Callable[[str], TerminalRecord] | None = None,
) -> None:
    async def invoke(method: str, params: dict) -> dict:
        nonlocal registry
        try:
            if registry is None:
                registry = IdentityRegistry()
            if method == "register":
                name = params.get("name") or params.get("agent_name")
                if not isinstance(name, str) or not name:
                    raise ValueError("name is required")
                kind = params.get("kind") or params.get("kind_ref") or name
                binding = None
                if handle := params.get("terminal"):
                    if terminal_show is None:
                        raise TerminalError("terminal_unavailable", "Terminal host is unavailable")
                    binding = terminal_show(handle)
                record = await asyncio.to_thread(
                    registry.register,
                    name,
                    kind_ref=kind,
                    binding=binding,
                    role=params.get("role", ""),
                    task_ref=params.get("task_ref", ""),
                    session_key=params.get("session_key"),
                    description=params.get("description"),
                    aliases=params.get("aliases"),
                )
                result = {"agent": record.model_dump(by_alias=True, mode="json")}
            elif method == "list":
                records = await asyncio.to_thread(registry.list)
                result = {"agents": [record.model_dump(by_alias=True, mode="json") for record in records]}
            elif method == "show":
                name = params.get("name") or params.get("agent_name")
                if not isinstance(name, str) or not name:
                    raise ValueError("name is required")
                record = await asyncio.to_thread(registry.show, name)
                result = {"agent": record.model_dump(by_alias=True, mode="json")}
            else:
                mention = params.get("mention")
                if not isinstance(mention, str) or not mention:
                    raise ValueError("mention is required")
                result = await asyncio.to_thread(registry.resolve, mention)
        except TerminalError as exc:
            raise IdentityRpcError(
                str(exc), data={"code": exc.code, "message": str(exc), "_meta": RuntimeInfo().model_dump(by_alias=True)}
            ) from exc
        except (ValueError, ValidationError, TypeError) as exc:
            error = IdentityRpcError(
                str(exc), data={"code": "invalid_params", "_meta": RuntimeInfo().model_dump(by_alias=True)}
            )
            error.CODE = -32602
            raise error from exc
        return {**result, "_meta": RuntimeInfo().model_dump(by_alias=True)}

    for method in ("register", "list", "show", "resolve"):

        async def handler(params: dict, method: str = method) -> dict:
            return await invoke(method, params)

        dispatcher.register(f"agents.{method}", handler)
