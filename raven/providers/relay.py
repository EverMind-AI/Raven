"""A Raven agent's LiteLLM calls, made by the host that launched it.

Every Raven process that talks to a model through LiteLLM loads LiteLLM, its
OpenAI SDK and its catalogue -- measured at about 120 MiB of a product agent's
240. A host that launches several Raven agents already holds all of that once,
so it can make their calls for them: the host serves a relay on a local socket
and names it in the launch environment (``RAVEN_LLM_RELAY``), and the agent
reads it here.

What crosses is deliberately narrow. The agent keeps everything a call is
wrapped in -- request assembly, the retry ladder, classification, usage
records, traces -- and hands over only the single attempt:
:class:`RelayProvider` stands where a :class:`LiteLLMProvider` would, answers the
same questions from the same code (:mod:`raven.providers.litellm_shape`), and
sends ``chat`` and ``chat_stream`` to a :class:`LiteLLMProvider` the host builds
from the very arguments this one was built with (:mod:`raven.providers.relay_provider`).
The catalogue questions that need LiteLLM's tables -- a context window, a price,
an output ceiling -- cross the same way, through :func:`relayed`.

This module is the transport and the offer, and imports nothing of the provider
stack: the catalogue modules that use :func:`relayed` sit under that stack, and
an import back up from here would tie them into one cycle.

Nothing here is required. An agent started without the variable, or one whose
host stops answering, loads LiteLLM and does it all itself, as before.
"""

from __future__ import annotations

import asyncio
import functools
import os
import socket
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, TypeVar

from loguru import logger

from raven.providers import relay_wire

RELAY_ENV = "RAVEN_LLM_RELAY"
"""Where the host's relay listens: ``unix:<path>`` or ``tcp:<host>:<port>``."""
RELAY_TOKEN_ENV = "RAVEN_LLM_RELAY_TOKEN"  # noqa: S105 - the variable's name, not its value
"""The secret every request carries, so only the agents the host launched are served."""

#: The modules whose :func:`relayed` functions a host will run for an agent.
#: The host imports a module by name from a request, so only these are honoured.
RELAYED_MODULES = (
    "raven.providers.rates",
    "raven.providers.common_models",
    "raven.providers.capabilities",
    "raven.providers.anthropic_messages_provider",
)

#: How long a catalogue question waits for the host. Generous because the
#: host's first answer may wait out its own LiteLLM import.
_ASK_TIMEOUT_S = 60.0
#: How long opening the socket may take before the host is judged gone.
_CONNECT_TIMEOUT_S = 10.0
_ANSWER_CACHE_SIZE = 4096

_UNREAD = object()
_target: Any = _UNREAD
_target_lock = threading.Lock()
_given_up = False
_RELAYED: dict[str, Callable[..., Any]] = {}
_ANSWERS: list[OrderedDict[Any, Any]] = []

F = TypeVar("F", bound=Callable[..., Any])


class RelayUnavailableError(ConnectionError):
    """The host's relay could not be reached; the caller does the work itself.

    ``connecting`` tells the two ways apart: a host nobody answers for is gone
    for the rest of this process, while a question that timed out on a host that
    did answer leaves the next question to it.
    """

    def __init__(self, message: str, *, connecting: bool = False) -> None:
        super().__init__(message)
        self.connecting = connecting


class RelayFaultError(RuntimeError):
    """The host refused a request as malformed: a bug on one side, not a provider failure."""


def target() -> tuple[str, str] | None:
    """``(address, token)`` from the launch environment, or ``None`` when there is no relay.

    Read once and taken out of ``os.environ`` on the way: every process this
    agent starts -- a shell command, a nested Raven -- inherits its environment,
    and a relay is an offer to the agent the host launched, not to everything
    downstream of it.
    """
    global _target
    with _target_lock:
        if _target is _UNREAD:
            address = os.environ.pop(RELAY_ENV, "").strip()
            token = os.environ.pop(RELAY_TOKEN_ENV, "").strip()
            _target = (address, token) if address and token else None
        return _target


def active() -> bool:
    """Whether this process sends its LiteLLM work to a host right now."""
    return not _given_up and target() is not None


def give_up(reason: BaseException) -> None:
    """Stop asking a host that cannot be reached; this process does the work from now on."""
    global _given_up
    if not _given_up:
        _given_up = True
        logger.warning("llm relay: the host cannot be reached ({}); loading LiteLLM in this process", reason)


def _reset_for_tests() -> None:
    global _target, _given_up
    with _target_lock:
        _target = _UNREAD
        _given_up = False
    for answers in _ANSWERS:
        answers.clear()


def _split(address: str) -> tuple[str, str | tuple[str, int]]:
    kind, _, rest = address.partition(":")
    if kind == "unix" and rest:
        return "unix", rest
    if kind == "tcp":
        host, _, port = rest.rpartition(":")
        if host and port.isdigit():
            return "tcp", (host, int(port))
    raise RelayFaultError(f"unreadable relay address {address!r}")


def request_frame(op: str, **fields: Any) -> dict[str, Any]:
    found = target()
    if found is None:
        raise RelayUnavailableError("no relay was offered to this process")
    return {"token": found[1], "op": op, **fields}


def _connect_sync(timeout: float) -> socket.socket:
    found = target()
    if found is None:
        raise RelayUnavailableError("no relay was offered to this process")
    kind, where = _split(found[0])
    try:
        if kind == "unix":
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(_CONNECT_TIMEOUT_S)
            sock.connect(where)
        else:
            sock = socket.create_connection(where, timeout=_CONNECT_TIMEOUT_S)
    except OSError as exc:
        raise RelayUnavailableError(str(exc), connecting=True) from exc
    sock.settimeout(timeout)
    return sock


async def connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    found = target()
    if found is None:
        raise RelayUnavailableError("no relay was offered to this process")
    kind, where = _split(found[0])
    limit = relay_wire.MAX_FRAME_BYTES
    try:
        if kind == "unix":
            opening = asyncio.open_unix_connection(where, limit=limit)
        else:
            host, port = where  # type: ignore[misc]
            opening = asyncio.open_connection(host, port, limit=limit)
        return await asyncio.wait_for(opening, timeout=_CONNECT_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError) as exc:
        raise RelayUnavailableError(str(exc), connecting=True) from exc


def read_answer(reply: dict[str, Any] | None, *, sentinel: object | None = None) -> Any:
    if reply is None:
        raise relay_wire.FrameError("the host closed the relay connection without answering")
    if "error" in reply:
        raise relay_wire.decode_exception(reply["error"])
    if "fault" in reply:
        raise RelayFaultError(str(reply["fault"]))
    return relay_wire.decode(reply.get("result"), sentinel=sentinel)


def ask(op: str, *, timeout: float = _ASK_TIMEOUT_S, **fields: Any) -> Any:
    """One blocking question to the host. Small, local and rare enough to make from the event loop."""
    sock = _connect_sync(timeout)
    try:
        relay_wire.send_frame(sock, request_frame(op, **fields))
        reply = relay_wire.recv_frame(sock)
    except (OSError, relay_wire.FrameError) as exc:
        raise RelayUnavailableError(str(exc)) from exc
    finally:
        sock.close()
    return read_answer(reply)


def relayed(
    *, keep: Callable[[Any], bool] = lambda value: value is not None, shape: Callable[[Any], Any] | None = None
):
    """Run the decorated function on the host while this process has a relay.

    For a function whose answer comes out of LiteLLM's tables. The host runs the
    same function -- the undecorated original, registered here by name -- so the
    answer is the one this process would have computed with LiteLLM loaded.

    Answers ``keep`` accepts are remembered per arguments: a table row does not
    change while a process runs, and these are asked hundreds of times a turn. An
    unknown (``None`` by default) is asked again, since the host may learn it.
    ``shape`` restores what JSON flattens (a tuple travels as a list). A host
    that cannot be reached leaves the call to this process.
    """

    def wrap(fn: F) -> F:
        name = f"{fn.__module__}:{fn.__qualname__}"
        _RELAYED[name] = fn
        answers: OrderedDict[Any, Any] = OrderedDict()
        _ANSWERS.append(answers)
        lock = threading.Lock()

        @functools.wraps(fn)
        def call(*args: Any, **kwargs: Any) -> Any:
            if not active():
                return fn(*args, **kwargs)
            try:
                key: Any = (args, tuple(sorted(kwargs.items())))
                hash(key)
            except TypeError:
                key = None
            if key is not None:
                with lock:
                    if key in answers:
                        answers.move_to_end(key)
                        return answers[key]
            try:
                value = ask("call", fn=name, args=relay_wire.encode(list(args)), kwargs=relay_wire.encode(kwargs))
            except RelayUnavailableError as exc:
                if exc.connecting:
                    give_up(exc)
                return fn(*args, **kwargs)
            if shape is not None:
                value = shape(value)
            if key is not None and keep(value):
                with lock:
                    answers[key] = value
                    while len(answers) > _ANSWER_CACHE_SIZE:
                        answers.popitem(last=False)
            return value

        return call  # type: ignore[return-value]

    return wrap


def run_relayed(name: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    """The host's half of :func:`relayed`: run a registered function locally, by name."""
    import importlib

    module, _, _qualname = name.partition(":")
    if module not in RELAYED_MODULES:
        raise RelayFaultError(f"{module!r} is not a relayed module")
    importlib.import_module(module)
    fn = _RELAYED.get(name)
    if fn is None:
        raise RelayFaultError(f"{name!r} is not a relayed function")
    return fn(*args, **kwargs)
