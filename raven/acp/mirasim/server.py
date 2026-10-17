"""One Mirasim agent served over ACP, on the Mirasim host already running on this machine.

Mirasim has no ACP surface of its own, but its desktop and phone apps drive its
host over one protocol -- JSON frames on a WebSocket -- and so does this adapter:
it is one more client of the host the user runs (:mod:`.host`), never a host of
its own. Every session of the adapter rides that one connection, and the host
pushes each turn's progress as patches to the session's snapshot, which become
ACP updates as they arrive (:mod:`.stream`). So a reply streams, a tool call
shows when it starts, a question reaches the client when it is asked, and a
cancel is one frame:

===============================  ==============================================
``initialize``                   answered at once; the menus are read behind it
``session/new``                  nothing is sent -- the id is minted here, and
                                 the Mirasim conversation starts with the first
                                 turn
``session/load``                 the record :mod:`.store` kept, or a Mirasim
                                 conversation of this agent that
                                 ``session/list`` offered; its history is
                                 replayed from ``getHistory``
``session/resume``               the same, without the replay
``session/list``                 ``listSessions`` for the directory, this
                                 agent's conversations
``session/prompt``               ``prompt``; the turn's patches stream back as
                                 updates until it ends
``session/cancel``               ``stop`` for the running turn, ``recall`` for
                                 one still queued
``session/close``                the session, dropped from this connection
``session/delete``               the record, and ``deleteSession`` for the
                                 Mirasim conversation behind it
``session/set_config_option``    the model the next turn asks for
``session/set_mode``             the effort the next turn asks for
``_raven/session/steer``         the text queued as a ``prompt``, then
                                 ``steer``-ed into the running turn
===============================  ==============================================

Every turn names its model, effort and route, because a continued Mirasim
conversation otherwise keeps the ones it last ran on, and Raven re-asserts its
own choice on every route into a session. The route is the conversation's --
the platform's quota or the user's own account (see :mod:`.catalog`) -- and the
host takes it from any ``prompt`` frame.

The host is shared: the user's own turns run on it, and a turn can start with
nobody here having asked -- a background job's report, a scheduled wake, a
message typed in the app. A prompt sent while another turn runs is queued by
the host and runs after it, so a turn is followed by its ``taskId``, known from
``accepted`` or, for a queued prompt, from ``outbox.dispatched``; what another
turn does meanwhile is not this prompt's answer. Between prompts, though, a turn
on a session this adapter holds is forwarded as it runs, ended with a usage
update -- how Raven's client reads a turn it did not start
(``raven/acp_client/unprompted.py``). Its questions are left to the app: only a
turn this adapter started is answered from here.

A question a turn stops on -- a tool to approve, or one the agent asks -- goes
out as ``session/request_permission`` or, to a client that declared forms,
``elicitation/create``, and the answer goes back as ``interact``. A client that
takes no forms has the question skipped and named in the reply, and one that
cannot answer has it dismissed, so no question holds a turn past
:data:`HUMAN_ANSWER_TIMEOUT_S`. Approvals follow Mirasim's own settings, not the
host Raven runs on.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import mimetypes
import os
import re
import signal
import sys
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from loguru import logger

from raven.acp.capabilities import ClientCapabilities
from raven.acp.mirasim.catalog import (
    IMAGE_AGENTS,
    MODEL_OPTION,
    STEER_AGENTS,
    WIRE_ROUTES,
    AgentCatalog,
    agent_entry,
    installed_ids,
    read_catalog,
)
from raven.acp.mirasim.host import HostAddress, HostConnection, HostUnavailableError, locate_host
from raven.acp.mirasim.interactions import (
    PERMISSION,
    Reply,
    describe,
    elicitation_request,
    pending,
    permission_request,
    read_elicitation,
    read_permission,
)
from raven.acp.mirasim.store import Binding, BindingStore, store_path
from raven.acp.mirasim.stream import SessionView, TurnStream, is_running, replay
from raven.acp.outbound import ConnectionClosedError, OutboundRequests, RequestFailedError
from raven.acp.protocol import (
    CANCEL_REQUEST_METHOD,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    MENU_UNREAD_META,
    METHOD_NOT_FOUND,
    RESOURCE_NOT_FOUND,
    STEER_CAPABILITY,
    STEER_METHOD,
    error_response,
    negotiated_version,
    notification,
    result_response,
)
from raven.acp.stdio import claim_stdout, open_stdin, read_frames, write_frame
from raven.utils import asyncio_runner

CATALOG_TTL_S = 15 * 60
"""How long the cached menus stand before a new or loaded session reads them again."""

CATALOG_RETRY_S = 30.0
"""How long a read that failed holds off the next one.

Menus a failed read left unread are read again by the first session opened
after it, not when the copy would have expired. And a host that keeps failing
is asked no more often than this."""

MAX_IDLE_SESSIONS = 512
"""Sessions held in memory beyond the ones with a turn running. Raven opens a
session per dispatch that names no instance, so an adapter that lives as long as
Raven does would otherwise grow by one per spawn. An evicted session that had a
turn is still on disk and comes back on ``session/load``."""

DELETED_KEPT = 1024
"""Deleted session ids remembered, so a ``session/load`` racing a delete -- one
that read the record before the delete removed it -- cannot bring the session
back."""

QUERY_TIMEOUT_S = 30.0
"""How long a read -- the catalog, a page of history, the session list -- may take the host."""

ACCEPT_TIMEOUT_S = 60.0
"""How long the host may take to take a ``prompt``: it answers before the turn starts, queued or not."""

CLIS_QUIET_S = 2.0
"""How long ``listClis`` stays quiet before its last row is taken as final.

The host answers it progressively and says nothing when it is done: measured on
0.0.537, eleven ``clis`` frames over 1.9 s, an agent's ``installed`` turning true
in a later one than the first. The first frame alone reads installed agents as
missing."""

CLIS_TIMEOUT_S = 30.0

CANCEL_SETTLE_S = 4.0
"""How long a cancelled turn may take to end before the prompt is answered ``cancelled`` anyway.

Under the five seconds Raven's client gives a cancel. Measured on 0.0.465, a
``stop`` ended the turn in 9 ms and its tools within about five seconds."""

LOST_TURN_S = 60.0
"""How long a turn may go unheard of after the connection drops before it is given up.

The connection is made again in the background and the session subscribed to
anew, which brings its snapshot back at once if the host still runs the turn; a
host that restarted runs it no more."""

STEER_TIMEOUT_S = 15.0
DELETE_TIMEOUT_S = 8.0
"""Under the ten seconds Raven's client gives a ``session/delete``."""

HISTORY_PAGE = 50
"""Turns per ``getHistory`` page. A replay reads every page there is: it has no cap."""

IMAGE_LIMIT_BYTES = 10 * 1024 * 1024
"""The largest image sent inline as an attachment; a larger one goes as its path."""

INITIALIZE_CONNECT_S = 2.0
SHUTDOWN_GRACE_S = 5.0
HUMAN_ANSWER_TIMEOUT_S = 900.0
"""How long a question put to the client may take, the budget raven's own ACP
server gives a person (``HUMAN_ANSWER_TIMEOUT_S`` in ``raven/acp/questions.py``).
Past it the question is dismissed and the turn goes on."""

PERMISSION_METHOD = "session/request_permission"
ELICITATION_METHOD = "elicitation/create"

HOST_VERBS = frozenset({"branch"})
"""The commands the host runs itself rather than passing to the agent, the ones it lists with
``source: host``: a prompt that starts with one never reaches the agent. Measured on 0.0.537,
``/branch`` forks the conversation into a new one."""

_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SESSION_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}:[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")
_INTERRUPTED = "Interrupted by user."


class RefusalError(Exception):
    """A request this adapter answers with a JSON-RPC error rather than a result."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(eq=False)
class Turn:
    """One prompt's turn on the host: from the frame asking for it to the snapshot that ends it."""

    client_ref: str
    session: Session
    ended: asyncio.Future[dict[str, Any] | None]
    """The snapshot the turn ended on; ``None`` when it ended without one -- recalled from the
    queue, given up after a cancel, or lost with the connection."""
    task_id: str | None = None
    """The host's id for the turn; ``None`` until it says, and while the turn is queued."""
    cancelled: bool = False
    lost: str | None = None
    heard_at: float = 0.0
    """``time.monotonic()`` when a frame of this turn last arrived."""
    notes: list[str] = field(default_factory=list)


@dataclass(eq=False)
class Session:
    """One ACP session and what its next turn will ask Mirasim for."""

    id: str
    cwd: str
    mirasim_key: str | None = None
    model: str | None = None
    """The model the client put this session on, or ``None`` for Mirasim's default."""
    mode: str | None = None
    """The effort rung the client put this session on, or ``None`` for Mirasim's default."""
    mcp_names: tuple[str, ...] = ()
    mcp_noted: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    store_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: int = 0
    """Prompts received and not yet answered, the running one included."""
    deleted: bool = False
    turn: Turn | None = None
    view: SessionView = field(default_factory=SessionView)
    stream: TurnStream = field(default_factory=TurnStream)
    resyncing: bool = False
    replies: dict[str, tuple[Reply, int]] = field(default_factory=dict)
    """The answer given to each of the turn's questions, and the connection it was sent on."""
    relays: dict[str, asyncio.Task[None]] = field(default_factory=dict)

    def binding(self) -> Binding:
        return Binding(session_id=self.id, cwd=self.cwd, mirasim_key=self.mirasim_key)


@dataclass(frozen=True)
class PromptInput:
    """A ``session/prompt``'s content as a ``prompt`` frame carries it."""

    text: str
    attachments: tuple[str, ...] = ()
    """Images, as ``data:`` URLs."""
    paths: tuple[str, ...] = ()
    """Local files, which the host names in the prompt for the agent to open."""


class MirasimAgent:
    """The ACP methods for one Mirasim agent, over one connection to the host."""

    def __init__(
        self,
        *,
        agent: str,
        store: BindingStore,
        emit: Callable[[dict[str, Any]], None],
        locate: Callable[[], HostAddress | None] = locate_host,
    ) -> None:
        self.agent = agent
        self.store = store
        self.emit = emit
        self.outbound = OutboundRequests(emit)
        self.client = ClientCapabilities()
        self.host = HostConnection(
            on_frame=self._on_frame,
            locate=locate,
            on_connect=self._resubscribe,
            on_lost=self._on_lost,
            wanted=lambda: any(session.mirasim_key for session in self.sessions.values()),
        )
        self.sessions: OrderedDict[str, Session] = OrderedDict()
        self._by_key: dict[str, Session] = {}
        self._turns: dict[str, Turn] = {}
        self._abandoned: OrderedDict[str, Session | None] = OrderedDict()
        """Prompts given up before the host took them; one it takes later is stopped at once.

        A first prompt also names its session: the conversation the host opens for it
        is that session's, to keep, or to delete when the session was deleted meanwhile.
        """
        self._deleted: OrderedDict[str, None] = OrderedDict()
        self._catalog: AgentCatalog | None = None
        self._catalog_at = 0.0
        self._catalog_failed_at: float | None = None
        self._catalog_lock = asyncio.Lock()
        self._prefetch: asyncio.Task[AgentCatalog] | None = None
        self._commands: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._effort_skipped: set[tuple[str | None, str]] = set()
        self._tasks: set[asyncio.Task[Any]] = set()

    async def handle(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one inbound frame; ``None`` for a notification or a response.

        A response is the client answering a question this adapter put to it,
        and goes to the request that is waiting for it.
        """
        method = frame.get("method")
        if not isinstance(method, str):
            if "id" in frame and ("result" in frame or "error" in frame):
                self.outbound.resolve(frame)
            return None
        is_request = "id" in frame
        request_id = frame.get("id")
        params = frame.get("params")
        params = params if isinstance(params, dict) else {}
        try:
            result = await self._dispatch(method, params, is_request)
        except RefusalError as exc:
            return error_response(request_id, exc.code, exc.message) if is_request else None
        except Exception as exc:  # noqa: BLE001 - one failed request must not end the connection
            logger.opt(exception=True).error("Mirasim adapter: {} failed", method)
            if not is_request:
                return None
            return error_response(request_id, INTERNAL_ERROR, str(exc) or type(exc).__name__)
        return result_response(request_id, result) if is_request else None

    async def _dispatch(self, method: str, params: dict[str, Any], is_request: bool) -> Any:
        handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "initialize": self.initialize,
            "session/new": self.new_session,
            "session/load": self.load_session,
            "session/resume": self.resume_session,
            "session/list": self.list_sessions,
            "session/prompt": self.prompt,
            "session/set_config_option": self.set_config_option,
            "session/set_mode": self.set_mode,
            "session/close": self.close_session,
            "session/delete": self.delete_session,
            STEER_METHOD: self.steer,
        }
        if method == "session/cancel":
            await self.cancel(params)
            return None
        if method in handlers:
            return await handlers[method](params)
        if method == CANCEL_REQUEST_METHOD or not is_request:
            return None
        raise RefusalError(METHOD_NOT_FOUND, f"method not found: {method}")

    # -- the handshake and the menus --------------------------------------------

    async def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        self.client = ClientCapabilities.from_params(params)
        if self._prefetch is None:
            self._prefetch = asyncio.create_task(self.catalog())
            self._prefetch.add_done_callback(_note_prefetch)
            # Only so the host's version can be named: the pool's liveness probe
            # repeats initialize with five seconds, so nothing here may wait long,
            # and a host that is not there is the menus' read to report.
            connecting = asyncio.ensure_future(self.host.connect())
            connecting.add_done_callback(_quiet)
            await asyncio.wait({connecting}, timeout=INITIALIZE_CONNECT_S)
        label = self._catalog.label if self._catalog is not None else self.agent
        capabilities: dict[str, Any] = {
            "loadSession": True,
            "promptCapabilities": {"image": self.agent in IMAGE_AGENTS, "audio": False, "embeddedContext": True},
            "mcpCapabilities": {"http": False, "sse": False},
            "sessionCapabilities": {"list": {}, "resume": {}, "close": {}, "delete": {}},
        }
        if self.agent in STEER_AGENTS:
            capabilities["_meta"] = {STEER_CAPABILITY: True}
        return {
            "protocolVersion": negotiated_version(params.get("protocolVersion")),
            "agentCapabilities": capabilities,
            "agentInfo": {
                "name": f"mirasim-{self.agent}",
                "title": f"{label} (Mirasim)",
                "version": self.host.version or "unknown",
            },
            "authMethods": [],
        }

    async def catalog(self, *, refresh: bool = False, allow_stale: bool = False) -> AgentCatalog:
        """The cached menus, read again when asked to or when older than :data:`CATALOG_TTL_S`.

        A failed re-read keeps the copy in hand: the menus a session was opened
        with are better than refusing the session over a read it did not need.
        Menus left unread count as stale, and no read starts within
        :data:`CATALOG_RETRY_S` of one that failed. ``allow_stale`` takes the
        copy in hand without queueing behind a read another session started: a
        turn waiting there could not settle a cancel in time.
        """
        if allow_stale and not refresh and self._catalog is not None:
            return self._catalog
        async with self._catalog_lock:
            held = self._catalog
            if held is not None:
                now = time.monotonic()
                failed_at = self._catalog_failed_at
                backing_off = failed_at is not None and now - failed_at < CATALOG_RETRY_S
                fresh = not held.menus_unread and now - self._catalog_at < CATALOG_TTL_S
                if backing_off or (not refresh and (fresh or allow_stale)):
                    return held
            try:
                rows = await self._cli_rows(lambda rows: _installed(rows, self.agent))
            except (HostUnavailableError, TimeoutError) as exc:
                self._catalog_failed_at = time.monotonic()
                if held is not None:
                    logger.bind(agent_output=True).warning(
                        "Mirasim adapter: re-reading the agent list failed ({}); keeping the last", exc
                    )
                    return held
                raise RefusalError(INTERNAL_ERROR, f"Mirasim could not list its agents: {_said(exc)}") from exc
            row = agent_entry(rows, self.agent)
            if row is None:
                offered = [r.get("id") for r in rows if isinstance(r, dict)]
                raise RefusalError(INVALID_PARAMS, f"Mirasim has no agent {self.agent!r}; it offers {offered}")
            catalog_doc: dict[str, Any] | None
            try:
                frame: dict[str, Any] = {"type": "getCatalog", "agent": self.agent}
                if refresh:
                    frame["refresh"] = True
                catalog_doc = await self.host.request(
                    frame,
                    lambda f: f.get("type") == "catalog" and f.get("agent") == self.agent,
                    timeout=QUERY_TIMEOUT_S,
                )
            except (HostUnavailableError, TimeoutError) as exc:
                logger.bind(agent_output=True).warning(
                    "Mirasim adapter: no model menu for {} ({})", self.agent, _said(exc)
                )
                self._catalog_failed_at = time.monotonic()
                if held is not None:
                    self._catalog = _relabelled(held, read_catalog(self.agent, row, None))
                    return self._catalog
                catalog_doc = None
            else:
                self._catalog_failed_at = None
            self._catalog = read_catalog(self.agent, row, catalog_doc)
            self._catalog_at = time.monotonic()
            return self._catalog

    async def _cli_rows(self, done: Callable[[list[dict[str, Any]]], bool] | None = None) -> list[dict[str, Any]]:
        return await cli_rows(self.host, done)

    async def _commands_for(self, cwd: str) -> list[dict[str, Any]]:
        """The commands the agent takes in ``cwd``, cached like the menus; empty when the host cannot say."""
        held = self._commands.get(cwd)
        if held is not None and time.monotonic() - held[0] < CATALOG_TTL_S:
            return held[1]
        try:
            answer = await self.host.request(
                {"type": "getCommands", "agent": self.agent, "workdir": cwd},
                lambda f: f.get("type") == "commands" and f.get("agent") == self.agent and f.get("workdir") == cwd,
                timeout=QUERY_TIMEOUT_S,
            )
        except (HostUnavailableError, TimeoutError) as exc:
            logger.debug("Mirasim adapter: no command list for {}: {}", self.agent, _said(exc))
            return []
        items = [item for item in answer.get("items") or () if isinstance(item, dict) and _text(item.get("name"))]
        self._commands[cwd] = (time.monotonic(), items)
        return items

    def _announce_commands(self, session: Session) -> None:
        """Tell the client the agent's commands, once the response that opened the session has gone."""

        async def announce() -> None:
            commands = [
                {"name": _text(item.get("name")), "description": _text(item.get("description"))}
                for item in await self._commands_for(session.cwd)
                if item.get("source") != "host"
            ]
            if commands and not session.deleted:
                self._update(session, {"sessionUpdate": "available_commands_update", "availableCommands": commands})

        self._spawn(announce())

    # -- sessions ---------------------------------------------------------------

    async def new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = _required_text(params, "cwd")
        catalog = await self.catalog()
        if not catalog.installed:
            catalog = await self.catalog(refresh=True)
        if not catalog.installed:
            raise RefusalError(
                INTERNAL_ERROR,
                f"Mirasim reports its {catalog.label} agent as not installed; install it from Mirasim's "
                "Settings -> Agents",
            )
        session = Session(id=str(uuid.uuid4()), cwd=cwd, mcp_names=_mcp_names(params))
        self._remember(session)
        self._announce_commands(session)
        return {"sessionId": session.id, **self._state(session, catalog)}

    async def load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        """Reopen a session and replay its history, the whole of it, before answering."""
        session = await self._reopen(params)
        if session.mirasim_key is not None:
            for update in await self._history(session.mirasim_key):
                self._update(session, update)
        return self._state(session, await self.catalog())

    async def resume_session(self, params: dict[str, Any]) -> dict[str, Any]:
        """Reopen a session without replaying it: the client already has what was said."""
        session = await self._reopen(params)
        return self._state(session, await self.catalog())

    async def _reopen(self, params: dict[str, Any]) -> Session:
        session_id = _required_text(params, "sessionId")
        cwd = _required_text(params, "cwd")
        missing = RefusalError(RESOURCE_NOT_FOUND, f"no Mirasim session {session_id!r} is recorded here")
        if session_id in self._deleted:
            raise missing
        session = self.sessions.get(session_id)
        if session is None:
            binding = await asyncio.to_thread(self.store.get, session_id)
            if binding is None and self._own_key(session_id):
                binding = await self._adopt(session_id, cwd)
            if binding is None or session_id in self._deleted:
                raise missing
            session = Session(id=binding.session_id, cwd=binding.cwd, mirasim_key=binding.mirasim_key)
        if not _same_dir(session.cwd, cwd):
            if session.mirasim_key is not None:
                # Mirasim keeps a conversation in the directory it began in, and
                # an agent continuing it there would edit a tree the caller has
                # left. Refused, so the caller opens a session where it now is.
                raise RefusalError(
                    RESOURCE_NOT_FOUND,
                    f"Mirasim session {session_id!r} works in {session.cwd}, not {cwd}; open a new session there",
                )
            session.cwd = cwd
        session.model = None
        session.mode = None
        session.mcp_names = _mcp_names(params)
        self._remember(session)
        if session.mirasim_key is not None:
            await self._subscribe(session)
        self._announce_commands(session)
        return session

    async def _adopt(self, key: str, cwd: str) -> Binding | None:
        """A conversation of this agent in ``cwd`` that this adapter never recorded -- one begun in Mirasim."""
        for row in await self._listed(cwd):
            if row.get("sessionKey") == key:
                binding = Binding(session_id=key, cwd=_text(row.get("workdir")) or cwd, mirasim_key=key)
                await asyncio.to_thread(self.store.put, binding)
                return binding
        return None

    async def list_sessions(self, params: dict[str, Any]) -> dict[str, Any]:
        """This agent's Mirasim conversations in ``cwd``, newest first -- the ones begun in Mirasim included.

        One this adapter recorded is listed under its ACP id; any other goes by
        its Mirasim key, which ``session/load`` takes as well.
        """
        cwd = params.get("cwd") if isinstance(params.get("cwd"), str) and params.get("cwd") else None
        rows = await self._listed(cwd)
        recorded = await asyncio.to_thread(self.store.by_key)
        sessions: list[dict[str, Any]] = []
        for row in rows:
            key = _text(row.get("sessionKey"))
            info: dict[str, Any] = {"sessionId": recorded.get(key, key), "cwd": _text(row.get("workdir"))}
            if title := _text(row.get("title")) or _text(row.get("preview")):
                info["title"] = title
            if updated := _iso(row.get("updatedAt")):
                info["updatedAt"] = updated
            sessions.append(info)
        return {"sessions": sessions}

    async def _listed(self, cwd: str | None) -> list[dict[str, Any]]:
        request_id = f"raven-list-{uuid.uuid4().hex}"
        frame: dict[str, Any] = {"type": "listSessions", "reqId": request_id}
        if cwd is not None:
            frame["workdir"] = cwd
        else:
            frame["scope"] = "global"
        try:
            answer = await self.host.request(
                frame,
                lambda f: f.get("type") == "sessions" and f.get("reqId") == request_id,
                timeout=QUERY_TIMEOUT_S,
            )
        except (HostUnavailableError, TimeoutError) as exc:
            raise RefusalError(INTERNAL_ERROR, f"Mirasim could not list its sessions: {_said(exc)}") from exc
        return [
            row
            for row in answer.get("sessions") or ()
            if isinstance(row, dict)
            and row.get("agent") == self.agent
            and _text(row.get("sessionKey"))
            and _text(row.get("workdir"))
            and (cwd is None or _same_dir(_text(row.get("workdir")), cwd))
        ]

    async def _history(self, key: str) -> list[dict[str, Any]]:
        """Every turn of conversation ``key``, oldest first, as the updates that replay it.

        ``getHistory`` pages back from the newest turn, ``before`` naming the
        oldest one read, so the pages are read to the first before any is sent.
        """
        pages: list[list[dict[str, Any]]] = []
        before: str | None = None
        while True:
            request_id = f"raven-history-{uuid.uuid4().hex}"
            frame: dict[str, Any] = {
                "type": "getHistory",
                "sessionKey": key,
                "reqId": request_id,
                "limit": HISTORY_PAGE,
            }
            if before is not None:
                frame["before"] = before
            try:
                answer = await self.host.request(
                    frame,
                    lambda f, rid=request_id: f.get("type") == "history" and f.get("reqId") == rid,
                    timeout=QUERY_TIMEOUT_S,
                )
            except (HostUnavailableError, TimeoutError) as exc:
                raise RefusalError(
                    INTERNAL_ERROR, f"Mirasim could not read the session's history: {_said(exc)}"
                ) from exc
            turns = [turn for turn in answer.get("turns") or () if isinstance(turn, dict)]
            pages.append(turns)
            older = answer.get("older")
            before = (_text(turns[0].get("taskId")) or _text(turns[0].get("anchor"))) if turns else ""
            if not before or not isinstance(older, int) or older <= 0:
                break
        return [update for page in reversed(pages) for turn in page for update in replay(turn)]

    async def set_config_option(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._session(params)
        if params.get("configId") != MODEL_OPTION:
            raise RefusalError(
                INVALID_PARAMS, f"unknown config option {params.get('configId')!r}; only {MODEL_OPTION!r}"
            )
        value = params.get("value")
        if not isinstance(value, str) or not value:
            raise RefusalError(INVALID_PARAMS, "the model is a non-empty string")
        catalog = await self.catalog(allow_stale=True)
        if not _offers(catalog, value):
            # Read again before refusing: a model added in Mirasim after these
            # menus were read is one a fresh probe has already offered Raven.
            catalog = await self.catalog(refresh=True)
        if not _offers(catalog, value):
            offered = [entry.id for entry in catalog.models]
            raise RefusalError(INVALID_PARAMS, f"Mirasim's {catalog.label} has no model {value!r}; it offers {offered}")
        session.model = value
        return {"configOptions": catalog.config_options(session.model)}

    async def set_mode(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._session(params)
        mode = _required_text(params, "modeId")
        catalog = await self.catalog(allow_stale=True)
        if not catalog.offers_effort(mode):
            # Read again before refusing, as for a model: menus left unread by a
            # cold start would refuse every rung until the copy expired, and the
            # turns would run on Mirasim's default effort meanwhile.
            catalog = await self.catalog(refresh=True)
        if not catalog.offers_effort(mode):
            raise RefusalError(INVALID_PARAMS, f"{mode!r} is not one of {[rung for rung, _ in catalog.efforts]}")
        session.mode = mode
        return {}

    async def close_session(self, params: dict[str, Any]) -> dict[str, Any]:
        """Drop a session from this connection, ending its turn; the conversation itself stays."""
        session = self.sessions.pop(_required_text(params, "sessionId"), None)
        if session is not None:
            await self._forget(session)
        return {}

    async def delete_session(self, params: dict[str, Any]) -> dict[str, Any]:
        """Forget a session and delete the Mirasim conversation behind it.

        The host deletes without asking anyone -- the delete delegate that would
        ask is never set (measured on 0.0.465 and 0.0.537: ``ok`` at once, and the
        row gone from ``listSessions``) -- so this is where the decision is made.
        """
        session_id = _required_text(params, "sessionId")
        self._deleted[session_id] = None
        while len(self._deleted) > DELETED_KEPT:
            self._deleted.popitem(last=False)
        session = self.sessions.pop(session_id, None)
        key: str | None = None
        if session is not None:
            session.deleted = True
            key = session.mirasim_key
            await self._forget(session)
            async with session.store_lock:
                binding = await asyncio.to_thread(self.store.get, session_id)
                await asyncio.to_thread(self.store.delete, session_id)
        else:
            binding = await asyncio.to_thread(self.store.get, session_id)
            await asyncio.to_thread(self.store.delete, session_id)
        key = key or (binding.mirasim_key if binding is not None else None)
        if key is None and self._own_key(session_id):
            key = session_id
        if key is not None:
            await self._delete_conversation(key)
        return {}

    async def _delete_conversation(self, key: str) -> None:
        client_ref = f"raven-delete-{uuid.uuid4().hex}"
        try:
            answer = await self.host.request(
                {"type": "deleteSession", "sessionKey": key, "clientRef": client_ref},
                lambda f: f.get("type") == "sessionDeleteResult" and f.get("clientRef") == client_ref,
                timeout=DELETE_TIMEOUT_S,
            )
        except (HostUnavailableError, TimeoutError) as exc:
            raise RefusalError(
                INTERNAL_ERROR, f"the session is forgotten, but Mirasim did not delete conversation {key}: {_said(exc)}"
            ) from exc
        if answer.get("ok") is not True:
            said = _text(answer.get("error")) or "it gave no reason"
            raise RefusalError(
                INTERNAL_ERROR, f"the session is forgotten, but Mirasim did not delete conversation {key}: {said}"
            )

    async def _forget(self, session: Session) -> None:
        """Let go of a session: its turn is stopped, and its prompt answered ``cancelled`` without waiting to hear so."""
        if session.turn is not None:
            await self._cancel_turn(session.turn)
            _settle(session.turn.ended, None)
        if session.mirasim_key is not None and self._by_key.get(session.mirasim_key) is session:
            del self._by_key[session.mirasim_key]
        for task in session.relays.values():
            task.cancel()

    # -- turns ------------------------------------------------------------------

    async def prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._session(params)
        content = self._prompt_input(params.get("prompt"))
        session.pending += 1
        try:
            async with session.lock:
                return await self._turn(session, content)
        finally:
            session.pending -= 1

    async def _turn(self, session: Session, content: PromptInput) -> dict[str, Any]:
        catalog = await self.catalog(allow_stale=True)
        notes: list[str] = []
        if session.mcp_names and not session.mcp_noted:
            session.mcp_noted = True
            notes.append(
                f"MCP servers {', '.join(session.mcp_names)} were not handed to the agent: Mirasim takes no servers "
                "per session."
            )
        retried = False
        while True:
            if session.deleted:
                return {"stopReason": "cancelled"}
            turn = Turn(client_ref=f"raven-{uuid.uuid4().hex}", session=session, ended=_future())
            turn.notes = notes
            answer = await self._start(session, turn, self._prompt_frame(session, catalog, content, turn.client_ref))
            if answer is None:
                return await self._finish(session, turn)
            said = " ".join((_text(answer.get("message")) or "the host gave no reason").split())
            if not retried and session.mirasim_key is not None and "deleted" in said.lower():
                # The record outlived the conversation (deleted in Mirasim), and
                # the host refuses to recreate it under its old key. Re-dispatching
                # cannot help -- the next load finds the same record -- so the turn
                # starts a new one here and says so.
                logger.warning(
                    "Mirasim adapter: {} conversation {} was deleted; starting a new one",
                    self.agent,
                    session.mirasim_key,
                )
                self._bind(session, None)
                notes.append(
                    "The Mirasim conversation this session continued was deleted, so this turn ran in a new one "
                    "without the earlier context."
                )
                retried = True
                continue
            raise RefusalError(INTERNAL_ERROR, f"Mirasim did not run the {self.agent} turn: {said}")

    def _prompt_frame(
        self, session: Session, catalog: AgentCatalog, content: PromptInput, client_ref: str
    ) -> dict[str, Any]:
        model = session.model or catalog.default_model
        frame: dict[str, Any] = {"type": "prompt", "clientRef": client_ref, "prompt": content.text}
        if session.mirasim_key is not None:
            frame["sessionKey"] = session.mirasim_key
        else:
            frame["agent"] = self.agent
            frame["workdir"] = session.cwd
        if mirasim_model := catalog.mirasim_model(model):
            frame["model"] = mirasim_model
        if effort := self._effort(session, catalog, model):
            frame["effort"] = effort
        route = catalog.route_for(session.model)
        if route is not None:
            frame["route"] = WIRE_ROUTES[route]
        if content.attachments:
            frame["attachments"] = list(content.attachments)
        if content.paths:
            frame["paths"] = [{"path": path} for path in content.paths]
        return frame

    async def _start(self, session: Session, turn: Turn, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Send the turn's ``prompt``; ``None`` once the host took it (or a cancel gave it up), else its refusal."""
        self._turns[turn.client_ref] = turn
        session.turn = turn
        answer = asyncio.ensure_future(
            self.host.request(
                frame,
                lambda f: f.get("clientRef") == turn.client_ref and f.get("type") in ("accepted", "error", "goalError"),
                timeout=ACCEPT_TIMEOUT_S,
            )
        )
        await asyncio.wait({answer, turn.ended}, return_when=asyncio.FIRST_COMPLETED)
        if not answer.done():
            # A cancel, a close or a delete gave the turn up before the host said it took the prompt.
            answer.cancel()
            self._abandon_ref(turn)
            return None
        try:
            got = answer.result()
        except (HostUnavailableError, TimeoutError) as exc:
            self._drop(turn)
            raise RefusalError(INTERNAL_ERROR, f"Mirasim did not take the {self.agent} turn: {_said(exc)}") from exc
        if got.get("type") != "accepted":
            self._drop(turn)
            return got
        await self._persist(session)
        if turn.cancelled and turn.task_id is None and not turn.ended.done():
            await self._recall(turn)
        return None

    def _abandon_ref(self, turn: Turn) -> None:
        session = turn.session
        self._abandoned[turn.client_ref] = session if session.mirasim_key is None else None
        while len(self._abandoned) > DELETED_KEPT:
            self._abandoned.popitem(last=False)

    async def _finish(self, session: Session, turn: Turn) -> dict[str, Any]:
        """Wait the turn out and answer the prompt for it."""
        try:
            snapshot = await turn.ended
        finally:
            self._drop(turn)
            if turn.task_id is None:
                # Given up before the host said it runs: if a recall did not land, it runs later.
                self._abandon_ref(turn)
        if turn.lost is not None:
            raise RefusalError(INTERNAL_ERROR, f"Mirasim {self.agent} turn: {turn.lost}")
        if snapshot is None:
            self._notes(session, turn.notes)
            return {"stopReason": "cancelled"}
        error = " ".join(_text(snapshot.get("error")).split())
        if turn.cancelled or (error == _INTERRUPTED and snapshot.get("incomplete")):
            stop = "cancelled"
        elif error and not (session.stream.text or session.stream.tools):
            raise RefusalError(INTERNAL_ERROR, f"Mirasim {self.agent} turn failed: {error}")
        else:
            stop = "end_turn"
            if error:
                turn.notes.append(f"The turn ended with an error: {error}")
            elif snapshot.get("incomplete"):
                turn.notes.append("Mirasim reported the turn incomplete.")
        if usage := self._usage(snapshot):
            self._update(session, usage)
        self._notes(session, turn.notes)
        return {"stopReason": stop}

    def _drop(self, turn: Turn) -> None:
        self._turns.pop(turn.client_ref, None)
        session = turn.session
        if session.turn is turn:
            session.turn = None
        for task in session.relays.values():
            task.cancel()
        session.relays.clear()
        session.replies.clear()

    async def cancel(self, params: dict[str, Any]) -> None:
        session = self.sessions.get(params.get("sessionId") or "")
        if session is not None and session.turn is not None:
            await self._cancel_turn(session.turn)

    async def _cancel_turn(self, turn: Turn) -> None:
        """Stop the turn, or take it back from the queue, and answer its prompt within :data:`CANCEL_SETTLE_S`.

        The host's process tree is the user's desktop as much as this turn, so
        nothing is ever killed: the turn is asked to stop, and a turn that does
        not say it stopped in time is answered ``cancelled`` all the same.
        """
        if turn.cancelled:
            return
        turn.cancelled = True
        asyncio.get_running_loop().call_later(CANCEL_SETTLE_S, _settle, turn.ended, None)
        if turn.task_id is not None:
            await self._stop(turn)
        elif turn.session.mirasim_key is not None and turn.client_ref in self._turns:
            # Taken by the host and still queued: the recall drops it before it runs.
            await self._recall(turn)

    async def _stop(self, turn: Turn) -> None:
        key = turn.session.mirasim_key
        if key is None or turn.task_id is None:
            return
        with contextlib.suppress(HostUnavailableError):
            await self.host.send({"type": "stop", "sessionKey": key, "taskId": turn.task_id})

    async def _recall(self, turn: Turn) -> None:
        key = turn.session.mirasim_key
        if key is not None:
            with contextlib.suppress(HostUnavailableError):
                await self.host.send({"type": "recall", "sessionKey": key, "ref": turn.client_ref})
        if turn.task_id is None:
            _settle(turn.ended, None)

    async def steer(self, params: dict[str, Any]) -> dict[str, Any]:
        """Merge text into the session's running turn: queue it as a prompt, then ``steer`` it in.

        The two steps work on every host version measured; the one-frame form
        (``prompt`` with ``steer: true``) is ignored by 0.0.465, which queued the
        text to run as a turn of its own. So does a queued text the turn ends
        before taking, which is why one not taken is recalled -- and one already
        started on its own stopped -- before answering ``no_turn``.
        """
        session = self._session(params)
        text = _required_text(params, "text")
        if self.agent not in STEER_AGENTS:
            raise RefusalError(METHOD_NOT_FOUND, f"Mirasim's {self.agent} takes no text into a running turn")
        turn = session.turn
        key = session.mirasim_key
        if turn is None or turn.task_id is None or turn.ended.done() or key is None:
            return {"status": "no_turn"}
        ref = f"raven-steer-{uuid.uuid4().hex}"

        def outcome(frame: dict[str, Any]) -> bool:
            return frame.get("type") == "outbox" and ref in (_ref(frame.get("steered")), _ref(frame.get("dispatched")))

        catalog = await self.catalog(allow_stale=True)
        model = session.model or catalog.default_model
        frame: dict[str, Any] = {"type": "prompt", "clientRef": ref, "sessionKey": key, "prompt": text}
        if mirasim_model := catalog.mirasim_model(model):
            frame["model"] = mirasim_model
        with self.host.listen(outcome) as outcomes:
            try:
                answer = await self.host.request(
                    frame,
                    lambda f: f.get("clientRef") == ref and f.get("type") in ("accepted", "error", "goalError"),
                    timeout=STEER_TIMEOUT_S,
                )
            except (HostUnavailableError, TimeoutError) as exc:
                logger.warning("Mirasim adapter: a steer could not be queued: {}", _said(exc))
                return {"status": "no_turn"}
            if answer.get("type") != "accepted":
                return {"status": "no_turn"}
            if started := _text(answer.get("taskId")):
                # The turn ended before the text arrived, and it started one of its own.
                await self._stop_task(key, started)
                return {"status": "no_turn"}
            await self.host.send({"type": "steer", "sessionKey": key, "ref": ref})
            got = await _outcome(outcomes.get(), turn.ended, timeout=STEER_TIMEOUT_S)
        if isinstance(got, dict) and _ref(got.get("steered")) == ref:
            return {"status": "injected"}
        if isinstance(got, dict) and (started := _text((got.get("dispatched") or {}).get("taskId"))):
            await self._stop_task(key, started)
        else:
            with contextlib.suppress(HostUnavailableError):
                await self.host.send({"type": "recall", "sessionKey": key, "ref": ref})
        return {"status": "no_turn"}

    async def _stop_task(self, key: str, task_id: str) -> None:
        await self._quiet_send({"type": "stop", "sessionKey": key, "taskId": task_id})

    def _claim(self, session: Session, key: str, task_id: str) -> None:
        """Settle the conversation a given-up first prompt opened, by what became of its session.

        A deleted session's conversation is deleted too; any other session keeps
        it, recorded as a turn the host took would have been -- so a closed one can
        be loaded again, and an open one's next prompt continues it. The given-up
        turn itself is marked shown, so its frames are not forwarded as a turn
        nobody prompted.
        """
        if session.deleted:
            self._spawn(self._discard(key))
            return
        if session.mirasim_key is not None:
            return
        self._bind(session, key)
        if task_id:
            session.stream.start(task_id)
            session.stream.ended = True
        self._spawn(self._persist(session))

    async def _discard(self, key: str) -> None:
        try:
            await self._delete_conversation(key)
        except RefusalError as exc:
            logger.warning("Mirasim adapter: {}", exc.message)

    async def _quiet_send(self, frame: dict[str, Any]) -> None:
        """Send a frame whose loss changes nothing that matters: a stop or recall the host no longer needs."""
        with contextlib.suppress(HostUnavailableError):
            await self.host.send(frame)

    # -- what the host sends ----------------------------------------------------

    def _on_frame(self, frame: dict[str, Any]) -> None:
        """Route one frame from the host. Answers to requests were taken by their listeners already."""
        kind = frame.get("type")
        if kind == "accepted" and (ref := _text(frame.get("clientRef"))) in self._abandoned:
            key, task_id = _text(frame.get("sessionKey")), _text(frame.get("taskId"))
            if key and task_id:
                self._spawn(self._stop_task(key, task_id))
            elif key:
                self._spawn(self._quiet_send({"type": "recall", "sessionKey": key, "ref": ref}))
            owner = self._abandoned[ref]
            if key and owner is not None:
                self._claim(owner, key, task_id)
        elif kind == "accepted":
            turn = self._turns.get(_text(frame.get("clientRef")))
            if turn is not None:
                if key := _text(frame.get("sessionKey")):
                    self._bind(turn.session, key)
                if task_id := _text(frame.get("taskId")):
                    self._dispatched(turn, task_id)
        elif kind in ("snapshot", "session"):
            session = self._by_key.get(_text(frame.get("sessionKey")))
            if session is None:
                return
            if kind == "snapshot":
                session.view.reset(frame.get("snapshot"), frame.get("seq"))
                session.resyncing = False
            elif not session.view.apply(frame):
                if not session.resyncing:
                    session.resyncing = True
                    self._spawn(self._subscribe(session))
                return
            self._observe(session)
        elif kind == "outbox":
            dispatched = frame.get("dispatched")
            turn = self._turns.get(_ref(dispatched))
            if turn is not None and (task_id := _text(dispatched.get("taskId"))):
                self._dispatched(turn, task_id)
            elif _ref(dispatched) in self._abandoned and (task_id := _text(dispatched.get("taskId"))):
                self._spawn(self._stop_task(_text(frame.get("sessionKey")), task_id))
            if frame.get("noRun") is True:
                session = self._by_key.get(_text(frame.get("sessionKey")))
                if session is not None and session.turn is not None and session.turn.task_id is not None:
                    # Asked again after the connection came back, the host runs nothing for
                    # this session: it restarted, and the turn went with it.
                    self._lose(session.turn, "the Mirasim host is no longer running it (it restarted)")

    def _dispatched(self, turn: Turn, task_id: str) -> None:
        turn.task_id = task_id
        turn.heard_at = time.monotonic()
        if turn.cancelled:
            self._spawn(self._stop(turn))
        self._observe(turn.session)

    def _observe(self, session: Session) -> None:
        """Bring the client level with the session's snapshot, as far as anyone is listening for it."""
        snapshot = session.view.snapshot
        task_id = _text(snapshot.get("taskId")) or None
        turn = session.turn
        if turn is not None:
            if turn.task_id is None or task_id != turn.task_id:
                return
            turn.heard_at = time.monotonic()
            for update in session.stream.updates(snapshot):
                self._update(session, update)
            self._relay_questions(session, turn, snapshot)
            if not is_running(snapshot):
                session.stream.ended = True
                _settle(turn.ended, snapshot)
            return
        if task_id is None:
            return
        if session.stream.task_id != task_id and not is_running(snapshot):
            # A turn that had ended before anyone here looked: the state a subscribe brings back.
            session.stream.start(task_id)
            session.stream.ended = True
            return
        was_ended = session.stream.ended and session.stream.task_id == task_id
        for update in session.stream.updates(snapshot):
            self._update(session, update)
        if not is_running(snapshot) and not was_ended:
            session.stream.ended = True
            if usage := self._usage(snapshot):
                self._update(session, usage)

    def _relay_questions(self, session: Session, turn: Turn, snapshot: dict[str, Any]) -> None:
        """Put each new question of the turn to the client; answer again one whose answer the connection lost."""
        for interaction in pending(snapshot):
            prompt_id = interaction["promptId"]
            replied = session.replies.get(prompt_id)
            if replied is not None:
                if replied[1] != self.host.epoch:
                    self._spawn(self._answer(session, prompt_id, replied[0]))
                continue
            if prompt_id not in session.relays:
                session.relays[prompt_id] = self._spawn(self._relay(session, turn, interaction))

    async def _relay(self, session: Session, turn: Turn, interaction: dict[str, Any]) -> None:
        reply = await self._ask(session, interaction, turn.notes)
        session.relays.pop(interaction["promptId"], None)
        if session.turn is turn:
            await self._answer(session, interaction["promptId"], reply)

    async def _answer(self, session: Session, prompt_id: str, reply: Reply) -> None:
        session.replies[prompt_id] = (reply, self.host.epoch)
        frame: dict[str, Any] = {"type": "interact", "promptId": prompt_id, "action": reply.action}
        if reply.value is not None:
            frame["value"] = reply.value
        with contextlib.suppress(HostUnavailableError):
            await self.host.send(frame)

    async def _ask(self, session: Session, interaction: dict[str, Any], notes: list[str]) -> Reply:
        """The client's answer to one question, or the skip or dismissal that stands in for one."""
        asked = describe(interaction)
        try:
            if interaction.get("kind") == PERMISSION:
                params, offered = permission_request(session.id, interaction)
                result = await self.outbound.call(PERMISSION_METHOD, params, timeout=HUMAN_ANSWER_TIMEOUT_S)
                return read_permission(result, offered)
            if self.client.elicitation_form:
                params = elicitation_request(session.id, interaction)
                result = await self.outbound.call(ELICITATION_METHOD, params, timeout=HUMAN_ANSWER_TIMEOUT_S)
                return read_elicitation(interaction, result)
        except (RequestFailedError, ConnectionClosedError, TimeoutError) as exc:
            logger.warning("Mirasim adapter: a question could not be put to the client ({}); dismissing it", exc)
            notes.append(f"The agent asked: {asked}\nIt could not be put to you ({exc}), so it was dismissed.")
            return Reply("cancel")
        notes.append(
            f"The agent asked: {asked}\nThis client takes no forms, so the agent went on without an answer; "
            "answer it in the next prompt to this session if it matters."
        )
        return Reply("skip")

    async def _resubscribe(self) -> None:
        """After a (re)connect: ask for a fresh snapshot of every session that has a conversation."""
        for session in list(self.sessions.values()):
            if session.mirasim_key is not None:
                await self._subscribe(session)

    async def _subscribe(self, session: Session) -> None:
        if session.mirasim_key is None:
            return
        session.view.seq = None
        with contextlib.suppress(HostUnavailableError):
            await self.host.send({"type": "subscribe", "sessionKey": session.mirasim_key})

    def _on_lost(self) -> None:
        lost_at = time.monotonic()
        for turn in list(self._turns.values()):
            asyncio.get_running_loop().call_later(LOST_TURN_S, self._abandon, turn, lost_at)

    def _abandon(self, turn: Turn, lost_at: float) -> None:
        if not turn.ended.done() and turn.heard_at <= lost_at:
            self._lose(
                turn, f"lost the connection to the Mirasim host and heard nothing of the turn for {LOST_TURN_S:.0f}s"
            )

    def _lose(self, turn: Turn, why: str) -> None:
        if not turn.ended.done():
            turn.lost = why
            _settle(turn.ended, None)

    # -- helpers ----------------------------------------------------------------

    def _usage(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """The context the turn ended on, as a usage update: ``used`` of the model's window.

        The window is the snapshot's own when it says, else the catalog's for the
        model the turn ran on, else what ``contextPercent`` implies -- Claude Code's
        snapshots carry the percentage and not the window (measured on 0.0.465).
        """
        usage = snapshot.get("usage")
        if not isinstance(usage, dict):
            return None
        used = usage.get("contextUsedTokens")
        size = usage.get("contextWindowTokens")
        if not _is_count(size) and self._catalog is not None:
            size = self._catalog.context_window(_text(snapshot.get("model")) or None)
        percent = usage.get("contextPercent")
        if not _is_count(size) and _is_count(used) and isinstance(percent, (int, float)) and percent > 0:
            size = round(used * 100 / percent)
        if not _is_count(used) or not _is_count(size) or size <= 0:
            return None
        return {"sessionUpdate": "usage_update", "used": used, "size": size}

    def _notes(self, session: Session, notes: list[str]) -> None:
        if notes:
            text = "".join(f"\n\n[Mirasim] {note}" for note in notes)
            self._update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})

    def _update(self, session: Session, update: dict[str, Any]) -> None:
        self.emit(notification("session/update", {"sessionId": session.id, "update": update}))

    def _prompt_input(self, blocks: Any) -> PromptInput:
        content = read_prompt(blocks, images=self.agent in IMAGE_AGENTS)
        verb = content.text.lstrip().split(maxsplit=1)[0] if content.text.strip() else ""
        if verb.startswith("/") and verb[1:] in HOST_VERBS:
            raise RefusalError(
                INVALID_PARAMS,
                f"a prompt that starts with {verb} would run Mirasim's own command instead of reaching the agent",
            )
        return content

    def _effort(self, session: Session, catalog: AgentCatalog, model: str | None) -> str | None:
        """The rung this turn asks for: the session's own, else Mirasim's default, if ``model`` takes it.

        Mirasim drops a rung the model lacks without a word -- measured, an effort
        ``bogus`` ran the turn with no effort at all -- so a choice that cannot
        apply is left off here and said once, rather than sent and silently ignored.
        """
        rungs = catalog.rungs_for(model)
        if not session.mode:
            return catalog.default_effort if catalog.default_effort in rungs else None
        if session.mode in rungs:
            return session.mode
        if (model, session.mode) not in self._effort_skipped:
            self._effort_skipped.add((model, session.mode))
            logger.warning(
                "Mirasim adapter: {} model {} takes no effort {!r}; it runs on its own", self.agent, model, session.mode
            )
        return None

    def _bind(self, session: Session, key: str | None) -> None:
        if session.mirasim_key is not None and self._by_key.get(session.mirasim_key) is session:
            del self._by_key[session.mirasim_key]
        if key != session.mirasim_key:
            session.view = SessionView()
            session.stream = TurnStream()
        session.mirasim_key = key
        if key is not None and session.id in self.sessions:
            self._by_key[key] = session

    def _own_key(self, value: str) -> bool:
        return bool(_SESSION_KEY.fullmatch(value)) and value.split(":", 1)[0] == self.agent

    async def _persist(self, session: Session) -> None:
        if session.mirasim_key is None:
            return
        async with session.store_lock:
            if session.deleted:
                return
            try:
                await asyncio.to_thread(self.store.put, session.binding())
            except Exception as exc:  # noqa: BLE001 - a turn that ran is not undone by a failed save
                logger.warning("Mirasim adapter: could not record session {}: {}", session.id, exc)

    def _state(self, session: Session, catalog: AgentCatalog) -> dict[str, Any]:
        state: dict[str, Any] = {}
        if options := catalog.config_options(session.model):
            state["configOptions"] = options
        if modes := catalog.modes(session.mode):
            state["modes"] = modes
        # A menu that is empty because the read failed goes out marked as such,
        # so a prober does not record it as a measurement of no models. Raven's
        # own key under `_meta`, the way every extension here travels: a
        # conformant client reads no `_meta` and sees exactly the empty menu it
        # sees today.
        if catalog.menus_unread:
            state.setdefault("_meta", {})["raven"] = {MENU_UNREAD_META: True}
        return state

    def _session(self, params: dict[str, Any]) -> Session:
        session_id = _required_text(params, "sessionId")
        session = self.sessions.get(session_id)
        if session is None:
            raise RefusalError(RESOURCE_NOT_FOUND, f"no open session {session_id!r}; open it with session/load first")
        self.sessions.move_to_end(session_id)
        return session

    def _remember(self, session: Session) -> None:
        self.sessions[session.id] = session
        self.sessions.move_to_end(session.id)
        if session.mirasim_key is not None:
            self._by_key[session.mirasim_key] = session
        idle = [sid for sid, held in self.sessions.items() if not held.pending and held.turn is None]
        for sid in idle[: max(0, len(idle) - MAX_IDLE_SESSIONS)]:
            dropped = self.sessions.pop(sid)
            if dropped.mirasim_key is not None and self._by_key.get(dropped.mirasim_key) is dropped:
                del self._by_key[dropped.mirasim_key]

    def _spawn(self, work: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_note_failure)
        return task

    async def shutdown(self) -> None:
        """Stop every turn this adapter started, as a client that hung up can no longer cancel them.

        Questions still out with the client fail first, so a turn waiting on one
        is not held for a client that is gone. The connection stays up for the
        requests still being answered; :meth:`close` ends it.
        """
        self.outbound.close()
        turns = list(self._turns.values())
        await asyncio.gather(*(self._cancel_turn(turn) for turn in turns), return_exceptions=True)

    async def close(self) -> None:
        """End the background work and the connection to the host."""
        if self._prefetch is not None and not self._prefetch.done():
            self._prefetch.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.host.close()


def _future() -> asyncio.Future[dict[str, Any] | None]:
    return asyncio.get_running_loop().create_future()


def _settle(future: asyncio.Future[Any], value: Any) -> None:
    if not future.done():
        future.set_result(value)


async def _outcome(got: Awaitable[Any], ended: asyncio.Future[Any], *, timeout: float) -> Any:
    """What ``got`` brings, or ``None`` when the turn ``ended`` first or ``timeout`` passes.

    ``ended`` is the turn's own and is never cancelled here; only the wait for ``got`` is.
    """
    waiting = asyncio.ensure_future(got)
    try:
        await asyncio.wait({waiting, ended}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        return waiting.result() if waiting.done() and not waiting.cancelled() else None
    finally:
        waiting.cancel()


def _note_prefetch(task: asyncio.Task[AgentCatalog]) -> None:
    """Retrieve a failed background read, which a session that never opens would leave unread."""
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.warning("Mirasim adapter: reading the agent's menus failed: {}", exc)


def _note_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.opt(exception=exc).warning("Mirasim adapter: background work failed")


def _quiet(task: asyncio.Future[Any]) -> None:
    """Retrieve an outcome nobody waits for, so a failure is not reported as never retrieved."""
    if not task.cancelled():
        task.exception()


async def cli_rows(
    host: HostConnection, done: Callable[[list[dict[str, Any]]], bool] | None = None
) -> list[dict[str, Any]]:
    """The host's ``listClis`` rows once they settle, or as soon as ``done`` is satisfied by them."""
    rows: list[dict[str, Any]] | None = None
    with host.listen(lambda frame: frame.get("type") == "clis") as frames:
        await host.send({"type": "listClis"})
        deadline = time.monotonic() + CLIS_TIMEOUT_S
        while (left := deadline - time.monotonic()) > 0:
            try:
                got = await asyncio.wait_for(frames.get(), min(CLIS_QUIET_S, left))
            except TimeoutError:
                if rows is not None:
                    break
                continue
            if isinstance(got, BaseException):
                raise got
            rows = [row for row in got.get("clis") or () if isinstance(row, dict)]
            if done is not None and done(rows):
                break
    if rows is None:
        raise TimeoutError(f"the host listed no agents within {CLIS_TIMEOUT_S:.0f}s")
    return rows


def _relabelled(held: AgentCatalog, listed: AgentCatalog) -> AgentCatalog:
    """The menus in hand, with what the agent list just said of the agent: a new session refreshes to see an install."""
    if (listed.installed, listed.label) == (held.installed, held.label):
        return held
    return replace(held, installed=listed.installed, label=listed.label)


def _installed(rows: list[dict[str, Any]], agent: str) -> bool:
    return agent in installed_ids(rows)


def _offers(catalog: AgentCatalog, model: str) -> bool:
    return catalog.offers_model(model) or model == catalog.default_model


def _same_dir(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _required_text(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise RefusalError(INVALID_PARAMS, f"{key} is a required non-empty string")
    return value


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _ref(value: Any) -> str:
    return _text(value.get("ref")) if isinstance(value, dict) else ""


def _said(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _iso(epoch_ms: Any) -> str | None:
    if not isinstance(epoch_ms, (int, float)) or isinstance(epoch_ms, bool) or epoch_ms <= 0:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _mcp_names(params: dict[str, Any]) -> tuple[str, ...]:
    servers = params.get("mcpServers")
    if not isinstance(servers, list):
        return ()
    return tuple(str(server.get("name") or "?") for server in servers if isinstance(server, dict))


def read_prompt(blocks: Any, *, images: bool) -> PromptInput:
    """A ``session/prompt``'s content, as the text, images and files of a ``prompt`` frame.

    An image goes inline as a ``data:`` URL -- the way Mirasim's own composer
    attaches one, and the only way the agent sees it rather than its name -- for
    an agent measured seeing one (``images``). Raven's dispatches carry images as
    ``resource_link`` blocks to local files, so those are read here. Any other
    local file goes as a path the host names in the prompt for the agent to
    open; a link to anything else stays in the text as its address.
    """
    if not isinstance(blocks, list) or not blocks:
        raise RefusalError(INVALID_PARAMS, "prompt is a non-empty list of content blocks")
    texts: list[str] = []
    attachments: list[str] = []
    paths: list[str] = []
    for block in blocks:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text":
            texts.append(_raw(block.get("text")))
        elif kind == "image":
            if not images:
                raise RefusalError(
                    INVALID_PARAMS,
                    "this agent takes no image content (promptCapabilities.image is false); "
                    "send the picture as a resource_link to its file, which goes as a path",
                )
            if not isinstance(block.get("data"), str) or _raw(block.get("mimeType")) not in _IMAGE_TYPES:
                raise RefusalError(
                    INVALID_PARAMS, f"an image block carries base64 data and one of {', '.join(sorted(_IMAGE_TYPES))}"
                )
            attachments.append(f"data:{block['mimeType']};base64,{block['data']}")
        elif kind == "resource_link":
            uri = _raw(block.get("uri"))
            path = _local_file(uri)
            if path is None:
                texts.append(f"\n[{_raw(block.get('name')) or 'link'}]({uri})\n")
            elif images and (inline := _inline_image(path, _raw(block.get("mimeType")))) is not None:
                attachments.append(inline)
            else:
                paths.append(path)
        elif kind == "resource" and isinstance(block.get("resource"), dict) and "text" in block["resource"]:
            resource = block["resource"]
            texts.append(f"\n[{_raw(resource.get('uri')) or 'resource'}]\n{_raw(resource.get('text'))}\n")
        else:
            raise RefusalError(
                INVALID_PARAMS, f"content block {kind!r} is not supported; send text, an image or a resource_link"
            )
    text = "".join(texts).strip()
    if not text and (attachments or paths):
        text = "See the attached files."
    if not text:
        raise RefusalError(INVALID_PARAMS, "the prompt carries no text")
    return PromptInput(text=text, attachments=tuple(attachments), paths=tuple(paths))


def _inline_image(path: str, mime: str) -> str | None:
    """``path`` as a ``data:`` URL when it is an image small enough to send inline, else ``None``."""
    mime = mime or (mimetypes.guess_type(path)[0] or "")
    if mime not in _IMAGE_TYPES:
        return None
    try:
        if os.path.getsize(path) > IMAGE_LIMIT_BYTES:
            return None
        data = Path(path).read_bytes()
    except OSError:
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _raw(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _local_file(uri: str) -> str | None:
    try:
        parsed = urlparse(uri)
    except ValueError:
        return None
    if parsed.scheme != "file":
        return None
    host = unquote(parsed.netloc)
    if any(char in host for char in "/\\:@") or any(ord(char) < 32 or ord(char) == 127 for char in host):
        return None
    # Keep UNC authorities separate from the converter's localhost-drive handling.
    try:
        native = url2pathname(parsed.path)
    except (OSError, ValueError):
        return None
    if host:
        if sys.platform == "win32":
            if native.startswith("\\") and not native.startswith("\\\\"):
                native = f"\\\\{host}{native}"
            elif host.casefold() != "localhost" or re.match(r"^[A-Za-z]:\\", native) is None:
                return None
        elif host.casefold() != "localhost":
            return None
    path = Path(native)
    return str(path) if path.is_file() else None


async def serve(
    reader: asyncio.StreamReader,
    out: BinaryIO,
    *,
    agent: str,
    store: BindingStore,
    locate: Callable[[], HostAddress | None] = locate_host,
) -> None:
    """Serve ACP on one reader/writer pair until the client closes stdin."""

    def emit(frame: dict[str, Any]) -> None:
        try:
            write_frame(out, frame)
        except (BrokenPipeError, ValueError, OSError) as exc:
            logger.warning("Mirasim adapter: the client is gone ({}); dropping a frame", exc)

    server = MirasimAgent(agent=agent, store=store, emit=emit, locate=locate)
    tasks: set[asyncio.Task[None]] = set()

    async def answer(frame: dict[str, Any]) -> None:
        response = await server.handle(frame)
        if response is not None:
            emit(response)

    async for frame in read_frames(reader, emit):
        task = asyncio.create_task(answer(frame))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    await server.shutdown()
    pending_tasks = [task for task in tasks if not task.done()]
    if pending_tasks:
        _, still = await asyncio.wait(pending_tasks, timeout=SHUTDOWN_GRACE_S)
        for task in still:
            task.cancel()
        await asyncio.gather(*still, return_exceptions=True)
    await server.close()


async def _print_installed(locate: Callable[[], HostAddress | None] = locate_host) -> int:
    """``--installed``: the agents the running host has installed, for a caller deciding which are worth a handshake."""
    host = HostConnection(on_frame=lambda frame: None, locate=locate)
    try:
        rows = await cli_rows(host)
    except (HostUnavailableError, TimeoutError) as exc:
        print(f"Mirasim could not list its agents: {_said(exc)}", file=sys.stderr)
        return 1
    finally:
        await host.close()
    print(json.dumps(installed_ids(rows)))
    return 0


async def _main(args: argparse.Namespace, out: BinaryIO) -> None:
    store = BindingStore(store_path(args.agent))
    async with open_stdin() as reader:
        # SIGTERM ends the session the way a closed stdin does, so the turns
        # still running are stopped instead of being left to run unwatched.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, reader.feed_eof)
        await serve(reader, out, agent=args.agent, store=store)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m raven.acp.mirasim",
        description="Serve one Mirasim agent over ACP on stdio, on the Mirasim host running on this machine.",
    )
    parser.add_argument("--agent", help="the Mirasim agent id, as Mirasim lists it")
    parser.add_argument("--verbose", action="store_true", help="log progress to stderr, not only problems")
    parser.add_argument(
        "--installed", action="store_true", help="print the agents Mirasim has installed, as a JSON list, and exit"
    )
    args = parser.parse_args(argv)
    if args.installed:
        return asyncio_runner.run(_print_installed())
    if args.agent is None:
        parser.error("--agent is required")
    if not _AGENT_ID.match(args.agent):
        parser.error(f"--agent {args.agent!r} is not a Mirasim agent id")
    logger.remove()
    # Raven journals this stream, so annotated frames would publish every local
    # a failure carries -- the prompt among them (`raven/cli/_log_file.py`).
    logger.add(sys.stderr, level="INFO" if args.verbose else "WARNING", backtrace=False, diagnose=False)
    with claim_stdout() as out:
        asyncio_runner.run(_main(args, out))
    return 0


__all__ = [
    "CATALOG_TTL_S",
    "MAX_IDLE_SESSIONS",
    "MirasimAgent",
    "PromptInput",
    "RefusalError",
    "Session",
    "Turn",
    "cli_rows",
    "main",
    "read_prompt",
    "serve",
]
