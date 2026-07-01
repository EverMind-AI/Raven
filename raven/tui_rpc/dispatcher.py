"""Self-written async JSON-RPC 2.0 dispatcher.

Decision: see design.md §3 D8 — we chose a self-written ~30-line dispatcher
over `ajsonrpc` / `jsonrpcserver` framework. Rationale:
- Pydantic v2 already covers schema validation (no framework dup).
- Newline-delimited JSON framing is trivial.
- Subscription registry + 16ms throttle is custom anyway.
- Fewer dependencies → smaller `pip-audit` surface.

Fallback: if cancellation/error edge cases blow up beyond budget, switch to
`ajsonrpc` and reuse its dispatch + add notification side-channel manually.
"""

from __future__ import annotations

import inspect
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from raven.tui_rpc.errors import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RpcError,
)

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class Dispatcher:
    """Routes JSON-RPC 2.0 request frames to registered async handlers.

    Usage:
        d = Dispatcher()
        d.register("system.hello", system_hello)
        response = await d.dispatch(request_frame)
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, method: str, handler: Handler) -> None:
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(f"handler for '{method}' must be async; got {type(handler).__name__}")
        if method in self._handlers:
            raise ValueError(f"method '{method}' already registered")
        self._handlers[method] = handler

    def methods(self) -> list[str]:
        return sorted(self._handlers)

    async def dispatch(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Validate frame, route to handler, wrap result/error per JSON-RPC 2.0.

        Per spec, parse errors and frames without a recoverable `id` still
        return a response with `id: null`; callers may choose to drop those.
        """
        # ----- Frame validation -----------------------------------------------------
        if not isinstance(frame, dict):
            return _err_frame(None, PARSE_ERROR, "parse_error", data={"reason": "frame is not an object"})

        frame_id = frame.get("id")  # may be None for notification frames

        if frame.get("jsonrpc") != "2.0":
            return _err_frame(
                frame_id,
                INVALID_REQUEST,
                "invalid_request",
                data={"reason": "missing or wrong jsonrpc version"},
            )

        method = frame.get("method")
        if not isinstance(method, str) or not method:
            return _err_frame(
                frame_id,
                INVALID_REQUEST,
                "invalid_request",
                data={"reason": "missing or non-string method"},
            )

        params = frame.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _err_frame(
                frame_id,
                INVALID_REQUEST,
                "invalid_request",
                data={"reason": "params must be an object"},
            )

        # ----- Method routing -------------------------------------------------------
        handler = self._handlers.get(method)
        if handler is None:
            return _err_frame(frame_id, METHOD_NOT_FOUND, "method_not_found", data={"method": method})

        # ----- Dispatch -------------------------------------------------------------
        try:
            result = await handler(params)
        except RpcError as exc:
            err_payload: dict[str, Any] = {
                "code": exc.code,
                "message": exc.message,
            }
            if exc.data is not None:
                err_payload["data"] = exc.data
            elif exc.detail:
                err_payload["data"] = {"detail": exc.detail}
            return {"jsonrpc": "2.0", "id": frame_id, "error": err_payload}
        except SystemExit as exc:
            # Click/Typer can leak SystemExit even with standalone_mode=False;
            # treat as internal error rather than crashing the dispatcher.
            tb_tail = _truncate_traceback(traceback.format_exc())
            logger.warning("tui_rpc: SystemExit in handler {}: code={}", method, exc.code)
            return _err_frame(
                frame_id,
                INTERNAL_ERROR,
                "internal_error",
                data={"reason": "SystemExit from handler", "traceback_tail": tb_tail},
            )
        except Exception:
            tb_tail = _truncate_traceback(traceback.format_exc())
            logger.exception("tui_rpc: unhandled exception in handler {}", method)
            return _err_frame(
                frame_id,
                INTERNAL_ERROR,
                "internal_error",
                data={"traceback_tail": tb_tail},
            )

        # ----- Result validation ---------------------------------------------------
        if not isinstance(result, dict):
            logger.error("tui_rpc: handler {} returned non-dict result", method)
            return _err_frame(
                frame_id,
                INTERNAL_ERROR,
                "internal_error",
                data={"reason": f"handler returned {type(result).__name__}, expected dict"},
            )

        return {"jsonrpc": "2.0", "id": frame_id, "result": result}


def _err_frame(
    frame_id: Any,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": frame_id, "error": err}


def _truncate_traceback(tb: str, max_lines: int = 12) -> str:
    """Truncate a formatted traceback to its trailing N lines."""
    lines = tb.strip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "...\n" + "\n".join(lines[-max_lines:])


__all__ = ["Dispatcher", "Handler"]
