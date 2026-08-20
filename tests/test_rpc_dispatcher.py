"""The dispatcher's two last-resort guards must answer, not crash.

``Dispatcher.dispatch`` is the only thing between a buggy handler and the
frame pump. Two of its guards had no coverage, and both of them exist because
a handler already violated the contract once:

1. ``SystemExit`` — Click/Typer raise it even with ``standalone_mode=False``,
   so any handler that reaches into the CLI can leak one. ``SystemExit``
   derives from ``BaseException``, *not* ``Exception``, so the generic
   ``except Exception`` below it does not catch it: without the dedicated
   branch the exception unwinds through ``_handle_frame`` and the caller's
   request never gets a response.
2. A non-dict return — the ``Handler`` alias promises ``dict[str, Any]``, but
   the annotation is not enforced at runtime. Returning a list would put
   ``"result": [...]`` on the wire, which the TypeScript client decodes into
   the wrong shape rather than failing loudly.

Both must come back as a well-formed JSON-RPC error frame carrying the
request's own ``id``, because a client that never sees ``id`` again keeps the
pending promise forever.
"""

from __future__ import annotations

from typing import Any

import pytest

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.errors import INTERNAL_ERROR


def _request(method: str, rid: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": {}}


@pytest.mark.asyncio
async def test_system_exit_from_handler_becomes_an_error_frame() -> None:
    """A handler leaking SystemExit gets converted, not propagated."""

    async def _exits(_params: dict[str, Any]) -> dict[str, Any]:
        raise SystemExit(2)

    disp = Dispatcher()
    disp.register("test.exits", _exits)

    frame = await disp.dispatch(_request("test.exits", rid=7))

    assert frame["id"] == 7, "the response must carry the request id back"
    assert frame["error"]["code"] == INTERNAL_ERROR
    assert frame["error"]["message"] == "internal_error"
    assert frame["error"]["data"]["reason"] == "SystemExit from handler"
    # The traceback tail is what makes this debuggable; it is capped at 12
    # lines by _truncate_traceback so a deep stack cannot bloat the frame.
    assert frame["error"]["data"]["traceback_tail"]
    assert "result" not in frame


@pytest.mark.asyncio
async def test_non_dict_handler_result_becomes_an_error_frame() -> None:
    """A handler returning the wrong type is rejected at the boundary."""

    async def _returns_list(_params: dict[str, Any]) -> Any:
        return ["not", "a", "dict"]

    disp = Dispatcher()
    disp.register("test.wrong_type", _returns_list)

    frame = await disp.dispatch(_request("test.wrong_type", rid=9))

    assert frame["id"] == 9
    assert frame["error"]["code"] == INTERNAL_ERROR
    # The concrete type name is in the payload so the fix does not need a
    # server-side log dive.
    assert frame["error"]["data"]["reason"] == "handler returned list, expected dict"
    assert "result" not in frame


@pytest.mark.asyncio
async def test_a_dict_returning_handler_still_takes_the_result_path() -> None:
    """Guard against the isinstance check drifting into rejecting valid dicts."""

    async def _ok(_params: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True}

    disp = Dispatcher()
    disp.register("test.ok", _ok)

    frame = await disp.dispatch(_request("test.ok", rid=11))

    assert frame == {"jsonrpc": "2.0", "id": 11, "result": {"pong": True}}
