"""The ACP methods this agent answers.

Two rules run through the whole file.

* **Tolerant inbound.** Unknown params are ignored, a wrongly-typed
  ``protocolVersion`` is read for intent, and an unknown method is *answered*
  rather than dropped. The spec asks for this, and the failure it prevents is the
  worst one available: a client left waiting on a promise nothing will resolve.
* **A prompt is never answered with a JSON-RPC error.** Whatever happens to the
  turn -- refused before it started, failed halfway, cancelled, given no material
  to work from -- the client gets a ``stopReason``, with the explanation as
  message content. Measured from the client side of this protocol: an error in
  reply to a turn-shaped request makes clients tear down the whole turn.

The one exception to the second rule is a prompt naming a session this connection
does not have, which is ``-32002`` by the spec's own error table. A silent fresh
session instead would show a person an empty transcript for a conversation that
had one.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from loguru import logger

from raven.acp import materials, protocol
from raven.acp.capabilities import ClientCapabilities, initialize_result
from raven.acp.session import AcpSession, SessionTable, TurnAlreadyRunningError
from raven.acp.spine import Frames
from raven.spine import ChatType, Origin, Source, TurnRequest

# Methods in the stable manifest that are not served here. Answered with
# method-not-found, the same answer an unknown name gets; the distinction is kept
# so a reader can see the difference between "not in the protocol" and "declared
# unsupported". Each one is false in ``agent_capabilities`` as well -- a client
# that reads the handshake never sends these at all.
UNIMPLEMENTED_METHODS = frozenset(
    {
        "session/load",
        "session/resume",
        "session/list",
        "session/delete",
        "session/set_mode",
        "session/set_config_option",
        "logout",
    }
)

# Notifications that are safe to receive and correct to ignore. ``$/cancel_request``
# is protocol-level and explicitly optional: the spec says a receiver MAY act on
# it, and a request it would have cancelled is answered -32800 by whoever owns
# that request rather than here.
IGNORED_NOTIFICATIONS = frozenset({"$/cancel_request"})


class AcpMethodError(Exception):
    """A JSON-RPC error to answer one request with."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


EngineFactory = Callable[[AcpSession], Awaitable[Any]]
"""Builds one session's engine. Awaited inside ``session/new``.

A seam rather than an inline call so a test can drive the whole method surface
against a scripted engine instead of standing up a provider, a plugin registry
and a memory backend to exchange four frames. It is also why the engine build is
inside ``session/new`` at all: the host bounds that call by ``readyTimeoutMs``,
which is the only budget on the wire big enough to hold it.
"""


class AcpMethods:
    """Answers inbound ACP frames for one connection."""

    def __init__(
        self,
        *,
        emit: Frames,
        sessions: SessionTable,
        engine_factory: EngineFactory,
        jobs_root: Path,
        channel: str = "acp",
    ) -> None:
        self._emit = emit
        self._sessions = sessions
        self._engine_factory = engine_factory
        self._jobs_root = jobs_root
        self._channel = channel
        self.initialized = False
        self.client = ClientCapabilities()

    # -- frame handling ---------------------------------------------------

    async def handle(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one inbound frame, or return ``None`` to stay silent.

        The request/notification split is on the *presence* of ``id``, not on its
        truthiness. JSON-RPC says a notification is a frame with no ``id`` member,
        so ``{"id": 0, ...}`` and ``{"id": null, ...}`` are requests -- and a
        request that goes unanswered because its id happened to be falsy is a hang
        with no diagnostic.
        """
        if "method" not in frame:
            # A response to something this agent asked. Nothing here asks: no
            # permission requests, no elicitation. So a response can only be a
            # client answering something it invented.
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
            # JSON-RPC and unusable here, so say so rather than silently reading it
            # as absent.
            if not is_request:
                return None
            return protocol.error_response(request_id, protocol.INVALID_PARAMS, f"{method} params must be an object")

        try:
            result = await self._route(method, params or {})
        except AcpMethodError as exc:
            if not is_request:
                logger.debug("acp: notification {} failed: {}", method, exc.message)
                return None
            return protocol.error_response(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:
            # The connection outlives one bad request. Without this the read loop
            # dies on a handler bug and the client sees the agent vanish mid-turn,
            # which is indistinguishable from a crash.
            logger.exception("acp: {} raised", method)
            if not is_request:
                return None
            return protocol.error_response(
                request_id, protocol.INTERNAL_ERROR, f"{method} failed", {"reason": str(exc)[:400]}
            )
        if not is_request:
            return None
        return protocol.result_response(request_id, result if result is not None else {})

    async def _route(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method in IGNORED_NOTIFICATIONS:
            return None
        if not self.initialized:
            # Nothing before the handshake, including session/new. Two of three
            # servers measured from the client side tolerate a session/new without
            # one and simply work; codex-acp answers -32603, which is the correct
            # reading of the protocol and the one to build against.
            raise AcpMethodError(
                protocol.INVALID_REQUEST,
                "initialize must be called before any other method",
                {"method": method},
            )
        if method == "authenticate":
            # authMethods is empty, which is a statement that none is needed. A
            # client calling this anyway is told what it declared, not given a
            # method-not-found it would read as a version mismatch.
            raise AcpMethodError(
                protocol.INVALID_PARAMS,
                "this agent advertises no authentication methods",
                {"authMethods": []},
            )
        if method == "session/new":
            return await self._session_new(params)
        if method == "session/prompt":
            return await self._session_prompt(params)
        if method == "session/cancel":
            return await self._session_cancel(params)
        if method == "session/close":
            return await self._session_close(params)
        if method in UNIMPLEMENTED_METHODS:
            raise AcpMethodError(protocol.METHOD_NOT_FOUND, f"{method} is not supported by this agent")
        raise AcpMethodError(protocol.METHOD_NOT_FOUND, f"unknown method {method}")

    # -- handlers ---------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer the handshake and keep what the client declared.

        Re-initialising is allowed rather than refused: the state it replaces is
        only the capability record, and refusing would strand a client whose first
        attempt raced its own setup.
        """
        self.client = ClientCapabilities.from_params(params)
        self.initialized = True
        return initialize_result(params)

    async def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        """Mint a session, give it a job directory, and build its engine."""
        self._refuse_per_session_mcp(params)
        cwd = self._validated_cwd(params.get("cwd"))
        chat_id = _new_chat_id()
        session = AcpSession(
            session_id=f"{self._channel}:{chat_id}",
            cwd=cwd,
            root=self._jobs_root / chat_id,
        )
        session.ensure_dirs()
        # Registered before the engine is built, because the engine's outlet
        # resolves every frame through this table: an event emitted during the
        # build -- an MCP status line, a plugin notice -- would otherwise be
        # dropped as belonging to no session.
        self._sessions.add(session)
        try:
            session.engine = await self._engine_factory(session)
        except Exception as exc:
            # A session with no engine can never run a turn, so it must not be
            # left on the table for a later prompt to find. The build error is the
            # one thing a client can act on, so it goes out with its reason.
            await self._sessions.release(session.session_id)
            logger.exception("acp: building the engine for {} failed", session.session_id)
            raise AcpMethodError(
                protocol.INTERNAL_ERROR,
                "the agent could not be started for this session",
                {"reason": str(exc)[:400]},
            ) from exc
        logger.info("acp: session {} created; job {} delivering to {}", session.session_id, session.root, cwd)
        # The ACP sessionId IS the raven session key: spine addresses a turn's
        # events by conversation id and every session/update frame is addressed by
        # sessionId, so a second id space would need a map that buys nothing.
        return {"sessionId": session.session_id}

    async def _session_close(self, params: dict[str, Any]) -> dict[str, Any]:
        """Drop one session: cancel its work, release its engine.

        Per the spec, closing is a cancel plus a resource release. The job
        directory stays on disk -- closing says "I am done with this stream", not
        that the deck is gone.
        """
        session = self._session_for(params)
        engine = session.engine
        if session.turn is not None and engine is not None:
            with contextlib.suppress(Exception):
                engine.scheduler.cancel_conversation(session.session_id)
        await self._sessions.release(session.session_id)
        return {}

    async def _session_cancel(self, params: dict[str, Any]) -> None:
        """Cancel the session's turn, and make sure its prompt is answered.

        Order matters: cancel the work first, then resolve the pending prompt --
        last, so a late event cannot settle it with a different reason after the
        client has been told ``cancelled``. Resolved unconditionally rather than
        only when something was actually cancelled: a cancel that arrives between
        ``begin_turn`` and the scheduler accepting the turn finds nothing to
        cancel, and the prompt still has to be answered.
        """
        session_id = str(params.get("sessionId") or "")
        session = self._sessions.get(session_id)
        if session is None:
            # A notification for a session this connection does not have. Not an
            # error to report -- notifications have no reply, and a client
            # cancelling a session it already dropped is tidy, not broken.
            logger.debug("acp: session/cancel for unknown session {}", session_id)
            return None
        try:
            if session.engine is not None:
                session.engine.scheduler.cancel_conversation(session.session_id)
        finally:
            session.settle("cancelled")
        return None

    async def _session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run one turn and answer with its stop reason."""
        session = self._session_for(params)
        text, notes = self._read_prompt(params.get("prompt"))
        for note in notes:
            logger.info("acp: prompt content degraded: {}", note)
        outlet = session.engine.outlet
        if not text.strip():
            # Nothing to send. ``end_turn`` is the honest reading: the client asked
            # for a turn on an empty prompt, and no turn ran.
            return {"stopReason": "end_turn"}

        try:
            staged = self._stage_for(session, text)
        except materials.StagingError as exc:
            # Fatal to the turn, not skipped: a deck built from part of its
            # material is wrong in a way nothing downstream can see. Reported as
            # message content because a prompt is never answered with an error.
            outlet.say(session.session_id, f"The material could not be staged. {exc}")
            return {"stopReason": "end_turn"}
        if not session.staged:
            outlet.say(
                session.session_id,
                "No source material. This agent builds decks from documents you name, so open the task "
                'with a fenced block declaring them:\n```raven-ppt\n{"materials": ["/abs/notes.md"]}\n```',
            )
            return {"stopReason": "end_turn"}
        if staged:
            text += materials.describe(session.staged, session.materials, session.out, session.template)

        # Taken before the turn starts, so what this turn publishes can be told
        # apart from what an earlier turn of the same session left in the job.
        before = materials.deck_mtimes(session.out)
        try:
            future = session.begin_turn()
        except TurnAlreadyRunningError as exc:
            raise AcpMethodError(protocol.INVALID_REQUEST, str(exc), {"sessionId": session.session_id}) from exc
        try:
            session.engine.scheduler.submit(
                TurnRequest(
                    origin=Origin.USER,
                    source=Source(
                        channel=self._channel,
                        chat_id=session.session_id.split(":", 1)[-1],
                        sender_id="client",
                        chat_type=ChatType.DM,
                    ),
                    text=text,
                    conversation=session.session_id,
                )
            )
            # The sink resolves this after the render barrier, so every
            # session/update belonging to the turn is on the wire before the
            # client is told the turn is over.
            stop = await future
        finally:
            session.end_turn()

        if stop == "end_turn":
            # Only a turn that ran to its own end has a deck worth announcing. A
            # cancelled one left a half-written render behind, and the reference
            # implementation's rule applies to the report as much as to the reply:
            # every stop reason other than end_turn means what is there is partial.
            # ``outlet`` is the one captured above rather than re-read off the
            # session: a connection shutting down releases the session while this
            # handler is still suspended, so ``session.engine`` may be gone by now.
            self._report_deck(session, outlet, before)
        return {"stopReason": stop}

    # -- material and deck ------------------------------------------------

    def _stage_for(self, session: AcpSession, text: str) -> list[tuple[str, Path]]:
        """Copy anything this prompt names that the session does not already hold.

        Returns only the newly staged pairs; ``session.staged`` accumulates. A
        source already staged is skipped rather than copied again under a
        collision-suffixed name, which would have the prompt list one document
        twice and the deck ground twice in it.
        """
        declared, declared_template = materials.inputs_from_prompt(text)
        wanted = materials.unique_sources(
            declared + ([declared_template] if declared_template else []) + materials.materials_from_prompt(text)
        )
        held = {os.path.realpath(source) for source, _ in session.staged}
        fresh = [source for source in wanted if os.path.realpath(source) not in held]
        staged = materials.stage(session.materials, fresh, session.taken)
        session.staged.extend(staged)
        if declared_template:
            wanted_real = os.path.realpath(declared_template)
            session.template = next(
                (target for source, target in session.staged if os.path.realpath(source) == wanted_real),
                session.template,
            )
        return staged

    def _report_deck(self, session: AcpSession, outlet: Any, before: dict[Path, float]) -> None:
        """Say what this turn published, as text and as a typed resource.

        The text lines are the same three the launcher printed, and they are not
        cosmetic: a raven host builds the sub-agent's reply out of
        ``agent_message_chunk`` text alone, so a deck announced only as a
        ``resource_link`` would reach the model as nothing.

        A turn that published no deck says nothing, unlike the launcher's
        unconditional ``FAILED`` line -- on this transport the model's own reply is
        already on the wire, and a follow-up turn that only answered a question did
        not fail. The one case still worth naming is a turn that *claimed* a deck
        and produced none, which is exactly what the launcher's check was for.
        """
        deck, slides = materials.verified_deck(session.out, session.reply_text(), before)
        if deck is None:
            if "MEDIA:" in session.reply_text():
                outlet.say(
                    session.session_id,
                    f"No verifiable deck was published: nothing under {session.out} opens as a "
                    "presentation carrying slides.",
                )
            return
        delivered = materials.deliver(deck, session.cwd)
        outlet.say(
            session.session_id,
            f"\n\nPublished a {slides}-slide deck.\nDeck: {deck}\nMEDIA: {delivered or deck}",
        )
        outlet.send_media(session.session_id, str(delivered or deck), mime=materials.PPTX_MIME)

    # -- prompt content ---------------------------------------------------

    def _read_prompt(self, blocks: Any) -> tuple[str, list[str]]:
        """Flatten ACP content blocks into the text a turn takes.

        Returns the text and a list of notes about anything degraded, for the log.
        One unusable block never fails the prompt: a person who attached something
        odd should get an answer about the rest of what they said.

        A raven host sends exactly one ``text`` block, so that is the branch that
        runs in practice. The other two are here because they are how ACP names
        material and cost one branch each.
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
                value = block.get("text")
                if isinstance(value, str) and value:
                    parts.append(value)
                continue
            if kind == "resource_link":
                # The block an editor sends for an @-mention of a file, gated by no
                # capability at all -- promptCapabilities covers only image, audio
                # and embeddedContext. An agent with no branch for it drops the
                # whole point of the mention. Named in the prompt rather than read
                # here: the path lands in the text, where the staging scan picks it
                # up like any other named source.
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
                # promptCapabilities.image is false, so this should not arrive.
                # Named rather than dropped: a person who attached a figure
                # deserves to know it did not get through, and the reply says how
                # to send one instead.
                parts.append(
                    "[an image was attached inline, which this agent cannot read; "
                    "name the file by absolute path instead]"
                )
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
        """One line naming a linked resource, with a usable path when there is one.

        A ``file://`` URI is turned back into a path so the staging scan can act on
        it; anything else is passed through as a URI, which the fetch tool can
        take.
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

        ``embeddedContext`` is declared true on the strength of the text case. The
        blob case has no honest inline form -- base64 in a prompt is tokens spent
        on nothing -- so it is named, and the note says so.
        """
        resource = block.get("resource")
        if not isinstance(resource, dict):
            return "", "an embedded resource block carried no resource"
        uri = resource.get("uri") if isinstance(resource.get("uri"), str) else ""
        text = resource.get("text")
        label = _file_uri_to_path(uri) or uri or "embedded resource"
        if isinstance(text, str):
            return f"[{label}]\n{text}", None
        if "blob" in resource:
            return f"[binary resource, not inlined: {label}]", f"blob resource {label} was named, not inlined"
        return "", "an embedded resource had neither text nor blob"

    # -- plumbing ---------------------------------------------------------

    def _session_for(self, params: dict[str, Any]) -> AcpSession:
        session = self._sessions.get(params.get("sessionId"))
        if session is None:
            raise AcpMethodError(protocol.RESOURCE_NOT_FOUND, "unknown session", {"sessionId": params.get("sessionId")})
        if session.engine is None:
            # Reachable only for a session whose engine build failed and whose
            # release then raced this call. Refused rather than dereferenced, so
            # the client gets an error instead of the handler an AttributeError.
            raise AcpMethodError(
                protocol.INTERNAL_ERROR, "this session has no engine", {"sessionId": session.session_id}
            )
        return session

    @staticmethod
    def _refuse_per_session_mcp(params: dict[str, Any]) -> None:
        """Refuse a non-empty ``mcpServers`` rather than ignoring it.

        MCP is connected once per engine and nothing scopes a server to one
        session, so accepting the field would leave a client believing its tools
        are available for the rest of the session. Required by the schema on
        ``session/new``, and an empty array is the normal value.
        """
        servers = params.get("mcpServers")
        if isinstance(servers, list) and servers:
            raise AcpMethodError(
                protocol.INVALID_PARAMS,
                "per-session MCP servers are not supported; configure MCP servers in this agent's own config",
                {"field": "mcpServers", "count": len(servers)},
            )

    @staticmethod
    def _validated_cwd(raw: Any) -> str:
        """The client's working directory, checked as far as it is used.

        Narrower than the host's own validator, and deliberately: ``cwd`` here is
        only where the finished deck is delivered. The engine's workspace is the
        session's job directory, and this surface runs non-interactive, so the
        per-turn git checkpoint that makes an unwise ``cwd`` dangerous over there
        never runs.
        """
        if not isinstance(raw, str) or not raw.strip():
            raise AcpMethodError(protocol.INVALID_PARAMS, "cwd is required and must be a string", {"field": "cwd"})
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise AcpMethodError(protocol.INVALID_PARAMS, "cwd must be an absolute path", {"field": "cwd", "cwd": raw})
        if not path.is_dir():
            raise AcpMethodError(
                protocol.INVALID_PARAMS, "cwd is not an existing directory", {"field": "cwd", "cwd": raw}
            )
        return str(path)


def _new_chat_id() -> str:
    from raven.session.manager import new_chat_id

    return new_chat_id()


def _file_uri_to_path(uri: str) -> str | None:
    """The filesystem path behind a ``file://`` URI, or ``None``.

    Percent-decoded, because an editor encodes spaces, and restricted to a local
    URI: ``file://host/share`` names somebody else's machine, and turning it into
    a local path would point the agent at the wrong file rather than at none.
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
    return unquote(parsed.path) or None


__all__ = [
    "IGNORED_NOTIFICATIONS",
    "UNIMPLEMENTED_METHODS",
    "AcpMethodError",
    "AcpMethods",
    "EngineFactory",
]
