"""The ACP methods Raven-X answers, mapped straight onto the spine.

Adapted from the main repo's ``raven/acp/methods.py`` with the dispatcher
round-trip removed: the main repo routes every handler through its RPC
dispatcher to reuse ``turn.send``'s guards, but those guards live on the
subscription-emitter path this build deliberately does not take (plan §2.2).
The two guards a prompt actually needs -- one turn per session, a stopReason on
every exit -- are the session registry's and the sink's.

Two rules run through the whole file (the main repo's, kept):

* **Tolerant inbound.** Unknown params are ignored, a wrongly-typed
  ``protocolVersion`` is read for intent, and an unknown method is *answered*
  rather than dropped -- the failure this prevents is a client left waiting on
  a promise nothing will resolve.
* **A prompt is never answered with a JSON-RPC error.** Whatever happens to
  the turn -- refused before it started, failed halfway, cancelled -- the
  client gets a ``stopReason``, with the explanation as message content.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote, urlparse

from loguru import logger

from raven.acp import protocol
from raven.acp.capabilities import ClientCapabilities, initialize_result
from raven.acp.questions import CLARIFY_RESPOND_METHOD
from raven.acp.redact import redact, redact_value
from raven.acp.replay import replay
from raven.acp.spine import AcpSession, AcpSessions, TurnAlreadyRunningError
from raven.spine import ChatType, Origin, Source, TurnRequest

# Methods this agent knows of and does not serve: main-repo extensions the
# consuming raven never calls, plus spec surface with no consumer here.
# Answered with method-not-found -- the same answer an unknown name gets; the
# set exists so a reader can tell "not built" from "not in the protocol".
#
# ``session/set_mode`` is in the set and still served: the router answers it
# before this check whenever the build declares modes, so membership here is
# what a build that declares NONE falls through to. Dropping it from the set
# would answer that build with "unknown method", which reads as a version
# mismatch rather than as a surface this deployment did not turn on.
UNIMPLEMENTED_METHODS = frozenset(
    {
        "session/list",
        "session/close",
        "session/delete",
        "session/set_config_option",
        "session/set_mode",
        "logout",
    }
)

# Notifications that are safe to receive and correct to ignore.
IGNORED_NOTIFICATIONS = frozenset({"$/cancel_request"})

# Keys an internal error's data may carry that must not leave the process: a
# traceback tail names the filesystem it ran on and sometimes argument values.
_PRIVATE_ERROR_KEYS = frozenset({"traceback_tail", "traceback", "stack"})

_SENDER_ID = "acp-client"


def sanitise_error_data(data: Any) -> Any:
    """What may leave the process from an internal error's ``data``."""
    if not isinstance(data, dict):
        return redact_value(data)
    kept = {key: value for key, value in data.items() if key not in _PRIVATE_ERROR_KEYS}
    return redact_value(kept) or None


class AcpMethodError(Exception):
    """A JSON-RPC error to answer one request with."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class AcpMethods:
    """Answers inbound ACP frames against the assembled ACP spine.

    ``submit`` is ``Scheduler.submit``; ``sessions`` is the registry the sink
    settles into; ``emit`` writes one finished frame and carries the
    notifications a handler produces on its way to an answer.

    ``session_manager`` must be the ENGINE'S own manager (the agent loop's
    ``sessions`` attribute): a freshly built manager caches nothing, so a
    session loaded through one would be reloaded -- or missed -- by the turn
    that follows. ``None`` (a test without an engine) answers every
    ``session/load`` with -32002, which is honest for a store it cannot see.

    ``on_session_open`` is awaited once per session as it enters the registry,
    on all three routes in (new / load / resume). It is where the session's
    engine gets built, so a construction failure is answered as a failed
    ``session/new`` rather than surfacing halfway through the first turn.
    """

    def __init__(
        self,
        *,
        submit: Callable[[TurnRequest], Any],
        sessions: AcpSessions,
        emit: Callable[[dict[str, Any]], None],
        session_manager: Any = None,
        channel: str = "acp",
        question_broker: Any = None,
        arm_ask_user: Callable[[bool], None] | None = None,
        on_session_open: Callable[[str], Awaitable[Any]] | None = None,
        modes: Any = None,
    ) -> None:
        self._submit = submit
        self._sessions = sessions
        self._emit = emit
        self._session_manager = session_manager
        self._channel = channel
        self._on_session_open = on_session_open
        # The ask_user round trip (raven/acp/questions.py). The broker resolves
        # ``_raven/clarify_respond``; ``arm_ask_user`` is the server's seam that
        # binds or unbinds it on the engine's tool, called from initialize with
        # the client's declaration -- the methods layer knows the capability,
        # the server knows the tool, and neither should know the other's half.
        self._question_broker = question_broker
        self._arm_ask_user = arm_ask_user
        # raven/acp/modes.py. ``None`` (a test, or a build declaring no modes)
        # leaves session/set_mode method-not-found and every session response
        # without a ``modes`` object, which is the pre-modes wire byte for byte.
        self._modes = modes
        self.initialized = False
        self.client = ClientCapabilities()

    # -- frame handling ---------------------------------------------------

    async def handle(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one inbound frame, or return ``None`` to stay silent.

        The request/notification split is on the *presence* of ``id``, not its
        truthiness: JSON-RPC says a notification is a frame with no ``id``
        member, so ``{"id": 0}`` and ``{"id": null}`` are requests -- a request
        unanswered because its id was falsy is a hang with no diagnostic.
        """
        if "method" not in frame:
            # A response frame. This agent sends no outbound requests in Phase
            # 1, so there is nothing it could answer.
            logger.debug("acp: a response arrived for no outstanding request: id={}", frame.get("id"))
            return None
        method = frame.get("method")
        is_request = "id" in frame
        request_id = frame.get("id")

        if not isinstance(method, str):
            if not is_request:
                return None
            return protocol.error_response(request_id, protocol.INVALID_REQUEST, "method must be a string")

        params = frame.get("params")
        if params is not None and not isinstance(params, dict):
            # ACP uses by-name params throughout; a positional array is legal
            # JSON-RPC and unusable here, so say so rather than reading it as
            # absent.
            if not is_request:
                return None
            return protocol.error_response(request_id, protocol.INVALID_PARAMS, f"{method} params must be an object")

        try:
            result = await self._route(method, params or {}, is_request=is_request)
        except AcpMethodError as exc:
            if not is_request:
                logger.debug("acp: notification {} failed: {}", method, exc.message)
                return None
            return protocol.error_response(request_id, exc.code, redact(exc.message), sanitise_error_data(exc.data))
        except Exception as exc:
            # The connection outlives one bad request; without this the read
            # loop dies on a handler bug and the client sees the agent vanish
            # mid-turn.
            logger.exception("acp: {} raised", method)
            if not is_request:
                return None
            return protocol.error_response(
                request_id,
                protocol.INTERNAL_ERROR,
                f"{method} failed",
                {"reason": redact(str(exc)[:400])},
            )
        if not is_request:
            return None
        return protocol.result_response(request_id, result if result is not None else {})

    async def _route(self, method: str, params: dict[str, Any], *, is_request: bool) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method in IGNORED_NOTIFICATIONS:
            return None
        if not self.initialized:
            raise AcpMethodError(
                protocol.INVALID_REQUEST,
                "initialize must be called before any other method",
                {"method": method},
            )
        if method == "authenticate":
            # authMethods is empty, a statement that none is needed. A client
            # calling anyway is told what was declared, not given a
            # method-not-found it would read as a version mismatch.
            raise AcpMethodError(
                protocol.INVALID_PARAMS,
                "this agent advertises no authentication methods",
                {"authMethods": []},
            )
        if method == "session/new":
            return await self._session_new(params)
        if method == "session/load":
            return await self._session_load(params)
        if method == "session/resume":
            return await self._session_resume(params)
        if method == "session/prompt":
            return await self._session_prompt(params)
        if method == "session/cancel":
            return await self._session_cancel(params)
        if method == "session/set_mode" and self._modes is not None and self._modes.enabled:
            return self._session_set_mode(params)
        if method == CLARIFY_RESPOND_METHOD:
            return self._clarify_respond(params)
        if method in UNIMPLEMENTED_METHODS:
            raise AcpMethodError(protocol.METHOD_NOT_FOUND, f"{method} is not implemented")
        raise AcpMethodError(protocol.METHOD_NOT_FOUND, f"unknown method {method}")

    # -- handlers ---------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        # Re-initialising is allowed: the state this replaces is only the
        # capability record, and refusing would strand a client whose first
        # attempt raced its own setup.
        self.client = ClientCapabilities.from_params(params)
        self.initialized = True
        if self._arm_ask_user is not None:
            # Follows the record in BOTH directions on a re-initialize: a
            # declaration that disappeared must disarm, or the tool would keep
            # asking a client that no longer renders the question.
            self._arm_ask_user(self.client.ask_user)
        return initialize_result(params)

    async def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        """Mint a session; the ACP sessionId IS the raven session key.

        ``cwd`` is acknowledged and ignored (plan §3): the AgentLoop's
        workspace is fixed at construction and session history is keyed by the
        session id, so the client's directory has nothing to bind to. Logged
        so a client that expected it to matter leaves a trace.
        """
        self._refuse_per_session_mcp(params)
        cwd = params.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            logger.info("acp: session cwd {} noted and ignored; the workspace is fixed at construction", cwd)
        chat_id = _new_chat_id()
        session = AcpSession(session_id=f"{self._channel}:{chat_id}", chat_id=chat_id)
        self._sessions.add(session)
        await self._open_session(session.session_id)
        logger.info("acp: session {} created", session.session_id)
        return self._with_modes({"sessionId": session.session_id}, session.session_id)

    async def _session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        """Reopen a stored session, replaying it as it is loaded.

        Not a getter: the transcript goes out as ``session/update``
        notifications *before* this returns, so a resumed session is drawn by
        the same client code that draws a live one. The updates come from
        :mod:`raven.acp.replay`; continuity for the prompts that follow is the
        engine's session manager loading the same key from disk.
        """
        stored, session = self._reopen(params)
        await self._open_session(session.session_id)
        for update in replay(stored.messages, cwd=None):
            self._emit(protocol.notification("session/update", {"sessionId": session.session_id, "update": update}))
        logger.info("acp: loaded session {} ({} stored message(s))", session.session_id, len(stored.messages))
        return self._with_modes({}, session.session_id)

    async def _session_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        """Reopen a stored session for its own state, without replaying it.

        The distinction is the spec's and it is the whole of the method:
        ``session/load`` repaints a client that lost its history;
        ``session/resume`` makes a session the client already has promptable
        again. Both establish the same state and refuse an unknown id the same
        way. This is also the method behind ``sessionCapabilities.resume`` --
        the key the consuming raven reads as statefulness (plan §6 decision A).
        """
        _, session = self._reopen(params)
        await self._open_session(session.session_id)
        logger.info("acp: resumed session {}", session.session_id)
        return self._with_modes({}, session.session_id)

    def _reopen(self, params: dict[str, Any]) -> tuple[Any, AcpSession]:
        """The stored session behind ``params`` and its live registry entry.

        Only ids this channel minted are looked up: the engine's manager also
        files other channels' conversations (``cli:...``), and serving one of
        those to an ACP client would read a terminal session's transcript out
        through a different product's surface. An unknown or foreign id is
        -32002, never a silent fresh session -- a client silently handed a new
        session shows a person an empty transcript for a conversation that had
        one.
        """
        self._refuse_per_session_mcp(params)
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpMethodError(protocol.INVALID_PARAMS, "sessionId is required", {"field": "sessionId"})
        prefix = f"{self._channel}:"
        stored = None
        if session_id.startswith(prefix) and self._session_manager is not None:
            stored = self._session_manager.peek(session_id)
        if stored is None:
            raise AcpMethodError(protocol.RESOURCE_NOT_FOUND, "unknown session", {"sessionId": session_id})
        session = self._sessions.get(session_id)
        if session is None:
            session = AcpSession(session_id=session_id, chat_id=session_id[len(prefix) :])
            self._sessions.add(session)
        return stored, session

    async def _open_session(self, session_id: str) -> None:
        """Build the session's engine, or fail the method that opened it.

        The registry entry is rolled back on failure: a session that answered
        with an error and stayed registered would take a prompt, and the engine
        it could not build would be built again halfway through that turn --
        exactly the failure this ordering exists to prevent.
        """
        if self._on_session_open is None:
            return
        try:
            await self._on_session_open(session_id)
        except Exception:
            self._sessions.remove(session_id)
            raise

    async def _session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run one turn and answer with its stop reason.

        The answer comes from the session's future, which the spine sink
        resolves after the render barrier -- so every update belonging to this
        turn is on the wire before this returns.
        """
        session = self._session_for(params)
        text, notes = self._read_prompt(params.get("prompt"))
        for note in notes:
            logger.info("acp: prompt content degraded: {}", note)
        if not text.strip():
            # The client asked for a turn on an empty prompt; no turn ran. Said
            # out loud to keep the invariant that every completed prompt has at
            # least one message chunk -- the consuming raven treats a chunkless
            # turn as AcpEmptyTurnError.
            self._say(session, "The prompt contained no usable text; no turn was run.")
            return {"stopReason": "end_turn"}

        try:
            future = self._sessions.begin_turn(session.session_id)
        except TurnAlreadyRunningError as exc:
            raise AcpMethodError(
                protocol.INVALID_REQUEST,
                str(exc),
                {"sessionId": session.session_id},
            ) from exc
        try:
            request = TurnRequest(
                origin=Origin.USER,
                source=Source(
                    channel=self._channel,
                    chat_id=session.chat_id,
                    sender_id=_SENDER_ID,
                    chat_type=ChatType.DM,
                ),
                text=text,
            )
            try:
                handle = self._submit(request)
            except Exception as exc:
                # The turn never started, so no terminating event is coming and
                # awaiting the future would hang. Say why, then end the turn:
                # a prompt is answered with a stopReason even when refused.
                logger.exception("acp: submitting a turn for {} failed", session.session_id)
                self._say(session, f"The turn could not start: {redact(str(exc))}")
                return {"stopReason": "end_turn"}
            self._sessions.bind_handle(session.session_id, handle)
            stop = await future
        finally:
            self._sessions.end_turn(session.session_id)
        return {"stopReason": stop}

    async def _session_cancel(self, params: dict[str, Any]) -> None:
        """Cancel the session's turn, wait out its unwind, then answer its prompt.

        The wait in the middle closes a prompt-hijack race: the old shape
        settled the prompt immediately, while the cancelled turn's terminating
        TurnFailed was still in flight behind the render barrier -- and the
        settle gate keys on the session, so a new prompt armed in that window
        was resolved by the OLD turn's event: a chunkless "cancelled" for a
        turn nobody cancelled. ``handle.result()`` resolves strictly after the
        sink has processed the turn's terminating event (the lane worker
        resolves it after ``_run_turn`` returns, and the sink is awaited
        inside ``_run_turn``), so a started turn's prompt is settled by the
        sink before the wait returns, and no event stays behind to leak into
        the next turn. The wait also restores the sink's ordering invariant
        for cancels: the stopReason now leaves after the turn's final frames.
        Same shape as ``tui_rpc``'s ``turn_cancel`` ("await the handle so the
        turn is provably unwound").

        The settle at the end is the backstop for the turn that never started
        (dropped from the lane queue, which emits no event) -- addressed by
        the future captured BEFORE the wait, never the session's current slot,
        which by now may already belong to the next prompt.
        """
        session_id = str(params.get("sessionId") or "")
        session = self._sessions.get(session_id)
        if session is None:
            # Notifications have no reply; a client cancelling a session it
            # already dropped is tidy, not broken.
            logger.debug("acp: session/cancel for unknown session {}", session_id)
            return None
        future = session.future
        handle = session.handle
        try:
            if handle is not None:
                # A turn blocked on an ask_user round trip must have its
                # question resolved FIRST: the broker fail-safes it to the
                # default, so the wait releases whatever shape the handle's
                # cancel takes, and the cancel itself then lands at an await
                # point that propagates it instead of one that absorbs it.
                if self._question_broker is not None:
                    self._question_broker.cancel(session_id)
                handle.cancel()
                await handle.result()
        finally:
            # In finally so a cancelled handler task (shutdown) cannot skip it;
            # idempotent against the sink's settle either way.
            self._sessions.settle_future(future, "cancelled")
        return None

    def _session_set_mode(self, params: dict[str, Any]) -> dict[str, Any]:
        """Switch which profile this session's next turn runs on.

        Nothing is refused for timing and nothing is interrupted: a switch
        during a turn leaves that turn on the profile it started with and lands
        on the next one. That is not leniency about a race -- the engine is
        resolved once per turn, at its start, so "the next turn" is the only
        moment at which a different profile can be applied without pulling a
        running turn's tools out from under it.

        The session is looked up first, so an unknown session is -32002 rather
        than a mode error about a session that does not exist.
        """
        session = self._session_for(params)
        mode_id = params.get("modeId")
        if not isinstance(mode_id, str) or not mode_id:
            raise AcpMethodError(protocol.INVALID_PARAMS, "modeId is required", {"field": "modeId"})
        try:
            self._modes.set(session.session_id, mode_id)
        except KeyError as exc:
            raise AcpMethodError(
                protocol.INVALID_PARAMS,
                f"unknown mode {mode_id!r}",
                {"field": "modeId", "availableModes": list(self._modes.ids())},
            ) from exc
        return {}

    def _with_modes(self, result: dict[str, Any], session_id: str) -> dict[str, Any]:
        """Add the session's ``modes`` object to a session response, if any.

        On all three routes in. A client that reconnects to a session it did not
        open has no other way to learn which mode it is in, and a mode picker
        drawn from ``session/new`` alone would be blank after a ``session/load``.
        """
        state = self._modes.state(session_id) if self._modes is not None else None
        if state is not None:
            result["modes"] = state
        return result

    def _clarify_respond(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resolve a pending ask_user question by requestId or sessionId.

        Tolerant on purpose: a stale or unknown handle answers
        ``{"delivered": false}`` rather than an error -- the question may have
        just timed out, or its turn been cancelled, and the client can do
        nothing useful with a failure it did not cause. ``QuestionBroker.reply``
        is idempotent, so a double answer is the same false.
        """
        if self._question_broker is None:
            return {"delivered": False}
        key = str(params.get("requestId") or params.get("sessionId") or "")
        if not key:
            return {"delivered": False}
        answer = params.get("answer")
        delivered = self._question_broker.reply(key, str(answer if answer is not None else ""))
        return {"delivered": bool(delivered)}

    # -- prompt content ---------------------------------------------------

    def _read_prompt(self, blocks: Any) -> tuple[str, list[str]]:
        """Flatten ACP content blocks into the text a turn takes.

        Returns the text and a list of notes about anything degraded, for the
        log. One unusable block never fails the prompt. promptCapabilities
        declares text only, but a spec-tolerant reader still names what it
        cannot use rather than dropping it silently.
        """
        if not isinstance(blocks, list):
            raise AcpMethodError(protocol.INVALID_PARAMS, "prompt must be an array of content blocks")
        parts: list[str] = []
        notes: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                notes.append("a prompt block was not an object")
                continue
            kind = block.get("type")
            if kind == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
                continue
            if kind == "resource_link":
                # Sent by editors for every @-mention of a file, gated by no
                # capability at all; named in the prompt rather than read here,
                # so the agent's read goes through the tool that reports it.
                parts.append(self._resource_link(block))
                continue
            if kind == "resource":
                rendered, note = self._embedded_resource(block)
                if rendered:
                    parts.append(rendered)
                if note:
                    notes.append(note)
                continue
            if kind == "image":
                parts.append("[an image attachment was sent, which this agent does not accept]")
                notes.append("image block received despite promptCapabilities.image=false")
                continue
            if kind == "audio":
                parts.append("[an audio attachment was sent, which this agent cannot read]")
                notes.append("audio block received despite promptCapabilities.audio=false")
                continue
            notes.append(f"unknown prompt block type {kind!r}")
        return "\n\n".join(parts), notes

    @staticmethod
    def _resource_link(block: dict[str, Any]) -> str:
        uri = block.get("uri")
        name = block.get("name")
        label = name if isinstance(name, str) and name else "resource"
        if not isinstance(uri, str) or not uri:
            return f"[{label}]"
        path = _file_uri_to_path(uri)
        return f"[{label}: {path or uri}]"

    @staticmethod
    def _embedded_resource(block: dict[str, Any]) -> tuple[str, str | None]:
        # A text resource is inlined; a blob has no honest inline form (base64
        # in a prompt is tokens spent on nothing), so it is named.
        resource = block.get("resource")
        if not isinstance(resource, dict):
            return "", "an embedded resource block carried no resource"
        uri = resource.get("uri") if isinstance(resource.get("uri"), str) else ""
        text = resource.get("text")
        if isinstance(text, str):
            label = _file_uri_to_path(uri) or uri or "embedded resource"
            return f"[{label}]\n{text}", None
        if "blob" in resource:
            label = _file_uri_to_path(uri) or uri or "embedded resource"
            return f"[binary resource, not inlined: {label}]", f"blob resource {label} was named, not inlined"
        return "", "an embedded resource had neither text nor blob"

    # -- plumbing ---------------------------------------------------------

    def _session_for(self, params: dict[str, Any]) -> AcpSession:
        session_id = params.get("sessionId")
        session = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if session is None:
            # -32002, the code the spec assigns to an unknown session -- never a
            # silent fresh session: a client must be told, or it shows a person
            # an empty transcript for a conversation that had one.
            raise AcpMethodError(
                protocol.RESOURCE_NOT_FOUND,
                "unknown session",
                {"sessionId": session_id},
            )
        return session

    @staticmethod
    def _refuse_per_session_mcp(params: dict[str, Any]) -> None:
        # MCP is connected once per process; nothing scopes a server to one
        # session, and accepting the field would leave a client believing its
        # tools are available. An empty array is the schema's normal value.
        servers = params.get("mcpServers")
        if isinstance(servers, list) and servers:
            raise AcpMethodError(
                protocol.INVALID_PARAMS,
                "per-session MCP servers are not supported; configure MCP servers in raven's own config",
                {"field": "mcpServers", "count": len(servers)},
            )

    def _say(self, session: AcpSession, text: str) -> None:
        self._emit(
            protocol.notification(
                "session/update",
                {
                    "sessionId": session.session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text},
                    },
                },
            )
        )


def _new_chat_id() -> str:
    from raven.session.manager import new_chat_id

    return new_chat_id()


def _file_uri_to_path(uri: str) -> str | None:
    """The filesystem path behind a ``file://`` URI, or ``None``.

    Percent-decoded (editors encode spaces) and restricted to a local URI:
    ``file://host/share`` names somebody else's machine.
    """
    if not uri.startswith("file://"):
        return None
    try:
        parsed = urlparse(uri)
    except ValueError:
        return None
    if parsed.netloc and parsed.netloc != "localhost":
        return None
    path = unquote(parsed.path)
    return path or None


__all__ = [
    "IGNORED_NOTIFICATIONS",
    "UNIMPLEMENTED_METHODS",
    "AcpMethodError",
    "AcpMethods",
    "sanitise_error_data",
]
