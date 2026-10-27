"""The frames a relayed model call travels in, and how its values survive the trip.

A Raven agent whose LiteLLM calls are made by the host that launched it
(:mod:`raven.providers.relay`) talks to that host over a local socket. Every
message is one frame: a four-byte big-endian length, then that many bytes of
UTF-8 JSON. Length-prefixed rather than line-delimited because a request is a
whole conversation, images included, and a line reader would need a limit large
enough for the largest one.

What crosses is the provider vocabulary of :mod:`raven.contracts.llm_provider`
-- deltas, responses, tool calls, classifications, call records -- encoded by
name from a fixed list, never by pickling: the socket is local, but neither
side should have to trust the other's bytes to build arbitrary objects.

A failure crosses as the facts the agent's own retry and classification read
off an exception: its message, the class names along its type and cause chain,
the HTTP status, the response body and kept headers. The agent rebuilds an
exception that answers those reads the way the original did, so a rate limit
on the host is a rate limit in the agent's retry ladder, and a first-byte or
idle timeout is the same class it would have been without the relay.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import socket
import struct
from types import SimpleNamespace
from typing import Any

from raven.contracts.llm_provider import (
    CallRecord,
    ChatDelta,
    ErrorClassification,
    GenerationSettings,
    LLMResponse,
    RunMeta,
    ToolCallRequest,
    TruncationInfo,
)

#: Bounds one frame. A request carries the whole conversation including any
#: images, so this is far above any real call and exists only so a corrupt
#: length cannot make a reader allocate without limit.
MAX_FRAME_BYTES = 512 * 1024 * 1024

_HEADER = struct.Struct(">I")

_DATACLASSES: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        CallRecord,
        ChatDelta,
        ErrorClassification,
        GenerationSettings,
        LLMResponse,
        RunMeta,
        ToolCallRequest,
        TruncationInfo,
    )
}
_TAG = "__relay__"
_SENTINEL_TAG = "sentinel"


class FrameError(ConnectionError):
    """The other side sent something that is not a frame, or went away mid-frame."""


def _pack(message: dict[str, Any]) -> bytes:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise FrameError(f"relay frame of {len(body)} bytes exceeds {MAX_FRAME_BYTES}")
    return _HEADER.pack(len(body)) + body


def _unpack(body: bytes) -> dict[str, Any]:
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError(f"relay frame is not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise FrameError("relay frame is not an object")
    return message


def _length(header: bytes) -> int:
    (size,) = _HEADER.unpack(header)
    if size > MAX_FRAME_BYTES:
        raise FrameError(f"relay frame announces {size} bytes, over {MAX_FRAME_BYTES}")
    return size


async def write_frame(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(_pack(message))
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """The next frame, or ``None`` when the other side closed between frames."""
    try:
        header = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            raise FrameError("relay connection closed inside a frame header") from exc
        return None
    try:
        body = await reader.readexactly(_length(header))
    except asyncio.IncompleteReadError as exc:
        raise FrameError("relay connection closed inside a frame") from exc
    return _unpack(body)


def send_frame(sock: socket.socket, message: dict[str, Any]) -> None:
    sock.sendall(_pack(message))


def _recv_exactly(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(min(remaining, 1 << 20))
        if not chunk:
            raise FrameError("relay connection closed inside a frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock: socket.socket) -> dict[str, Any]:
    return _unpack(_recv_exactly(sock, _length(_recv_exactly(sock, _HEADER.size))))


# ---- values ----------------------------------------------------------------


def encode(value: Any, *, sentinel: object | None = None) -> Any:
    """``value`` as plain JSON, with the provider vocabulary tagged by class name.

    ``sentinel`` is the provider's "not given" marker, which travels as a tag so
    the far side substitutes its own: the two processes hold different objects.
    Anything outside the vocabulary that a client library leaves in a payload --
    a pydantic model in a usage block -- is reduced to its plain form.
    """
    if sentinel is not None and value is sentinel:
        return {_TAG: _SENTINEL_TAG}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type) and type(value).__name__ in _DATACLASSES:
        fields = {f.name: encode(getattr(value, f.name), sentinel=sentinel) for f in dataclasses.fields(value)}
        return {_TAG: type(value).__name__, "fields": fields}
    if isinstance(value, dict):
        return {str(key): encode(item, sentinel=sentinel) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode(item, sentinel=sentinel) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return encode(dump(), sentinel=sentinel)
        except Exception:  # noqa: BLE001 - fall through to the generic forms below
            pass
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return encode(dataclasses.asdict(value), sentinel=sentinel)
    return str(value)


def decode(value: Any, *, sentinel: object | None = None) -> Any:
    """The inverse of :func:`encode`; ``sentinel`` is this side's "not given" marker."""
    if isinstance(value, list):
        return [decode(item, sentinel=sentinel) for item in value]
    if not isinstance(value, dict):
        return value
    tag = value.get(_TAG)
    if tag == _SENTINEL_TAG:
        return sentinel
    if isinstance(tag, str) and tag in _DATACLASSES and isinstance(value.get("fields"), dict):
        cls = _DATACLASSES[tag]
        known = {f.name for f in dataclasses.fields(cls)}
        fields = {k: decode(v, sentinel=sentinel) for k, v in value["fields"].items() if k in known}
        if cls is CallRecord and isinstance(fields.get("headers"), dict):
            fields["headers"] = {str(k): str(v) for k, v in fields["headers"].items()}
        return cls(**fields)
    return {key: decode(item, sentinel=sentinel) for key, item in value.items()}


# ---- failures --------------------------------------------------------------

_BUILTIN_BASES = {"object", "BaseException", "Exception", "TimeoutError", "OSError"}
_CARRIERS: dict[tuple[tuple[str, ...], bool], type[BaseException]] = {}


class RelayedProviderError(Exception):
    """A provider failure raised on the host, standing in for the original in the agent.

    Its class is built per failure so that its MRO carries the original's class
    names (see :func:`decode_exception`); this is the root every one of them shares.
    """


def _chain(exc: BaseException) -> list[BaseException]:
    seen: set[int] = set()
    found: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        found.append(cur)
        cur = cur.__cause__ or cur.__context__
    return found


def encode_exception(exc: BaseException, *, status: int | None) -> dict[str, Any]:
    """The facts the agent's classification and call record read off ``exc``."""
    from raven.providers import call_record
    from raven.providers.first_byte import FirstByteTimeoutError, StreamIdleTimeoutError

    names: list[str] = []
    for link in _chain(exc):
        for klass in type(link).__mro__:
            if klass.__name__ not in names:
                names.append(klass.__name__)
    response = getattr(exc, "response", None)
    data: dict[str, Any] = {
        "message": str(exc),
        "names": names,
        "status": status,
        "body": call_record.exception_body(exc),
        "headers": call_record.keep_headers(getattr(response, "headers", None) if response is not None else None),
        "attrs": {
            attr: value for attr in ("text", "message") if isinstance(value := getattr(exc, attr, None), str) and value
        },
    }
    if isinstance(exc, FirstByteTimeoutError):
        data["kind"] = "first_byte"
        data["timeout"] = {"phase": exc.phase, "budget": exc.budget, "waited": exc.waited}
    elif isinstance(exc, StreamIdleTimeoutError):
        data["kind"] = "stream_idle"
        data["timeout"] = {"idle": getattr(exc, "idle", None)}
    elif isinstance(exc, TimeoutError):
        data["kind"] = "timeout"
    else:
        data["kind"] = "error"
    return data


def _carrier(names: tuple[str, ...], timeout: bool) -> type[BaseException]:
    """A class whose MRO spells ``names``, rooted in :class:`RelayedProviderError`.

    Built from the most general name down, so the most specific -- the original
    class's own -- is the class itself and reads as its name.
    """
    key = (names, timeout)
    cached = _CARRIERS.get(key)
    if cached is not None:
        return cached
    roots: tuple[type, ...] = (RelayedProviderError, TimeoutError) if timeout else (RelayedProviderError,)
    root_names = {klass.__name__ for root in roots for klass in root.__mro__}
    cls: type = type("RelayedTimeoutError", roots, {"__module__": __name__}) if timeout else RelayedProviderError
    for name in reversed([n for n in names if n not in _BUILTIN_BASES and n not in root_names]):
        cls = type(name, (cls,), {"__module__": __name__})
    _CARRIERS[key] = cls
    return cls


def decode_exception(data: dict[str, Any]) -> BaseException:
    """An exception that answers the agent's reads the way the host's original did."""
    from raven.providers.first_byte import FirstByteTimeoutError, StreamIdleTimeoutError

    kind = data.get("kind")
    message = str(data.get("message") or "")
    timeout = data.get("timeout") if isinstance(data.get("timeout"), dict) else {}
    bound: BaseException | None = None
    try:
        if kind == "first_byte":
            bound = FirstByteTimeoutError(
                phase=str(timeout.get("phase") or ""),
                budget=float(timeout.get("budget") or 0.0),
                waited=float(timeout.get("waited") or 0.0),
            )
        elif kind == "stream_idle":
            bound = StreamIdleTimeoutError(idle=float(timeout.get("idle") or 0.0))
    except (TypeError, ValueError):
        bound = None
    if bound is not None:
        # The host's own sentence, not one rebuilt from rounded numbers.
        bound.args = (message,)
        return bound
    names = tuple(str(n) for n in data.get("names") or () if isinstance(n, str))
    exc = _carrier(names, kind in {"timeout", "first_byte", "stream_idle"})(message)
    status = data.get("status")
    if isinstance(status, int):
        exc.status_code = status  # type: ignore[attr-defined]
    body = data.get("body")
    if isinstance(body, str):
        exc.body = body  # type: ignore[attr-defined]
    headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
    exc.response = SimpleNamespace(headers=headers, text=body if isinstance(body, str) else None)  # type: ignore[attr-defined]
    for attr, value in (data.get("attrs") or {}).items():
        if attr in ("text", "message") and isinstance(value, str):
            setattr(exc, attr, value)
    return exc
