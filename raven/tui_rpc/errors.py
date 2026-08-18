"""Custom RPC exception classes mapped to JSON-RPC 2.0 error codes.

Code table — frozen in `specs/tui-ipc.md` §4 (server-defined range -32000..-32099):

| code   | message                       | meaning                          |
|--------|-------------------------------|----------------------------------|
| -32001 | session_not_found             | session_key unknown              |
| -32002 | session_locked                | session held by another client   |
| -32003 | turn_in_progress              | session currently running a turn |
| -32004 | mcp_server_not_connected      | unknown / disconnected MCP       |
| -32005 | mcp_tool_call_failed          | MCP list_tools failure           |
| -32006 | skill_not_found               | skill_name not indexed           |
| -32007 | skill_pin_conflict            | pin/unpin already in that state  |
| -32008 | model_not_available           | model_id not routable            |
| -32009 | model_switch_in_turn          | switch attempt while turn live   |
| -32010 | config_field_readonly         | not on hot-changeable whitelist  |
| -32011 | config_validation_error       | Pydantic / semver validation     |
| -32012 | not_supported_in_v01          | hermes-only stub methods         |
| -32013 | cli_command_failed            | cli.dispatch exit_code != 0      |
| -32014 | cli_command_timeout           | cli.dispatch 30s timeout         |
| -32015 | not_dispatch_compatible       | interactive Rich widget rejected |

JSON-RPC pre-defined codes (-32700/-32600/-32601/-32602) are emitted directly
by the dispatcher and have no dedicated exception class.

-32603 ``internal_error`` is also dispatcher-emitted for uncaught handler
exceptions, but it has a dedicated ``InternalError`` class so non-dispatcher
code-paths (notably the ``_build_tui_agent_loop`` factory invoked from
``_spawn_agent_loop_task``) can raise typed -32603 cross-module. Without it
those paths conflated init crashes into -32008 ``model_not_available``.
"""

from __future__ import annotations

from typing import Any, ClassVar


class RpcError(Exception):
    """Base class for all RPC errors mapped to JSON-RPC error frames.

    Subclasses set the class-level `CODE` and `MESSAGE` constants; the
    dispatcher reads them when serializing the error frame. `data` is
    optional structured context (echoed into JSON-RPC `error.data`).
    """

    CODE: ClassVar[int] = -32099  # catch-all
    MESSAGE: ClassVar[str] = "rpc_error"

    def __init__(self, detail: str = "", data: dict[str, Any] | None = None):
        self.detail = detail
        self.data = data
        # Default str() shows code + message + optional detail
        super().__init__(f"{self.MESSAGE}: {detail}" if detail else self.MESSAGE)

    @property
    def code(self) -> int:
        return self.CODE

    @property
    def message(self) -> str:
        return self.MESSAGE


class SessionNotFoundError(RpcError):
    CODE = -32001
    MESSAGE = "session_not_found"


class SessionLockedError(RpcError):
    CODE = -32002
    MESSAGE = "session_locked"


class TurnInProgressError(RpcError):
    CODE = -32003
    MESSAGE = "turn_in_progress"


class McpServerNotConnectedError(RpcError):
    CODE = -32004
    MESSAGE = "mcp_server_not_connected"


class McpToolCallFailedError(RpcError):
    CODE = -32005
    MESSAGE = "mcp_tool_call_failed"


class SkillNotFoundError(RpcError):
    CODE = -32006
    MESSAGE = "skill_not_found"


class SkillPinConflictError(RpcError):
    CODE = -32007
    MESSAGE = "skill_pin_conflict"


class ModelNotAvailableError(RpcError):
    CODE = -32008
    MESSAGE = "model_not_available"


class ModelSwitchInTurnError(RpcError):
    CODE = -32009
    MESSAGE = "model_switch_in_turn"


class ConfigFieldReadonlyError(RpcError):
    CODE = -32010
    MESSAGE = "config_field_readonly"


class ConfigValidationError(RpcError):
    CODE = -32011
    MESSAGE = "config_validation_error"


class NotSupportedInV01Error(RpcError):
    CODE = -32012
    MESSAGE = "not_supported_in_v01"


class CliCommandFailedError(RpcError):
    CODE = -32013
    MESSAGE = "cli_command_failed"


class CliCommandTimeoutError(RpcError):
    CODE = -32014
    MESSAGE = "cli_command_timeout"


class NotDispatchCompatibleError(RpcError):
    CODE = -32015
    MESSAGE = "not_dispatch_compatible"


# Follow-up extension range (-32016..-32049). -32016 is subscription
# overflow; was incorrectly aliased to -32010 in early drafts — -32010 is
# already ConfigFieldReadonlyError.
class SubscriptionCapacityExceededError(RpcError):
    CODE = -32016
    MESSAGE = "subscription_capacity_exceeded"


# JSON-RPC pre-defined ``internal_error`` (-32603). Class added so non-dispatcher
# code-paths can raise typed -32603 cross-module — see ``_build_tui_agent_loop``
# which runs outside any handler context yet needs to surface init crashes
# through the factory closure consumed by ``_spawn_agent_loop_task``.
class InternalError(RpcError):
    CODE = -32603
    MESSAGE = "internal_error"


# JSON-RPC pre-defined error codes (specs §2.3 / RFC).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# Reverse lookup: JSON-RPC code → exception class. Useful for client-side
# reconstruction or test assertions.
JSONRPC_ERROR_REGISTRY: dict[int, type[RpcError]] = {
    cls.CODE: cls
    for cls in (
        SessionNotFoundError,
        SessionLockedError,
        TurnInProgressError,
        McpServerNotConnectedError,
        McpToolCallFailedError,
        SkillNotFoundError,
        SkillPinConflictError,
        ModelNotAvailableError,
        ModelSwitchInTurnError,
        ConfigFieldReadonlyError,
        ConfigValidationError,
        NotSupportedInV01Error,
        CliCommandFailedError,
        CliCommandTimeoutError,
        NotDispatchCompatibleError,
        SubscriptionCapacityExceededError,
        InternalError,
    )
}


__all__ = [
    "RpcError",
    "SessionNotFoundError",
    "SessionLockedError",
    "TurnInProgressError",
    "McpServerNotConnectedError",
    "McpToolCallFailedError",
    "SkillNotFoundError",
    "SkillPinConflictError",
    "ModelNotAvailableError",
    "ModelSwitchInTurnError",
    "ConfigFieldReadonlyError",
    "ConfigValidationError",
    "NotSupportedInV01Error",
    "CliCommandFailedError",
    "CliCommandTimeoutError",
    "NotDispatchCompatibleError",
    "SubscriptionCapacityExceededError",
    "InternalError",
    "JSONRPC_ERROR_REGISTRY",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
]
