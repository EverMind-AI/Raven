"""The node side: Raven's own file tools, answered over stdio.

Started on a registered machine as ``python -m raven.node --stdio`` by the host
(:mod:`raven.node.client`), over ssh. A request is a JSON-RPC 2.0 object on one
line of stdin; its answer is one line on stdout. Each file call runs the very
tool the host would run on its own disk -- ``read_file``, ``list_dir``,
``find``, ``grep`` -- so a read of a remote file comes back exactly as a local
one would: the same line numbers, the same limits, the same footer.

Deliberately small. It imports the file tools and nothing that talks to a
model, reads a config or opens a socket, so a machine needs Python 3.12 and
the packages in :data:`raven.node.protocol.REQUIREMENTS` beside Raven's own
code, not the whole of Raven's dependencies (about 1 GB, measured 2026-10-10).

Requests are answered one at a time, in the order they arrive.
"""

from __future__ import annotations

import asyncio
import json
import platform
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, BinaryIO

from raven.node import bundle, protocol

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

#: A request line the node will read; a file call is a few hundred bytes.
_LINE_LIMIT = 1024 * 1024


class _RefusedError(Exception):
    """A request answered with a JSON-RPC error rather than a result."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _text(params: dict[str, Any], name: str, *, default: str | None = None) -> str:
    value = params.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise _RefusedError(_INVALID_PARAMS, f"{name} must be a non-empty string")
    return value


def _number(params: dict[str, Any], name: str, default: int | None = None) -> int | None:
    value = params.get(name, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _RefusedError(_INVALID_PARAMS, f"{name} must be a whole number")
    return value


def _flag(params: dict[str, Any], name: str) -> bool:
    value = params.get(name, False)
    if not isinstance(value, bool):
        raise _RefusedError(_INVALID_PARAMS, f"{name} must be true or false")
    return value


def _roots(params: dict[str, Any]) -> tuple[Path, ...]:
    """The directories this call may touch, as given, made absolute on this machine.

    Required and never empty: the file tools read an empty fence as "no fence",
    so a call that named none would reach the whole disk.
    """
    raw = params.get("roots")
    if not isinstance(raw, list) or not raw or not all(isinstance(r, str) and r.strip() for r in raw):
        raise _RefusedError(_INVALID_PARAMS, "roots must be a non-empty list of directories")
    out = []
    for root in raw:
        path = Path(root.strip()).expanduser()
        if not path.is_absolute():
            raise _RefusedError(_INVALID_PARAMS, f"root {root!r} is not an absolute directory")
        out.append(path)
    return tuple(out)


def _tool(cls: Any, params: dict[str, Any]) -> Any:
    """One of Raven's file tools, fenced to this call's roots.

    Relative paths resolve against the home directory, as a path after the
    colon in ``host:path`` does for scp. ``follow_binding=False``: there is no
    turn here whose working directory could widen the fence.
    """
    return cls(workspace=Path.home(), allowed_dirs=_roots(params), follow_binding=False)


def _answer(value: Any) -> dict[str, Any]:
    """A tool's return value as the wire carries it: the text, and an image's blocks."""
    if isinstance(value, str):
        return {"text": value}
    return {
        "text": value.model_text,
        "display": value.display_text,
        "ok": bool(value.ok),
        "blocks": list(getattr(value, "blocks", None) or []),
    }


async def _hello(params: dict[str, Any]) -> dict[str, Any]:
    asked = params.get("protocol")
    if asked is not None and asked != protocol.PROTOCOL:
        raise _RefusedError(_INVALID_REQUEST, f"this node speaks protocol {protocol.PROTOCOL}, not {asked!r}")
    from raven.agent.tools.file_search import _resolve_rg

    return {
        "protocol": protocol.PROTOCOL,
        "digest": bundle.digest(),
        "python": platform.python_version(),
        "home": str(Path.home()),
        # Whether ``grep`` runs rg here or Raven's own Python search: the two
        # differ on regex corners, and the host may want to say which ran.
        "rg": _resolve_rg() is not None,
    }


async def _read(params: dict[str, Any]) -> dict[str, Any]:
    from raven.agent.tools.filesystem import ReadFileTool

    tool = _tool(ReadFileTool, params)
    return _answer(
        await tool.execute(
            _text(params, "path"), offset=_number(params, "offset", 1) or 1, limit=_number(params, "limit")
        )
    )


async def _list(params: dict[str, Any]) -> dict[str, Any]:
    from raven.agent.tools.filesystem import ListDirTool

    tool = _tool(ListDirTool, params)
    return _answer(
        await tool.execute(
            _text(params, "path"), recursive=_flag(params, "recursive"), max_entries=_number(params, "max_entries")
        )
    )


async def _find(params: dict[str, Any]) -> dict[str, Any]:
    from raven.agent.tools.file_search import FindTool

    tool = _tool(FindTool, params)
    return _answer(
        await tool.execute(
            _text(params, "pattern"), path=_text(params, "path", default="."), limit=_number(params, "limit")
        )
    )


async def _grep(params: dict[str, Any]) -> dict[str, Any]:
    from raven.agent.tools.file_search import GrepTool

    tool = _tool(GrepTool, params)
    glob = params.get("glob")
    if glob is not None and not isinstance(glob, str):
        raise _RefusedError(_INVALID_PARAMS, "glob must be a string")
    mode = params.get("output_mode", "content")
    if not isinstance(mode, str):
        raise _RefusedError(_INVALID_PARAMS, "output_mode must be a string")
    return _answer(
        await tool.execute(
            _text(params, "pattern"),
            path=_text(params, "path", default="."),
            glob=glob,
            output_mode=mode,
            case_insensitive=_flag(params, "case_insensitive"),
            context=_number(params, "context", 0) or 0,
            limit=_number(params, "limit"),
        )
    )


_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    protocol.HELLO: _hello,
    protocol.READ: _read,
    protocol.LIST: _list,
    protocol.FIND: _find,
    protocol.GREP: _grep,
}


async def handle(frame: Any) -> dict[str, Any] | None:
    """The answer to one decoded line, or ``None`` when nothing is owed (a notification)."""
    if not isinstance(frame, dict) or not isinstance(frame.get("method"), str):
        return None
    request_id = frame.get("id")
    if request_id is None:
        return None
    try:
        handler = _HANDLERS.get(frame["method"])
        if handler is None:
            raise _RefusedError(_METHOD_NOT_FOUND, f"unknown method {frame['method']!r}")
        params = frame.get("params") or {}
        if not isinstance(params, dict):
            raise _RefusedError(_INVALID_PARAMS, "params must be an object")
        return {"jsonrpc": "2.0", "id": request_id, "result": await handler(params)}
    except _RefusedError as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message}}
    except Exception as exc:  # noqa: BLE001 - the host gets a reason; the node keeps serving
        message = f"{type(exc).__name__}: {exc}"[:500]
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": _INTERNAL_ERROR, "message": message}}


async def serve(reader: asyncio.StreamReader, out: BinaryIO) -> None:
    """Answer requests until stdin closes."""
    while True:
        try:
            line = await reader.readline()
        except ValueError:
            # Longer than any request; the reader has dropped it and goes on.
            continue
        if not line:
            return
        text = line.strip()
        if not text:
            continue
        try:
            frame = json.loads(text)
        except ValueError:
            answer: dict[str, Any] | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": _PARSE_ERROR, "message": "not a JSON object"},
            }
        else:
            answer = await handle(frame)
        if answer is not None:
            out.write(json.dumps(answer, ensure_ascii=False).encode("utf-8") + b"\n")
            out.flush()


def run_stdio() -> int:
    """Serve on this process's stdin and stdout; return when the host hangs up."""
    out = sys.stdout.buffer
    # stdout is the protocol. Anything that prints by accident -- a library, a
    # stray debug line -- goes to stderr instead of corrupting a frame.
    sys.stdout = sys.stderr

    async def main() -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=_LINE_LIMIT)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        await serve(reader, out)

    asyncio.run(main())
    return 0


__all__ = ["handle", "run_stdio", "serve"]
