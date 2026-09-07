"""Hosted terminal RPC operations and native TUI size reporting."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.contracts.terminal import TerminalError
from raven.rpc.errors import RpcError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


# Module-level latest-known terminal size. ``None`` means "no resize event
# observed yet"; callers should fall back to ``shutil.get_terminal_size()``
# or a sensible default (80 cols) in that case.
_LATEST_COLS: int | None = None
_LATEST_ROWS: int | None = None


def _coerce_dim(value: Any) -> int | None:
    """Return ``value`` as a positive int, else ``None``."""
    if isinstance(value, bool):
        # bool is a subclass of int — reject explicitly.
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


async def terminal_resize(params: dict) -> dict:
    """``terminal.resize`` — record dimensions, return ``{ok: true}``.

    Accepts ``{cols, rows}`` (both optional positive ints). Anything else is
    silently coerced to a no-op record — we never raise here because the
    upstream SIGWINCH burst would otherwise flood error frames.
    """
    global _LATEST_COLS, _LATEST_ROWS
    if isinstance(params, dict):
        cols = _coerce_dim(params.get("cols"))
        rows = _coerce_dim(params.get("rows"))
        if cols is not None:
            _LATEST_COLS = cols
        if rows is not None:
            _LATEST_ROWS = rows
    return {"ok": True}


def register_terminal_methods(
    dispatcher: "Dispatcher", *, host=None, delivery=None, stream=None, receive_host=None, bind_session=None
):
    """Register hosted terminal operations, retaining the native TUI resize path."""
    from raven.rpc.methods.runtime import runtime_status

    dispatcher.register("runtime.status", runtime_status)
    if host is None:
        dispatcher.register("terminal.resize", terminal_resize)
        return None
    methods = TerminalMethods(host, delivery, stream, receive_host=receive_host, bind_session=bind_session)
    for name in ("create", "list", "show", "send", "wait", "close", "subscribe", "input", "resize", "rename"):
        dispatcher.register(f"terminal.{name}", methods.handler(name))
    return methods


__all__ = [
    "terminal_resize",
    "register_terminal_methods",
]


class TerminalMethodError(RpcError):
    def __init__(self, code: str, message: str = "", *, invalid: bool = False, data=None):
        self.CODE = -32602 if invalid else -32099
        self.MESSAGE = code
        super().__init__(message or code, data=data)


def _rpc_error(code: str, message: str = "", *, invalid: bool = False, data=None):
    return TerminalMethodError(code, message, invalid=invalid, data=data)


def _text(params, key, default=None):
    value = params.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise _rpc_error("invalid_argument", f"{key} must be a nonempty string", invalid=True)
    return value


def _integer(params, key, default, maximum=1_000_000):
    value = params.get(key, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise _rpc_error("invalid_argument", f"{key} must be between 1 and {maximum}", invalid=True)
    return value


def _boolean(params, key, default=False):
    value = params.get(key, default)
    if type(value) is not bool:
        raise _rpc_error("invalid_argument", f"{key} must be a boolean", invalid=True)
    return value


def terminal_json(record):
    """Project the host record into the terminal topology's public field names."""
    if isinstance(record, dict):
        return record
    return record.model_dump(by_alias=True, mode="json")


class TerminalMethods:
    """Transport validation and topology projection for an injected terminal host."""

    def __init__(self, host, delivery=None, stream=None, *, receive_host=None, bind_session=None):
        self.host = host
        self.delivery = delivery
        self.stream = stream
        self.receive_host = receive_host
        self.bind_session = bind_session
        self._topologies = {}
        self._revisions = {}

    def _record(self, record):
        result = terminal_json(record)
        if self.stream is not None:
            output = self.stream.outputs.get(result["handle"])
            result["visible"] = bool(output and output.acks)
        return result

    def _owner(self):
        from raven.rpc.connection import current_state

        return (current_state() or {}).get("terminal_owner", "human")

    async def create(self, params):
        session = _text(params, "session_id") if "session_id" in params else None
        command = params.get("command")
        if not (isinstance(command, str) and command.strip()) and not (
            isinstance(command, list) and command and all(isinstance(s, str) and s for s in command)
        ):
            raise _rpc_error("invalid_argument", "command must be a string or nonempty argv", invalid=True)
        owner = self._owner()
        requested_owner = _text(params, "owner", owner)
        if owner != "human" and requested_owner != owner:
            raise _rpc_error("forbidden", "Cannot create a terminal for another owner")
        record = await self.host.create(
            worktree_id=_text(params, "worktree_id"),
            command=command,
            title=_text(params, "title", "Terminal"),
            owner=requested_owner,
        )
        if session and self.bind_session is not None:
            self.bind_session(record.handle, session)
        return {"terminal": self._record(record)}

    async def list(self, params):
        limit = _integer(params, "limit", 1000)
        layouts = _boolean(params, "include_visual_layouts")
        worktree_id = params.get("worktree_id")
        if worktree_id is not None:
            worktree_id = _text(params, "worktree_id")
        records = [self._record(r) for r in self.host.list(worktree_id=worktree_id)]
        worktrees = {r["worktreeId"] for r in records}
        if worktree_id:
            worktrees.add(worktree_id)
        revisions = {}
        for worktree in sorted(worktrees):
            topology = tuple(
                (r["handle"], r["incarnationId"], r["tabId"], r["leafId"], r["title"])
                for r in records
                if r["worktreeId"] == worktree
            )
            if self._topologies.get(worktree) != topology:
                self._revisions[worktree] = self._revisions.get(worktree, 0) + 1
                self._topologies[worktree] = topology
            revisions[worktree] = self._revisions[worktree]
        result = {
            "terminals": records[:limit],
            "truncated": len(records) > limit,
            "hostScope": {"hostIds": ["local"], "omittedHostIds": []},
            "topologyRevisions": revisions,
        }
        if layouts:
            result["visualLayouts"] = [
                {
                    "worktreeId": worktree,
                    "root": {
                        "type": "group",
                        "tabs": [
                            {
                                "tabId": r["tabId"],
                                "title": r["title"],
                                "panes": {
                                    "type": "terminal",
                                    "handle": r["handle"],
                                    "tabId": r["tabId"],
                                    "leafId": r["leafId"],
                                    "title": r["title"],
                                },
                            }
                            for r in records[:limit]
                            if r["worktreeId"] == worktree
                        ],
                    },
                }
                for worktree in sorted(worktrees)
            ]
        return result

    async def show(self, params):
        return {"terminal": self._record(self.host.show(_text(params, "handle")))}

    async def send(self, params):
        text = params.get("text")
        if not isinstance(text, str):
            raise _rpc_error("invalid_argument", "text must be a string", invalid=True)
        enter = _boolean(params, "enter")
        require_ack = _boolean(params, "require_ack")
        if params.get("to") is not None:
            if params.get("handle") is not None or params["to"] != "raven":
                raise _rpc_error("invalid_argument", "Use one handle or to=raven", invalid=True)
            if self.receive_host is None:
                raise _rpc_error("terminal_unavailable")
            source = _text(params, "source_handle") if "source_handle" in params else None
            return {"send": await self.receive_host(text, source)}
        if self.delivery is None:
            raise _rpc_error("terminal_unavailable")
        handle = _text(params, "handle")
        if "session_id" in params:
            session = _text(params, "session_id")
            if self.bind_session is not None:
                self.bind_session(handle, session)
        result = await self.delivery.send(
            handle,
            text,
            enter=enter,
            require_ack=require_ack,
        )
        return {"send": terminal_json(result)}

    async def wait(self, params):
        condition = _text(params, "for", "tui-idle")
        if condition not in {"tui-idle", "exit"}:
            raise _rpc_error("invalid_argument", "for must be tui-idle or exit", invalid=True)
        timeout = _integer(params, "timeout_ms", 300000, 3_600_000)
        if self.delivery is None:
            raise _rpc_error("terminal_unavailable")
        result = await self.delivery.wait(_text(params, "handle"), for_condition=condition, timeout_ms=timeout)
        return {"wait": terminal_json(result)}

    async def close(self, params):
        handle = _text(params, "handle")
        await self.host.close(handle, owner=self._owner())
        return {"close": {"handle": handle, "closed": True}}

    async def input(self, params):
        data = params.get("data")
        if not isinstance(data, str) or len(data.encode("utf-8")) > 65536:
            raise _rpc_error("invalid_argument", "data must be a string of at most 64 KiB", invalid=True)
        handle = _text(params, "handle")
        await self.host.input(handle, data.encode("utf-8"))
        return {"input": {"handle": handle, "bytesWritten": len(data.encode("utf-8"))}}

    async def resize(self, params):
        if "handle" not in params:
            return await terminal_resize(params)
        handle = _text(params, "handle")
        cols = _integer(params, "cols", None, 10000)
        rows = _integer(params, "rows", None, 10000)
        await self.host.resize(handle, cols, rows)
        return {"ok": True}

    async def subscribe(self, params):
        if self.stream is None:
            raise _rpc_error("terminal_stream_unavailable")
        return await self.stream.subscribe(params)

    async def rename(self, params):
        record = terminal_json(self.host.show(_text(params, "handle")))
        title = _text(params, "title")
        if title != record["title"]:
            raise _rpc_error("title_is_canonical_projection", "Terminal titles follow the canonical agent name")
        return {"rename": {"handle": record["handle"], "tabId": record["tabId"], "title": title, "liveTitle": title}}

    def handler(self, method):
        async def invoke(params):
            try:
                return await getattr(self, method)(params)
            except TerminalError as exc:
                raise _rpc_error(exc.code, str(exc), data=exc.data) from exc

        return invoke
