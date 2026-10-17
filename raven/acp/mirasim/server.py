"""One Mirasim agent served over ACP: its sessions, their turns, and the frame loop.

Mirasim has no ACP surface of its own. Its scriptable surface is ``ui-cli``,
which runs one turn per command and blocks until the turn settles, so each ACP
method below maps onto at most one such command:

===============================  ==============================================
``initialize``                   ``mirasim version``; the agent's menus are read
                                 in the background, and a repeated initialize
                                 answers from what is in hand
``session/new``                  nothing is run -- the ACP id is minted here,
                                 the Mirasim conversation starts with the first
                                 turn
``session/load``                 the record :mod:`.store` kept; no replay
``session/prompt``               ``ui-cli prompt``, continuing the record's
                                 conversation, after ``ui-cli session route``
                                 when the turn asks for another route; what
                                 the turn stops to ask is put to the client
                                 and answered with ``ui-cli answer``
``session/set_config_option``    the model the next turn asks for
``session/set_mode``             the effort the next turn asks for
``session/cancel``               the running turn's process tree, ended
``session/delete``               the record, forgotten
===============================  ==============================================

A repeated ``initialize`` is how Raven's pool asks a long-silent connection
whether anyone is home, with a five-second budget (``_PROBE_TIMEOUT_S`` in
``raven/acp_client/pool.py``); reading the menus takes longer than that, which
is why only the first initialize starts the read and none waits for it.

``session/load`` replays nothing: the spec has the agent stream the history
back, and doing that from Mirasim's ``history`` would start a server on every
load -- Raven loads its instance's session before every dispatch, and discards
the replay it gets. It also drops the model and effort the session was put on:
Raven re-asserts its own choice on every route in, and a session it puts on no
model runs on Mirasim's default for the agent. Every turn therefore names its
model and effort, because a resumed Mirasim session otherwise keeps the ones it
last ran on.

A conversation's route -- the platform's quota or the user's own account --
is Mirasim's, set once rather than per turn (see :mod:`.catalog`). A new
conversation is started on the route its model choice names; one that continues
is switched with ``ui-cli session route`` only before a turn whose choice names
another, which the record in :mod:`.store` is what tells. Every server this
starts runs in one directory of Raven's (:func:`.store.server_workdir`), because
Mirasim keeps those routes in an index per server directory.

A reply arrives whole, as one message, because ``ui-cli`` reports a turn only
once it settles; there is no partial text to stream.

A turn that stops to ask -- a question, or a tool it wants approved -- waits
for an answer, and ``ui-cli prompt`` waits with it: measured, a turn that
asked held its command until the command's own timeout. So while a turn runs,
the private server running it is watched for what it asks, through the port
that server announces (:meth:`MirasimAgent._watch`), and each question is put
to the client and its answer given back (:mod:`.interactions`). A tool
approval goes out as ``session/request_permission``, which every client
answers; anything else as ``elicitation/create`` to a client that declared
forms. A client that takes no forms has the question skipped and named in the
reply, and one that cannot answer has it dismissed, so no question holds a
turn past :data:`HUMAN_ANSWER_TIMEOUT_S`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from loguru import logger

from raven.acp.capabilities import ClientCapabilities
from raven.acp.mirasim.catalog import (
    MODEL_OPTION,
    OWN_ROUTE,
    PLATFORM_ROUTE,
    AgentCatalog,
    agent_entry,
    read_catalog,
)
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
from raven.acp.mirasim.store import Binding, BindingStore, server_workdir, store_path
from raven.acp.mirasim.uicli import (
    EXECUTABLE,
    INLINE_TEXT_LIMIT,
    PROMPT_FILE_NOTE,
    QUERY_TIMEOUT_S,
    Invocation,
    Outcome,
    UiCli,
    UiCliError,
    answer_args,
    live_session,
    prompt_args,
    route_args,
    session_gone,
    sessions_args,
    snapshot_args,
    snapshot_of,
)
from raven.acp.outbound import ConnectionClosedError, OutboundRequests, RequestFailedError
from raven.acp.protocol import (
    CANCEL_REQUEST_METHOD,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    MENU_UNREAD_META,
    METHOD_NOT_FOUND,
    RESOURCE_NOT_FOUND,
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
after it, not when the copy would have expired. And a Mirasim that keeps failing
is asked no more often than this, because each read can take ui-cli's whole
timeout with every session waiting behind it."""

MAX_IDLE_SESSIONS = 512
"""Sessions held in memory beyond the ones with a turn running. Raven opens a
session per dispatch that names no instance, so an adapter that lives as long as
Raven does would otherwise grow by one per spawn. An evicted session that had a
turn is still on disk and comes back on ``session/load``."""

REPLY_CHUNK_CHARS = 64 * 1024

SHUTDOWN_GRACE_S = 5.0

WATCH_PAUSES_S = (1.0, 2.0, 4.0, 8.0, 15.0)
"""The pauses between looks at a running turn's server for what it asks.

Short at first and again after each answer, because a question tends to come
early or straight after another; then every fifteen seconds. Each look is a
``ui-cli`` command of its own, measured at two to four seconds and nearly
three of CPU, which is what the pauses are spent saving."""

LOOK_SECONDS = 5
"""How long a look waits on the turn's server for the one frame it asks for."""

LOOK_TIMEOUT_S = 45.0

ANNOUNCE_WARN_S = 120.0
"""How long a turn's server may take to say its port before that is logged.
Measured, a server booted in 1.8 s on an idle machine and 22.4 s on a loaded
one; until it says its port, nothing the turn asks can be answered."""

HUMAN_ANSWER_TIMEOUT_S = 900.0
"""How long a question put to the client may take, the budget raven's own ACP
server gives a person (``HUMAN_ANSWER_TIMEOUT_S`` in ``raven/acp/questions.py``).
Past it the question is dismissed and the turn goes on."""

PERMISSION_METHOD = "session/request_permission"
ELICITATION_METHOD = "elicitation/create"

_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class RefusalError(Exception):
    """A request this adapter answers with a JSON-RPC error rather than a result."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(eq=False)
class Session:
    """One ACP session and what its next turn will ask Mirasim for."""

    id: str
    cwd: str
    mirasim_key: str | None = None
    route: str | None = None
    """The route the conversation was last put on, as the record says; ``None`` when not known."""
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
    turn_cancelled: bool = False
    deleted: bool = False
    running: Invocation | None = None

    def binding(self) -> Binding:
        return Binding(session_id=self.id, cwd=self.cwd, mirasim_key=self.mirasim_key, route=self.route)


class MirasimAgent:
    """The ACP methods for one Mirasim agent, over one ``ui-cli``."""

    def __init__(
        self,
        *,
        agent: str,
        cli: UiCli,
        store: BindingStore,
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        self.agent = agent
        self.cli = cli
        self.store = store
        self.emit = emit
        self.outbound = OutboundRequests(emit)
        self.client = ClientCapabilities()
        self.sessions: OrderedDict[str, Session] = OrderedDict()
        self._catalog: AgentCatalog | None = None
        self._catalog_at = 0.0
        self._catalog_failed_at: float | None = None
        self._catalog_lock = asyncio.Lock()
        self._prefetch: asyncio.Task[AgentCatalog] | None = None
        self._version: str | None = None
        self._effort_skipped: set[tuple[str | None, str]] = set()

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
        if method == "initialize":
            return await self.initialize(params)
        if method == "session/new":
            return await self.new_session(params)
        if method == "session/load":
            return await self.load_session(params)
        if method == "session/prompt":
            return await self.prompt(params)
        if method == "session/set_config_option":
            return await self.set_config_option(params)
        if method == "session/set_mode":
            return await self.set_mode(params)
        if method == "session/delete":
            return await self.delete_session(params)
        if method == "session/cancel":
            await self.cancel(params)
            return None
        if method == CANCEL_REQUEST_METHOD or not is_request:
            return None
        raise RefusalError(METHOD_NOT_FOUND, f"method not found: {method}")

    async def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        self.client = ClientCapabilities.from_params(params)
        if self._prefetch is None:
            self._prefetch = asyncio.create_task(self.catalog())
            self._prefetch.add_done_callback(_note_prefetch)
        if self._version is None:
            try:
                self._version = await self.cli.version()
            except UiCliError as exc:
                logger.bind(agent_output=True).warning("Mirasim adapter: {}", exc)
                self._version = ""
        label = self._catalog.label if self._catalog is not None else self.agent
        return {
            "protocolVersion": negotiated_version(params.get("protocolVersion")),
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
                "mcpCapabilities": {"http": False, "sse": False},
                "sessionCapabilities": {"delete": {}},
            },
            "agentInfo": {
                "name": f"mirasim-{self.agent}",
                "title": f"{label} (Mirasim)",
                "version": self._version or "unknown",
            },
            "authMethods": [],
        }

    async def catalog(self, *, refresh: bool = False, allow_stale: bool = False) -> AgentCatalog:
        """The cached menus, read again when asked to or when older than :data:`CATALOG_TTL_S`.

        A failed re-read keeps the copy in hand: the menus a session was opened
        with are better than refusing the session over a read it did not need.
        Menus left unread count as stale, and no read starts within
        :data:`CATALOG_RETRY_S` of one that failed.
        """
        async with self._catalog_lock:
            held = self._catalog
            if held is not None:
                now = time.monotonic()
                failed_at = self._catalog_failed_at
                backing_off = failed_at is not None and now - failed_at < CATALOG_RETRY_S
                fresh = not held.menus_unread and now - self._catalog_at < CATALOG_TTL_S
                if backing_off or (not refresh and (fresh or allow_stale)):
                    return held
            agents_doc, catalog_doc = await asyncio.gather(
                self.cli.query(["agents"]),
                self.cli.query(["catalog", f"--agent={self.agent}"]),
                return_exceptions=True,
            )
            # From when the read ended: a cold start fails on ui-cli's timeout,
            # which is longer than the pause.
            ended = time.monotonic()
            if isinstance(agents_doc, BaseException):
                self._catalog_failed_at = ended
                if self._catalog is not None:
                    logger.bind(agent_output=True).warning(
                        "Mirasim adapter: re-reading the agent list failed ({}); keeping the last", agents_doc
                    )
                    return self._catalog
                raise RefusalError(INTERNAL_ERROR, f"Mirasim could not list its agents: {agents_doc}")
            if agent_entry(agents_doc, self.agent) is None:
                rows = agents_doc.get("agents") if isinstance(agents_doc, dict) else None
                offered = [row.get("id") for row in rows or () if isinstance(row, dict)]
                raise RefusalError(INVALID_PARAMS, f"Mirasim has no agent {self.agent!r}; it offers {offered}")
            if isinstance(catalog_doc, BaseException):
                logger.bind(agent_output=True).warning(
                    "Mirasim adapter: no model menu for {} ({})", self.agent, catalog_doc
                )
                self._catalog_failed_at = ended
                if self._catalog is not None:
                    return self._catalog
                catalog_doc = None
            else:
                self._catalog_failed_at = None
            self._catalog = read_catalog(self.agent, agents_doc, catalog_doc)
            self._catalog_at = ended
            return self._catalog

    async def new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = _required_text(params, "cwd")
        catalog = await self.catalog()
        if not catalog.installed:
            catalog = await self.catalog(refresh=True)
        if not catalog.installed:
            raise RefusalError(
                INTERNAL_ERROR,
                f"Mirasim reports its {catalog.label} agent as not installed on the PATH Raven starts it with; "
                f"install it from Mirasim's Settings -> Agents, or with `mirasim ui-cli cli install --agent "
                f"{self.agent}`",
            )
        session = Session(id=str(uuid.uuid4()), cwd=cwd, mcp_names=_mcp_names(params))
        self._remember(session)
        return {"sessionId": session.id, **self._state(session, catalog)}

    async def load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = _required_text(params, "sessionId")
        cwd = _required_text(params, "cwd")
        session = self.sessions.get(session_id)
        if session is None:
            binding = await asyncio.to_thread(self.store.get, session_id)
            if binding is None:
                raise RefusalError(RESOURCE_NOT_FOUND, f"no Mirasim session {session_id!r} is recorded here")
            session = Session(
                id=binding.session_id, cwd=binding.cwd, mirasim_key=binding.mirasim_key, route=binding.route
            )
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
        return self._state(session, await self.catalog())

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

    async def delete_session(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = _required_text(params, "sessionId")
        session = self.sessions.pop(session_id, None)
        if session is not None:
            session.deleted = True
            session.turn_cancelled = True
            if session.running is not None:
                await session.running.cancel()
            async with session.store_lock:
                await asyncio.to_thread(self.store.delete, session_id)
            return {}
        await asyncio.to_thread(self.store.delete, session_id)
        return {}

    async def cancel(self, params: dict[str, Any]) -> None:
        session = self.sessions.get(params.get("sessionId") or "")
        if session is None or not session.pending:
            return
        session.turn_cancelled = True
        if session.running is not None:
            await session.running.cancel()

    async def prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._session(params)
        text, files = _prompt_input(params.get("prompt"))
        session.pending += 1
        try:
            async with session.lock:
                try:
                    return await self._turn(session, text, files)
                finally:
                    session.turn_cancelled = False
        finally:
            session.pending -= 1

    async def _turn(self, session: Session, text: str, files: list[str]) -> dict[str, Any]:
        catalog = await self.catalog(allow_stale=True)
        notes: list[str] = []
        with _staged_prompt(text) as (prompt_text, staged):
            retried = False
            while True:
                if session.turn_cancelled or session.deleted:
                    return {"stopReason": "cancelled"}
                model = session.model or catalog.default_model
                route = catalog.route_for(session.model)
                if route is not None and session.mirasim_key is not None:
                    if not await self._put_on_route(session, session.mirasim_key, route):
                        return {"stopReason": "cancelled"}
                elif route is not None:
                    # A new conversation starts on it, and is on Mirasim's
                    # default when the flag names none.
                    session.route = route
                if session.turn_cancelled or session.deleted:
                    return {"stopReason": "cancelled"}
                invocation = self.cli.start(
                    prompt_args(
                        text=prompt_text,
                        agent=self.agent,
                        cwd=session.cwd,
                        session_key=session.mirasim_key,
                        model=catalog.mirasim_model(model),
                        effort=self._effort(session, catalog, model),
                        route=route if route in (OWN_ROUTE, PLATFORM_ROUTE) else None,
                        files=[*files, *staged],
                    )
                )
                session.running = invocation
                watcher = asyncio.create_task(self._watch(session, invocation, notes))
                try:
                    outcome = await invocation.run()
                finally:
                    session.running = None
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                result = await self._settle(session, outcome, notes)
                if result is not None:
                    return result
                failure = _failure(outcome)
                if not retried and session.mirasim_key and session_gone(failure):
                    # The record outlived the conversation (deleted in Mirasim, or
                    # another home). Re-dispatching cannot help -- the next load
                    # finds the same record -- so the turn starts a new one here
                    # and says so, rather than failing every turn after it.
                    logger.warning(
                        "Mirasim adapter: {} no longer has {}; starting a new conversation",
                        self.agent,
                        session.mirasim_key,
                    )
                    session.mirasim_key = None
                    notes.append(
                        "The Mirasim conversation this session continued no longer exists, so this turn ran in a "
                        "new one without the earlier context."
                    )
                    retried = True
                    continue
                await self._persist(session)
                raise RefusalError(INTERNAL_ERROR, f"Mirasim {self.agent} turn failed: {failure}")

    async def _put_on_route(self, session: Session, key: str, route: str) -> bool:
        """Switch conversation ``key`` to ``route`` unless its record has it there; ``False`` when cancelled.

        The record is read again first, because any adapter process may have
        switched the conversation since this one last ran a turn on it. It is
        marked unknown before the switch and set after, so a switch that did not
        finish -- refused, cancelled, or this process gone part-way -- is made
        again on the next turn rather than taken as done. A refused switch
        refuses the turn: run anyway, it would bill a route nobody chose.
        """
        stored = await asyncio.to_thread(self.store.get, session.id)
        if stored is not None and stored.mirasim_key == key:
            session.route = stored.route
        if session.route == route:
            return True
        session.route = None
        await self._persist(session)
        if session.turn_cancelled or session.deleted:
            return False
        invocation = self.cli.start(route_args(key, route))
        session.running = invocation
        try:
            outcome = await asyncio.wait_for(invocation.run(), QUERY_TIMEOUT_S)
        except TimeoutError:
            said = f"`mirasim ui-cli session route` did not answer within {QUERY_TIMEOUT_S:.0f}s"
            raise RefusalError(INTERNAL_ERROR, self._route_refused(key, route, said)) from None
        except UiCliError as exc:
            raise RefusalError(INTERNAL_ERROR, self._route_refused(key, route, str(exc))) from exc
        finally:
            session.running = None
        if outcome.cancelled or session.turn_cancelled:
            return False
        if outcome.exit_code != 0 or not isinstance(outcome.document, dict):
            said = outcome.failure or f"`mirasim ui-cli session route` exited {outcome.exit_code}"
            raise RefusalError(INTERNAL_ERROR, self._route_refused(key, route, said))
        session.route = route
        await self._persist(session)
        return True

    def _route_refused(self, key: str, route: str, said: str) -> str:
        return f"Mirasim could not put {self.agent} session {key} on the {route} route, so the turn did not run: {said}"

    async def _settle(self, session: Session, outcome: Outcome, notes: list[str]) -> dict[str, Any] | None:
        """The prompt response for a turn that ended or was cancelled; ``None`` for a failure."""
        doc = outcome.document if isinstance(outcome.document, dict) else None
        key = doc.get("sessionKey") if doc is not None else None
        if isinstance(key, str) and key:
            # Bound on a failed turn too: the conversation exists in Mirasim
            # whatever the turn did, and the next prompt should continue it.
            session.mirasim_key = key
        if doc is not None and doc.get("settled") is True and not doc.get("error") and not doc.get("incomplete"):
            await self._persist(session)
            self._reply(session, _text(doc.get("text")), notes)
            return {"stopReason": "end_turn"}
        if doc is not None and pending(doc):
            await self._persist(session)
            notes.append(_render_interactions(doc))
            self._reply(session, _text(doc.get("text")), notes)
            return {"stopReason": "end_turn"}
        if outcome.cancelled or session.turn_cancelled:
            await self._persist(session)
            return {"stopReason": "cancelled"}
        return None

    async def _watch(self, session: Session, invocation: Invocation, notes: list[str]) -> None:
        """Answer what the running turn stops to ask, until the turn ends and this is cancelled.

        Every interaction still pending is answered on every look, the stored
        answer if it was asked already: one whose answer did not arrive -- a
        failed command -- would otherwise hold the turn with nothing left to
        retry it, and one answered already is gone from the next look's
        snapshot, Mirasim dropping it as it resolves.
        """
        try:
            port = await self._port_of(invocation)
            if port is None:
                return
            key = session.mirasim_key
            replies: dict[str, Reply] = {}
            looks = 0
            while True:
                await asyncio.sleep(WATCH_PAUSES_S[min(looks, len(WATCH_PAUSES_S) - 1)])
                looks += 1
                if key is None:
                    # A new conversation has no key before it settles; its
                    # server's own rows say which session it is running.
                    key = live_session(await self._look(port, sessions_args(LOOK_SECONDS)))
                    if key is None:
                        continue
                snapshot = snapshot_of(await self._look(port, snapshot_args(key, LOOK_SECONDS)), key)
                for interaction in pending(snapshot or {}):
                    prompt_id = interaction["promptId"]
                    reply = replies.get(prompt_id)
                    if reply is None:
                        reply = replies[prompt_id] = await self._relay(session, interaction, notes)
                        looks = 0
                    await self._look(port, answer_args(prompt_id, reply.action, reply.value))
        except Exception:  # noqa: BLE001 - a watcher that fails must not take the turn down with it
            logger.opt(exception=True).warning("Mirasim adapter: watching a {} turn failed", self.agent)

    async def _port_of(self, invocation: Invocation) -> int | None:
        """The port the turn's server announces, saying so in the log if it is slow to.

        A turn whose server never says its port is one whose questions nothing
        here can see -- a Mirasim that stopped printing the line, say -- and the
        warning is what tells that apart from a turn that is merely long.
        """
        announced = asyncio.ensure_future(invocation.announced())
        try:
            try:
                return await asyncio.wait_for(asyncio.shield(announced), ANNOUNCE_WARN_S)
            except TimeoutError:
                logger.warning(
                    "Mirasim adapter: a {} turn's server has not said its port after {:.0f}s; "
                    "anything the turn asks cannot be answered until it does",
                    self.agent,
                    ANNOUNCE_WARN_S,
                )
                return await announced
        finally:
            announced.cancel()

    async def _look(self, port: int, args: list[str]) -> Any:
        """One command to a running turn's server; ``None`` when it fails, which the next look retries."""
        try:
            return await self.cli.query(args, port=port, timeout_s=LOOK_TIMEOUT_S)
        except UiCliError as exc:
            logger.bind(agent_output=True).debug("Mirasim adapter: `{}` on a turn's server failed: {}", args[0], exc)
            return None

    async def _relay(self, session: Session, interaction: dict[str, Any], notes: list[str]) -> Reply:
        """The client's answer to one interaction, or the skip or dismissal that stands in for one."""
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

    def _reply(self, session: Session, text: str, notes: list[str]) -> None:
        if session.mcp_names and not session.mcp_noted:
            session.mcp_noted = True
            notes.append(
                f"MCP servers {', '.join(session.mcp_names)} were not handed to the agent: Mirasim's ui-cli "
                "takes no servers per session."
            )
        reply = text + "".join(f"\n\n[Mirasim] {note}" for note in notes)
        for start in range(0, len(reply), REPLY_CHUNK_CHARS):
            self.emit(
                notification(
                    "session/update",
                    {
                        "sessionId": session.id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": reply[start : start + REPLY_CHUNK_CHARS]},
                        },
                    },
                )
            )

    def _effort(self, session: Session, catalog: AgentCatalog, model: str | None) -> str | None:
        """The rung this turn asks for: the session's own, else Mirasim's default, if ``model`` takes it.

        Mirasim drops a rung the model lacks without a word -- measured, ``--effort
        bogus`` ran the turn with no effort at all -- so a choice that cannot apply
        is left off here and said once, rather than sent and silently ignored.
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
        idle = [sid for sid, held in self.sessions.items() if not held.pending]
        for sid in idle[: max(0, len(idle) - MAX_IDLE_SESSIONS)]:
            del self.sessions[sid]

    async def shutdown(self) -> None:
        """End every running turn, as a client that hung up can no longer cancel them.

        Questions still out with the client fail first, so a turn waiting on one
        is not held for a client that is gone.
        """
        self.outbound.close()
        if self._prefetch is not None and not self._prefetch.done():
            self._prefetch.cancel()
        running = [session for session in self.sessions.values() if session.running is not None]
        for session in running:
            session.turn_cancelled = True
        await asyncio.gather(
            *(session.running.cancel() for session in running if session.running), return_exceptions=True
        )


def _note_prefetch(task: asyncio.Task[AgentCatalog]) -> None:
    """Retrieve a failed background read, which a session that never opens would leave unread."""
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.warning("Mirasim adapter: reading the agent's menus failed: {}", exc)


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
    return value if isinstance(value, str) else ""


def _mcp_names(params: dict[str, Any]) -> tuple[str, ...]:
    servers = params.get("mcpServers")
    if not isinstance(servers, list):
        return ()
    return tuple(str(server.get("name") or "?") for server in servers if isinstance(server, dict))


def _prompt_input(blocks: Any) -> tuple[str, list[str]]:
    """The prompt text and the local files a ``session/prompt`` carries.

    A ``resource_link`` to a local file is handed to Mirasim as an attachment,
    which is how Mirasim's own composer attaches one; a link to anything else
    stays in the text as its address.
    """
    if not isinstance(blocks, list) or not blocks:
        raise RefusalError(INVALID_PARAMS, "prompt is a non-empty list of content blocks")
    texts: list[str] = []
    files: list[str] = []
    for block in blocks:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text":
            texts.append(_text(block.get("text")))
        elif kind == "resource_link":
            uri = _text(block.get("uri"))
            path = _local_file(uri)
            if path is not None:
                files.append(path)
            else:
                texts.append(f"\n[{_text(block.get('name')) or 'link'}]({uri})\n")
        elif kind == "resource" and isinstance(block.get("resource"), dict) and "text" in block["resource"]:
            resource = block["resource"]
            texts.append(f"\n[{_text(resource.get('uri')) or 'resource'}]\n{_text(resource.get('text'))}\n")
        else:
            raise RefusalError(INVALID_PARAMS, f"content block {kind!r} is not supported; send text or a resource_link")
    text = "".join(texts).strip()
    if not text and files:
        text = "See the attached files."
    if not text:
        raise RefusalError(INVALID_PARAMS, "the prompt carries no text")
    return text, files


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


@contextlib.contextmanager
def _staged_prompt(text: str) -> Iterator[tuple[str, list[str]]]:
    """The prompt as ``--text`` takes it, and the file holding it when it is too long for one argv entry."""
    if len(text.encode("utf-8")) <= INLINE_TEXT_LIMIT:
        yield text, []
        return
    folder = Path(tempfile.mkdtemp(prefix="raven-mirasim-"))
    try:
        path = folder / "prompt.md"
        path.write_text(text, encoding="utf-8")
        yield PROMPT_FILE_NOTE.format(name=path.name), [str(path)]
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _render_interactions(doc: dict[str, Any]) -> str:
    """What a turn came back still asking, in words, for a caller who can answer it in the next prompt."""
    lines = [
        "The agent stopped to ask the following and is waiting for an answer, which this adapter could not give "
        "for you. Answer it in the next prompt to this same session; a new session will not see the question."
    ]
    lines += [f"- {describe(item)}" for item in pending(doc)]
    return "\n".join(lines)


def _failure(outcome: Outcome) -> str:
    doc = outcome.document if isinstance(outcome.document, dict) else None
    said = _text(doc.get("error")) if doc is not None else ""
    if said:
        return " ".join(said.split())
    if outcome.failure:
        return outcome.failure
    if doc is not None:
        state = "incomplete" if doc.get("incomplete") else "unsettled"
        return f"Mirasim reported the turn {state} (phase {doc.get('phase')!r})"
    return f"`mirasim ui-cli prompt` exited {outcome.exit_code} with no result"


async def serve(reader: asyncio.StreamReader, out: BinaryIO, *, agent: str, cli: UiCli, store: BindingStore) -> None:
    """Serve ACP on one reader/writer pair until the client closes stdin."""

    def emit(frame: dict[str, Any]) -> None:
        try:
            write_frame(out, frame)
        except (BrokenPipeError, ValueError, OSError) as exc:
            logger.warning("Mirasim adapter: the client is gone ({}); dropping a frame", exc)

    server = MirasimAgent(agent=agent, cli=cli, store=store, emit=emit)
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
    pending = [task for task in tasks if not task.done()]
    if pending:
        _, still = await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_S)
        for task in still:
            task.cancel()
        await asyncio.gather(*still, return_exceptions=True)


async def _main(args: argparse.Namespace, out: BinaryIO) -> None:
    workdir = server_workdir()
    with contextlib.suppress(OSError):
        workdir.mkdir(parents=True, exist_ok=True)
    cli = UiCli(executable=args.executable, workdir=workdir)
    store = BindingStore(store_path(args.agent))
    async with open_stdin() as reader:
        # SIGTERM ends the session the way a closed stdin does, so the turns
        # still running are ended with their trees instead of being left behind.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, reader.feed_eof)
        await serve(reader, out, agent=args.agent, cli=cli, store=store)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m raven.acp.mirasim",
        description="Serve one Mirasim agent over ACP on stdio, one `mirasim ui-cli` command per request.",
    )
    parser.add_argument("--agent", required=True, help="the Mirasim agent id, as `mirasim ui-cli agents` lists it")
    parser.add_argument("--executable", default=EXECUTABLE, help="the Mirasim command (default: %(default)s)")
    parser.add_argument("--verbose", action="store_true", help="log progress to stderr, not only problems")
    args = parser.parse_args(argv)
    if not _AGENT_ID.match(args.agent):
        parser.error(f"--agent {args.agent!r} is not a Mirasim agent id")
    logger.remove()
    # Raven journals this stream, so annotated frames would publish every local
    # a failure carries -- the prompt among them (`raven/cli/_log_file.py`).
    logger.add(sys.stderr, level="INFO" if args.verbose else "WARNING", backtrace=False, diagnose=False)
    with claim_stdout() as out:
        asyncio_runner.run(_main(args, out))
    return 0


__all__ = ["CATALOG_TTL_S", "MAX_IDLE_SESSIONS", "MirasimAgent", "RefusalError", "Session", "main", "serve"]
