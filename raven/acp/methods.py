"""The ACP methods raven answers, and how each maps onto an existing RPC call.

Every handler here goes through ``Dispatcher.dispatch`` rather than importing the
handler it wants. That is deliberate: ``turn.send`` / ``turn.cancel`` /
``turn.subscribe`` / ``fs.upload`` are already assembled, already validated
against their pydantic models, and already carry the guards (model availability,
one turn per lane, the upload size limit) that a second call path would have to
grow its own copy of. The cost is one dict round trip per call, against a turn
that takes seconds.

Two rules run through the whole file:

* **Tolerant inbound.** Unknown params are ignored, a wrongly-typed
  ``protocolVersion`` is read for intent, and an unknown method is *answered*
  rather than dropped. The spec asks for this, and the failure it prevents is the
  worst one available: a client left waiting on a promise nothing will resolve.
* **A prompt is never answered with a JSON-RPC error.** Whatever happens to the
  turn -- refused before it started, failed halfway, cancelled -- the client gets
  a ``stopReason``, with the explanation as message content. Measured on
  codex-acp from the other direction: an error in reply to a turn-shaped request
  makes clients tear down the whole turn.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
from collections.abc import Callable
from itertools import count
from typing import Any
from urllib.parse import unquote, urlparse

from loguru import logger

from raven.acp import protocol, redact
from raven.acp.capabilities import ClientCapabilities, initialize_result
from raven.acp.config_options import MODEL_OPTION_ID, model_option, set_model
from raven.acp.replay import replay
from raven.acp.updates import AcpSession, TurnAlreadyRunningError, UpdateTranslator

# Methods in the stable manifest that raven does not serve yet. Answered with
# method-not-found, which is the same answer an unknown name gets -- the
# distinction is kept here only so a reader can see the difference between "not
# in the protocol" and "not built yet".
UNIMPLEMENTED_METHODS = frozenset(
    {
        "session/set_mode",
        "session/resume",
        "session/close",
        "session/delete",
        "logout",
    }
)

# Notifications that are safe to receive and correct to ignore. ``$/cancel_request``
# is protocol-level and explicitly optional: the spec says a receiver MAY act on
# it, and a request it would have cancelled is answered -32800 by whoever owns
# that request rather than here.
IGNORED_NOTIFICATIONS = frozenset({"$/cancel_request"})


# Keys the internal dispatcher attaches that must not leave the process. The
# traceback tail is twelve lines of absolute paths -- and sometimes of argument
# values -- which is diagnostic on a log line and a disclosure in an editor's
# transcript. The reason string beside it is kept, because it is what a client
# can actually show.
_PRIVATE_ERROR_KEYS = frozenset({"traceback_tail", "traceback", "stack"})


def sanitise_error_data(data: Any) -> Any:
    """What may leave the process from an internal error's ``data``.

    Two removals, both measured. The traceback tail goes because every internal
    dispatcher error carries one and it names the filesystem it ran on. Whatever
    survives is then run through the redaction table, because an exception message
    routinely quotes the argument that caused it -- and for ``exec`` that argument
    is a command line.
    """
    if not isinstance(data, dict):
        return redact.redact_value(data)
    kept = {key: value for key, value in data.items() if key not in _PRIVATE_ERROR_KEYS}
    return redact.redact_value(kept) or None


class AcpMethodError(Exception):
    """A JSON-RPC error to answer one request with."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class AcpMethods:
    """Answers inbound ACP frames against an assembled RPC stack.

    ``emit`` writes one finished frame and is used for the notifications a
    handler produces on its way to an answer (the explanation that precedes a
    failed turn's ``stopReason``). Responses travel back through the return
    value instead, so the caller stays in charge of what goes on the wire for a
    given request.
    """

    def __init__(
        self,
        *,
        dispatcher: Any,
        translator: UpdateTranslator,
        emit: Callable[[dict[str, Any]], None],
        agent_loop: Any = None,
        outbound: Any = None,
        questions: Any = None,
        channel: str = "acp",
    ) -> None:
        self._dispatcher = dispatcher
        self._translator = translator
        self._emit = emit
        self._agent_loop = agent_loop
        self._outbound = outbound
        self._questions = questions
        self._channel = channel
        self._ids = count(1)
        self.initialized = False
        self.client = ClientCapabilities()

    # -- frame handling ---------------------------------------------------

    async def handle(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one inbound frame, or return ``None`` to stay silent.

        The request/notification split is on the *presence* of ``id``, not on its
        truthiness. JSON-RPC says a notification is a frame with no ``id``
        member, so ``{"id": 0, ...}`` and ``{"id": null, ...}`` are requests --
        and a request that goes unanswered because its id happened to be falsy is
        a hang with no diagnostic.
        """
        if "method" not in frame:
            # A response. It belongs to whoever sent the request, which is the
            # outbound broker -- a permission prompt is the agent asking and this
            # is the answer arriving. Unmatched is not an error: a client may
            # answer something it invented, or a request this agent already timed
            # out, and either way there is nothing to say back.
            if self._outbound is not None and not self._outbound.resolve(frame):
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
            # JSON-RPC and unusable here, so say so rather than silently reading
            # it as absent.
            if not is_request:
                return None
            return protocol.error_response(request_id, protocol.INVALID_PARAMS, f"{method} params must be an object")

        try:
            result = await self._route(method, params or {}, is_request=is_request)
        except AcpMethodError as exc:
            if not is_request:
                logger.debug("acp: notification {} failed: {}", method, exc.message)
                return None
            return protocol.error_response(
                request_id, exc.code, redact.redact(exc.message), sanitise_error_data(exc.data)
            )
        except Exception as exc:
            # The connection outlives one bad request. Without this the read loop
            # dies on a handler bug and the client sees the agent vanish
            # mid-turn, which is indistinguishable from a crash.
            logger.exception("acp: {} raised", method)
            if not is_request:
                return None
            return protocol.error_response(
                request_id,
                protocol.INTERNAL_ERROR,
                f"{method} failed",
                {"reason": redact.redact(str(exc)[:400])},
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
            # Nothing before the handshake, including session/new: the client's
            # capabilities decide how questions are routed, and a session built
            # without them would have to guess.
            raise AcpMethodError(
                protocol.INVALID_REQUEST,
                "initialize must be called before any other method",
                {"method": method},
            )
        if method == "authenticate":
            # authMethods is empty, which is a statement that none is needed.
            # A client calling this anyway is told what it declared, not given a
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
        if method == "session/list":
            return await self._session_list(params)
        if method == "session/set_config_option":
            return await self._set_config_option(params)
        if method == "session/prompt":
            return await self._session_prompt(params)
        if method == "session/cancel":
            return await self._session_cancel(params)
        if method in UNIMPLEMENTED_METHODS:
            raise AcpMethodError(protocol.METHOD_NOT_FOUND, f"{method} is not implemented")
        raise AcpMethodError(protocol.METHOD_NOT_FOUND, f"unknown method {method}")

    # -- handlers ---------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer the handshake and keep what the client declared.

        Re-initialising is allowed rather than refused. A client that renegotiates
        is unusual, but the state this replaces is only the capability record,
        and refusing would strand a client whose first attempt raced its own
        setup.
        """
        self.client = ClientCapabilities.from_params(params)
        if self._questions is not None:
            # Which route a question takes depends on what the client declared,
            # and the declaration arrives here. Re-initialising is allowed, so
            # this is a set rather than a one-time bind.
            self._questions.set_client(self.client)
        self.initialized = True
        return initialize_result(params)

    async def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        """Mint a session pinned to the client's working directory."""
        self._refuse_per_session_mcp(params)
        cwd = self._validated_cwd(params.get("cwd"))
        session_key = f"{self._channel}:{_new_chat_id()}"
        self._bind_workdir(session_key, cwd)
        subscription_id = await self._subscribe(session_key)
        session = AcpSession(
            session_id=session_key,
            session_key=session_key,
            cwd=cwd,
            subscription_id=subscription_id,
        )
        self._translator.add(session)
        logger.info("acp: session {} created at {}", session_key, cwd)
        # The ACP sessionId *is* the raven session key. One identity rather than
        # two: session/load and session/list both address raven sessions, and a
        # second id space would need a map that survives a restart to be worth
        # anything.
        result: dict[str, Any] = {"sessionId": session.session_id}
        # Offered at creation so a client can put a model picker in the session
        # menu without a second round trip. Absent rather than empty when there
        # is nothing to offer -- an empty list is a menu that opens onto nothing.
        options = await self._config_options()
        if options:
            result["configOptions"] = options
        return result

    async def _session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        """Reopen a stored session, replaying it as it is loaded.

        Not a getter: the transcript goes out as ``session/update`` notifications
        *before* this returns, so a resumed session is drawn by the same client
        code that draws a live one.

        The existence check is this agent's own, because ``session.resume`` is
        forgiving in a way the protocol is not: an unknown id makes it mint a
        fresh session and answer with that. Comparing the id it returns against
        the one asked for is what turns that into ``-32002``, and the distinction
        matters -- a client silently handed a new session shows a person an empty
        transcript for a conversation that had one.
        """
        self._refuse_per_session_mcp(params)
        cwd = self._validated_cwd(params.get("cwd"))
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpMethodError(protocol.INVALID_PARAMS, "sessionId is required", {"field": "sessionId"})

        result = await self._call("session.resume", {"session_id": session_id})
        if result.get("session_id") != session_id:
            raise AcpMethodError(protocol.RESOURCE_NOT_FOUND, "unknown session", {"sessionId": session_id})

        # The working directory comes from the client, not from what was stored:
        # a project moves, and the session's turns have to run where the editor
        # has it open now. Bound before the branch and not inside it, because it
        # is just as true of the second load as of the first -- and it is this
        # metadata, not ``AcpSession.cwd``, that ``WorkdirResolver`` reads when a
        # tool decides where to run.
        self._bind_workdir(session_id, cwd)
        session = self._translator.get(session_id)
        if session is None:
            session = AcpSession(
                session_id=session_id,
                session_key=session_id,
                cwd=cwd,
                subscription_id=await self._subscribe(session_id),
            )
            self._translator.add(session)
        else:
            # Loading a session this connection already holds. Its stream is
            # already live, so re-subscribing would leave two mappings to one
            # session and double every later frame.
            session.cwd = cwd

        updates = replay(result.get("messages"), session_id=session_id, cwd=cwd)
        for update in updates:
            self._emit(protocol.notification("session/update", {"sessionId": session_id, "update": update}))
        logger.info("acp: replayed {} update(s) for {}", len(updates), session_id)
        return {}

    async def _session_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """The sessions this agent can reopen, newest first.

        Read from the session manager rather than through ``session.list``, for
        one reason: ``SessionInfo.cwd`` is required by the schema and that
        method's wire shape does not carry it -- the directory lives in the
        stored metadata, which its mapper drops. Reaching past it avoids changing
        a shape the terminal client already consumes.

        A session with no recorded working directory is skipped rather than given
        a guessed one. The count is logged: a listing quietly shorter than the
        session directory is the kind of thing nobody notices until they go
        looking for a conversation.
        """
        wanted = params.get("cwd")
        entries = self._stored_sessions()
        out: list[dict[str, Any]] = []
        skipped = 0
        for entry in entries:
            metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
            key, workdir = entry.get("key"), metadata.get("workdir")
            if not isinstance(key, str) or not key or not isinstance(workdir, str) or not workdir:
                skipped += 1
                continue
            if isinstance(wanted, str) and wanted and workdir != wanted:
                continue
            info: dict[str, Any] = {"sessionId": key, "cwd": workdir}
            title = metadata.get("title")
            if isinstance(title, str) and title:
                info["title"] = title
            updated = entry.get("last_user_message_at") or entry.get("updated_at")
            if isinstance(updated, str) and updated:
                info["updatedAt"] = updated
            out.append(info)
        if skipped:
            logger.info("acp: {} stored session(s) have no recorded working directory and were omitted", skipped)
        # No ``nextCursor``: raven's listing is not paginated, so every session
        # that matched is in this response. The field is nullable, and omitting it
        # says "there is no more" rather than "ask again".
        return {"sessions": out}

    def _stored_sessions(self) -> list[dict[str, Any]]:
        """This channel's stored sessions, newest activity first.

        Sorted here because the manager returns them in directory order, and a
        picker that is not ordered by last activity is a picker nobody can find
        anything in. Without an engine there is no shared manager, and a fresh one
        caches nothing -- so the answer is an empty list rather than a listing
        built from an object that is about to be discarded.
        """
        if self._agent_loop is None:
            return []
        from raven.config import load_config
        from raven.rpc.methods.session import _manager_for

        try:
            entries = _manager_for(self._agent_loop, load_config()).list_sessions(channel=self._channel)
        except Exception:
            logger.exception("acp: listing stored sessions failed")
            return []
        entries = [entry for entry in entries if isinstance(entry, dict)]
        entries.sort(key=lambda e: str(e.get("last_user_message_at") or e.get("updated_at") or ""), reverse=True)
        return entries

    async def _set_config_option(self, params: dict[str, Any]) -> dict[str, Any]:
        """Apply one configuration option and answer with the full current set.

        The response carries every option, not just the one that changed, because
        the schema requires it -- and because applying one can change another's
        current value.

        The runtime's own refusals travel out with their codes intact: -32009 for
        a switch attempted during a turn is something a client can act on, and
        flattening it to an internal error would leave a person retrying a thing
        that will keep failing for a reason nobody told them.
        """
        session = self._session_for(params)
        config_id = params.get("configId")
        if config_id != MODEL_OPTION_ID:
            raise AcpMethodError(
                protocol.INVALID_PARAMS,
                f"unknown configuration option {config_id!r}",
                {"field": "configId", "supported": [MODEL_OPTION_ID]},
            )
        try:
            await set_model(self._call, session_id=session.session_key, value=params.get("value"))
        except ValueError as exc:
            raise AcpMethodError(protocol.INVALID_PARAMS, str(exc), {"field": "value"}) from exc
        return {"configOptions": await self._config_options()}

    async def _config_options(self) -> list[dict[str, Any]]:
        """Every configuration option this agent exposes, currently one."""
        option = await model_option(self._call)
        return [] if option is None else [option]

    async def _session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run one turn and answer with its stop reason.

        The answer comes from the translator's future, which the outbound event
        stream resolves -- so every update belonging to this turn is on the wire
        before this returns.
        """
        session = self._session_for(params)
        text, media, notes = self._read_prompt(params.get("prompt"))
        for note in notes:
            logger.info("acp: prompt content degraded: {}", note)
        if not text.strip() and not media:
            # Nothing to send. Answering end_turn is the honest reading: the
            # client asked for a turn on an empty prompt, and no turn ran.
            return {"stopReason": "end_turn"}

        if session.subscription_id is None:
            # The session's stream was closed out from under it (an overflow), so
            # it is bound to no live subscription. Re-subscribe before the turn
            # runs: a turn whose events have no subscriber left to deliver them is
            # a prompt that never answers.
            await self._rebind_subscription(session)

        try:
            future = self._translator.begin_turn(session.session_id)
        except TurnAlreadyRunningError as exc:
            raise AcpMethodError(
                protocol.INVALID_REQUEST,
                str(exc),
                {"sessionId": session.session_id},
            ) from exc
        try:
            try:
                accepted = await self._call(
                    "turn.send",
                    {"session_key": session.session_key, "content": text, "media": media},
                )
            except AcpMethodError as exc:
                # The turn never started, so no terminating event is coming and
                # awaiting the future would hang. Say why, then end the turn:
                # the rule is that a prompt is answered with a stopReason, and
                # that holds for a turn that was refused as much as for one that
                # ran.
                self._say(session, f"The turn could not start: {exc.message}")
                return {"stopReason": "end_turn"}
            # Which turn is this prompt's. The stream also carries turns the
            # runtime submitted, and without this their endings answer this
            # request -- the client is told the turn is over before its own turn
            # starts. ``turn.send`` is the only place that knows.
            # Always called, including with an empty id: the translator needs to
            # be told that no id is coming, or it holds every ending waiting for
            # one and the prompt is never answered.
            self._translator.accept_turn(
                session.session_id,
                str(accepted.get("turn_id") or "") if isinstance(accepted, dict) else "",
            )
            stop = await future
        finally:
            self._translator.end_turn(session.session_id)
        return {"stopReason": stop}

    async def _session_cancel(self, params: dict[str, Any]) -> None:
        """Cancel the session's turn, and make sure its prompt is answered.

        Order matters and follows the one measured to work: cancel the work
        first, then resolve the pending prompt -- last, so a late event cannot
        settle it with a different reason after the client has been told
        ``cancelled``. Resolving unconditionally rather than only when
        ``turn.cancel`` reports it cancelled something: a cancel that arrives in
        the window between ``begin_turn`` and the scheduler accepting the turn
        finds nothing to cancel, and the prompt still has to be answered.
        """
        session_id = str(params.get("sessionId") or "")
        session = self._translator.get(session_id)
        if session is None:
            # A notification for a session this connection does not have. Not an
            # error to report: notifications have no reply, and a client
            # cancelling a session it already dropped is tidy, not broken.
            logger.debug("acp: session/cancel for unknown session {}", session_id)
            return None
        try:
            await self._call("turn.cancel", {"session_key": session.session_key})
        finally:
            self._translator.settle_turn(session.session_id, "cancelled")
        return None

    # -- prompt content ---------------------------------------------------

    def _read_prompt(self, blocks: Any) -> tuple[str, list[str], list[str]]:
        """Flatten ACP content blocks into the text and media a turn takes.

        Returns the text, the media paths, and a list of notes about anything
        that was degraded, for the log. One unusable block never fails the
        prompt: a person who attached something odd should get an answer about
        the rest of what they said.
        """
        if not isinstance(blocks, list):
            raise AcpMethodError(protocol.INVALID_PARAMS, "prompt must be an array of content blocks")
        parts: list[str] = []
        media: list[str] = []
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
                # The block Zed sends for every @-mention of a file, and it is
                # gated by no capability at all -- PromptCapabilities covers only
                # audio and embeddedContext. An agent with no branch for it drops
                # the whole point of the mention.
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
                path, note = self._store_image_sync(block)
                if path:
                    media.append(path)
                    parts.append(f"[attached image: {path}]")
                if note:
                    notes.append(note)
                continue
            if kind == "audio":
                # promptCapabilities.audio is false, so this should not arrive.
                # Named rather than dropped: a person who spoke deserves to know
                # the words did not get through.
                parts.append("[an audio attachment was sent, which this agent cannot read]")
                notes.append("audio block received despite promptCapabilities.audio=false")
                continue
            notes.append(f"unknown prompt block type {kind!r}")
        return "\n\n".join(parts), media, notes

    @staticmethod
    def _resource_link(block: dict[str, Any]) -> str:
        """One line naming a linked resource, with a usable path when there is one.

        A ``file://`` URI is turned back into a path so the agent's own file
        tools can act on it; anything else is passed through as a URI, which
        ``web_fetch`` can take. Both are named in the prompt rather than read
        here: reading it would make an @-mention a silent file read, and the
        agent's read goes through the tool that reports it.
        """
        uri = block.get("uri")
        name = block.get("name")
        label = name if isinstance(name, str) and name else "resource"
        if not isinstance(uri, str) or not uri:
            return f"[{label}]"
        path = _file_uri_to_path(uri)
        return f"[{label}: {path or uri}]"

    @staticmethod
    def _embedded_resource(block: dict[str, Any]) -> tuple[str, str | None]:
        """Inline an embedded text resource; name a binary one.

        ``embeddedContext`` is declared true on the strength of the text case.
        The blob case has no honest inline form -- base64 in a prompt is tokens
        spent on nothing -- so it is named, and the note says so.
        """
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

    def _store_image_sync(self, block: dict[str, Any]) -> tuple[str | None, str | None]:
        """Decode an image block onto disk, returning a workspace-relative path.

        A path and not bytes because that is what a turn takes: ``turn.send``
        resolves media through the file tools' own policy, and the spelling it
        resolves is ``uploads/<name>`` -- which also survives a deployment with
        ``restrict_to_workspace`` on, where an absolute temp path outside the
        workspace would be dropped without a word.

        Written here rather than through ``fs.upload`` for one reason: this runs
        inside the frame handler and ``fs.upload`` is async, while every caller
        of this is inside a list comprehension over blocks. The size limit and
        the collision suffix are the parts that matter and both are kept.
        """
        raw = block.get("data")
        mime = block.get("mimeType")
        if not isinstance(raw, str) or not raw:
            return None, "an image block carried no data"
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return None, "an image block was not valid base64"
        # No "decoded to nothing" branch: with ``validate=True`` the only input
        # that yields empty bytes is the empty string, which the check above
        # already refused. Measured, because an unreachable guard reads as a case
        # somebody has handled.
        if len(data) > MAX_IMAGE_BYTES:
            return None, f"an image of {len(data)} bytes exceeds the {MAX_IMAGE_BYTES} byte limit"
        suffix = mimetypes.guess_extension(mime) if isinstance(mime, str) else None
        try:
            return _write_upload(data, suffix or ".bin"), None
        except Exception as exc:
            # Every failure shape, on purpose, and the same reasoning
            # ``turn.send``'s own attachment resolver uses: an unwritable
            # workspace (OSError), an unreadable config (anything), a name the
            # filesystem refuses. One bad attachment must not cost the turn the
            # rest of the prompt was asking for.
            return None, f"an image could not be written: {exc}"

    # -- plumbing ---------------------------------------------------------

    def _session_for(self, params: dict[str, Any]) -> AcpSession:
        session_id = params.get("sessionId")
        session = self._translator.get(session_id) if isinstance(session_id, str) else None
        if session is None:
            # -32002, the code the spec assigns to a session that does not
            # exist, and not a silent fresh session: a client that reopens a
            # session raven has lost must be told, or it shows a person an empty
            # transcript for a conversation that had one.
            raise AcpMethodError(
                protocol.RESOURCE_NOT_FOUND,
                "unknown session",
                {"sessionId": session_id},
            )
        return session

    @staticmethod
    def _refuse_per_session_mcp(params: dict[str, Any]) -> None:
        """Refuse a non-empty ``mcpServers``, rather than ignoring it.

        MCP is connected once per process, lazily, and nothing scopes a server to
        one session -- so accepting the field would leave a client believing its
        tools are available for the rest of the session. Required by the schema
        on both ``session/new`` and ``session/load``, and an empty array is the
        normal value.
        """
        servers = params.get("mcpServers")
        if isinstance(servers, list) and servers:
            raise AcpMethodError(
                protocol.INVALID_PARAMS,
                "per-session MCP servers are not supported; configure MCP servers in raven's own config",
                {"field": "mcpServers", "count": len(servers)},
            )

    def _validated_cwd(self, raw: Any) -> str:
        """The client's working directory, checked the way raven checks its own.

        ``validate_override`` refuses a relative path, agent home, any ancestor
        of it, and the memory / skills / sessions subtrees -- the last because
        the per-turn checkpoint runs ``add -A`` over the working directory, so a
        session rooted at ``~`` would commit provider keys into a shadow git
        repo. Its refusals arrive as ``ValueError``; a bare Python exception is
        not an acceptable handshake failure, so each becomes -32602 with the
        reason attached.
        """
        from raven.agent.workdir import validate_override
        from raven.config import load_config

        if not isinstance(raw, str) or not raw.strip():
            raise AcpMethodError(protocol.INVALID_PARAMS, "cwd is required and must be a string", {"field": "cwd"})
        try:
            return str(validate_override(raw, load_config().workspace_path))
        except ValueError as exc:
            raise AcpMethodError(
                protocol.INVALID_PARAMS, f"cwd is not usable: {exc}", {"field": "cwd", "cwd": raw}
            ) from exc

    def _bind_workdir(self, session_key: str, cwd: str) -> None:
        """Pin the session's turns to ``cwd`` the way ``session.create`` does.

        The same metadata key ``WorkdirResolver`` already honours, set on the
        cached session and persisted with its first save -- as lazy as the mint
        itself, so a client that opens a session and says nothing writes no file.

        It has to be *the engine's* manager. ``_manager_for(None, config)`` builds
        a fresh ``SessionManager`` every call and caches nothing, so writing the
        metadata through one would write it into an object discarded on the next
        line -- and the session would silently run in the wrong directory. Without
        an engine there is nothing to pin to, and the turn is going to fail on the
        build error anyway; said out loud rather than dropped, because "the agent
        edited the wrong tree" is not a failure anyone would trace back to here.
        """
        from raven.config import load_config
        from raven.rpc.methods.session import _manager_for

        if self._agent_loop is None:
            logger.warning("acp: no engine, so {} cannot be pinned to {}", session_key, cwd)
            return
        _manager_for(self._agent_loop, load_config()).get_or_create(session_key).metadata["workdir"] = cwd

    async def unsubscribe_all(self) -> None:
        """Close every subscription this connection opened.

        The symmetric half of ``_subscribe``, and it is not merely tidy: each
        subscription owns an ``asyncio`` task running a coalesce loop, and
        ``build_rpc_stack``'s teardown does not touch the emitter. Left open, they
        are reported at interpreter exit as "Task was destroyed but it is
        pending!" -- on stderr, which is the stream an ACP client shows to the
        person who just closed a window.

        Failures are swallowed per session rather than allowed to abort the sweep:
        this runs on the way out, and one unclosed subscription must not cost the
        rest of the shutdown.
        """
        for session in self._translator.sessions():
            if not session.subscription_id:
                continue
            try:
                await self._call("turn.unsubscribe", {"subscription_id": session.subscription_id})
            except Exception as exc:
                logger.debug("acp: closing subscription for {} failed: {}", session.session_id, exc)

    async def _subscribe(self, session_key: str) -> str:
        result = await self._call("turn.subscribe", {"session_key": session_key})
        subscription_id = result.get("subscription_id")
        if not isinstance(subscription_id, str) or not subscription_id:
            raise AcpMethodError(protocol.INTERNAL_ERROR, "turn.subscribe returned no subscription")
        return subscription_id

    async def _rebind_subscription(self, session: AcpSession) -> None:
        """Open a fresh subscription for a session whose stream died.

        The emitter closed the old subscription when it overflowed, so the session
        is bound to nothing. This subscribes again and points the translator's
        binding at the new stream.
        """
        subscription_id = await self._subscribe(session.session_key)
        self._translator.bind_subscription(session.session_id, subscription_id)

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Invoke a registered RPC method, raising its error as an ACP error.

        The dispatcher answers with a frame rather than by raising, so the error
        has to be unpacked. Its code is carried through unchanged: -32003 (a turn
        already running) and -32008 (no model available) mean something a client
        can act on, and flattening them to -32603 would throw that away.
        """
        frame = protocol.request(next(self._ids), method, params)
        response = await self._dispatcher.dispatch(frame)
        error = response.get("error") if isinstance(response, dict) else None
        if error:
            raise AcpMethodError(
                int(error.get("code", protocol.INTERNAL_ERROR)),
                str(error.get("message", method + " failed")),
                error.get("data"),
            )
        result = response.get("result") if isinstance(response, dict) else None
        return result if isinstance(result, dict) else {}

    def _say(self, session: AcpSession, text: str) -> None:
        """Put one line of agent message on the wire for this session."""
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


# Matches ``fs.upload``'s own ceiling rather than inventing a second one: an
# image pasted into an editor and an image dropped into the web page are the
# same file arriving by two roads.
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _new_chat_id() -> str:
    from raven.session.manager import new_chat_id

    return new_chat_id()


def _write_upload(data: bytes, suffix: str) -> str:
    """Store bytes under ``<workspace>/uploads`` and return the relative path."""
    from raven.config import load_config
    from raven.session.manager import new_chat_id

    root = load_config().workspace_path / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    # The minted id rather than a client-supplied name: the block carries no
    # filename, and a name derived from the mime type alone would collide on the
    # second paste.
    target = root / f"acp-{new_chat_id()}{suffix}"
    target.write_bytes(data)
    return f"uploads/{target.name}"


def _file_uri_to_path(uri: str) -> str | None:
    """The filesystem path behind a ``file://`` URI, or ``None``.

    Percent-decoded, because an editor encodes spaces, and restricted to a local
    URI: ``file://host/share`` names somebody else's machine, and turning it
    into a local path would point the agent at the wrong file rather than at
    none.
    """
    if not uri.startswith("file://"):
        return None
    try:
        parsed = urlparse(uri)
    except ValueError:
        # ``urlparse`` raises on a bracketed host that is not a valid IPv6
        # literal. A URI that cannot be parsed is one whose path cannot be
        # trusted, so it is passed through as text rather than guessed at.
        return None
    if parsed.netloc and parsed.netloc != "localhost":
        return None
    path = unquote(parsed.path)
    return path or None


__all__ = [
    "IGNORED_NOTIFICATIONS",
    "MAX_IMAGE_BYTES",
    "UNIMPLEMENTED_METHODS",
    "AcpMethodError",
    "AcpMethods",
]
