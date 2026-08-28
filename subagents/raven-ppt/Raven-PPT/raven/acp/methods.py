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
from raven.acp.outbound import OutboundRequests
from raven.acp.questions import AcpQuestions
from raven.acp.redact import redact, redact_value
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
        "session/list",
        "session/delete",
        "session/set_mode",
        "session/set_config_option",
        "logout",
    }
)

# Which stored roles a replay puts back on the wire, and as what. Only the two a
# person reads: a tool call has update kinds of its own that carry ids this
# connection never issued, and replaying one would name a call the client cannot
# match to anything.
_REPLAY_KINDS = {"user": "user_message_chunk", "assistant": "agent_message_chunk"}

# Notifications that are safe to receive and correct to ignore. ``$/cancel_request``
# is protocol-level and explicitly optional: the spec says a receiver MAY act on
# it, and a request it would have cancelled is answered -32800 by whoever owns
# that request rather than here.
IGNORED_NOTIFICATIONS = frozenset({"$/cancel_request"})

# Keys an internal error's data may carry that must not leave the process: a
# traceback tail names the filesystem it ran on and sometimes argument values.
_PRIVATE_ERROR_KEYS = frozenset({"traceback_tail", "traceback", "stack"})


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
        outbound: OutboundRequests | None = None,
        questions: AcpQuestions | None = None,
    ) -> None:
        self._emit = emit
        self._sessions = sessions
        self._engine_factory = engine_factory
        self._jobs_root = jobs_root
        self._channel = channel
        # Optional so a test can drive the method surface without the outbound
        # half. Absent, a response frame belongs to nobody and a question has
        # nowhere to go -- which is what this surface did before either existed.
        self._outbound = outbound
        self._questions = questions
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
            # A response to something this agent asked. Matched against the
            # outbound table first: an unmatched one can only be a client
            # answering something it invented, or one this side already timed out.
            if self._outbound is not None and self._outbound.resolve(frame):
                return None
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
            return protocol.error_response(request_id, exc.code, redact(exc.message), sanitise_error_data(exc.data))
        except Exception as exc:
            # The connection outlives one bad request. Without this the read loop
            # dies on a handler bug and the client sees the agent vanish mid-turn,
            # which is indistinguishable from a crash.
            logger.exception("acp: {} raised", method)
            if not is_request:
                return None
            # Scanned before it is clipped: clipping first can cut a credential in
            # half, and the head of it then reads as ordinary text.
            return protocol.error_response(
                request_id, protocol.INTERNAL_ERROR, f"{method} failed", {"reason": redact(str(exc))[:400]}
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
        if method == "session/load":
            return await self._session_load(params)
        if method == "session/resume":
            return await self._session_resume(params)
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
        if self._questions is not None:
            self._questions.set_client(self.client)
        self.initialized = True
        return initialize_result(params)

    async def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        """Mint a session, give it a job directory, and build its engine.

        A ``sessionId`` the client hands back is honoured when its job directory
        is still on disk. Minting unconditionally is what made every call to the
        same instance a fresh conversation: the id is the raven session key, the
        key is the transcript's filename, and a new key reads an empty history
        beside the previous one rather than continuing it. A client that sends
        nothing, or names a session this machine no longer holds, still gets a
        new one -- resuming is an offer, not a precondition.

        A returning id that is still *open* on this connection is answered with
        that session, the way ``session/load`` answers one. Constructing a
        replacement would overwrite the table entry, and the table is the only
        handle on an engine.
        """
        self._refuse_per_session_mcp(params)
        cwd = self._validated_cwd(params.get("cwd"))
        returning = params.get("sessionId")
        if (open_now := self._reuse_open(returning, cwd)) is not None:
            return {"sessionId": open_now.session_id}
        session = await self._open_session(self._resumable_chat_id(returning) or _new_chat_id(), cwd)
        logger.info("acp: session {} created; job {} delivering to {}", session.session_id, session.root, cwd)
        # The ACP sessionId IS the raven session key: spine addresses a turn's
        # events by conversation id and every session/update frame is addressed by
        # sessionId, so a second id space would need a map that buys nothing.
        return {"sessionId": session.session_id}

    async def _session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        """Reopen a session this machine still holds, and replay its transcript.

        Two halves, and only the first one the model cares about: rebuilding the
        session under its old id and root is what puts the history back in front
        of the model, because the loop reads it from disk by that key. The replay
        is for the person -- the spec has a loaded session emit its past turns as
        ``session/update`` notifications, so a client that reopens a conversation
        renders it instead of showing an empty pane above a live prompt.

        A session already open on this connection is answered without rebuilding
        it: a client that loads twice has one engine, not two on one job.
        """
        session = await self._reopen(params, "loaded")
        self._replay(session)
        return {}

    async def _session_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        """Reopen a held session without replaying it.

        The distinction is the spec's and it is the whole of the method:
        ``session/load`` returns the transcript as ``session/update`` frames so a
        client that lost its history can repaint it, while ``session/resume``
        continues a session the client still has on screen -- so the transcript
        stays where it is and the session becomes promptable again. The state
        both establish is identical, which is why both go through ``_reopen``,
        and both refuse an unknown id the same way: a client silently handed a
        new id would prompt an empty conversation for one that had history.

        Declared as a capability rather than served quietly: the consuming raven
        reads ``sessionCapabilities.resume`` for statefulness and will not even
        look up a stored id without it, so the key and this method are one change.
        """
        await self._reopen(params, "resumed")
        return {}

    async def _reopen(self, params: dict[str, Any], verb: str) -> AcpSession:
        """The state a reopened session needs, for whichever method asked.

        Returns the session rather than a result, because what differs between
        ``load`` and ``resume`` is only what is sent afterwards.
        """
        self._refuse_per_session_mcp(params)
        cwd = self._validated_cwd(params.get("cwd"))
        session_id = params.get("sessionId")
        if (open_now := self._reuse_open(session_id, cwd)) is not None:
            return open_now

        chat_id = self._resumable_chat_id(session_id)
        if chat_id is None:
            # Named rather than silently minted: a client asked for a specific
            # conversation, and handing it a different empty one under the same
            # promise is the failure the spec's own error table exists for.
            raise AcpMethodError(
                protocol.RESOURCE_NOT_FOUND,
                "no session by that id is held on this machine",
                {"sessionId": session_id},
            )

        session = await self._open_session(chat_id, cwd)
        logger.info("acp: session {} {}; job {} delivering to {}", session.session_id, verb, session.root, cwd)
        return session

    def _reuse_open(self, session_id: Any, cwd: str) -> AcpSession | None:
        """The session this connection already holds under ``session_id``, if any.

        Every entry point that accepts a returning id answers through here, and
        for one reason rather than two: ``SessionTable.add`` overwrites, and the
        table is the only handle on an engine. A replacement entry would leave the
        former engine running on the same job directory with no route to a
        teardown -- not ``session/close``, which looks the id up, and not the
        connection teardown, which walks the table.

        The working directory is still taken, because it is the client's and this
        call is where it says so.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.cwd = cwd
        return session

    async def _open_session(self, chat_id: str, cwd: str) -> AcpSession:
        """A session on ``chat_id``'s job directory, engine built and registered.

        One path for every method that opens a session, so a reopened session
        cannot reach a different state than a new one on the same job would: the
        job root outlives the process that staged into it, and a follow-up prompt
        reads the staging bookkeeping that ``materials.rehydrate`` recovers here.
        """
        session = AcpSession(session_id=f"{self._channel}:{chat_id}", cwd=cwd, root=self._jobs_root / chat_id)
        session.ensure_dirs()
        session.staged, session.taken = materials.rehydrate(session.materials)
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
                {"reason": redact(str(exc))[:400]},
            ) from exc
        return session

    def _replay(self, session: AcpSession) -> None:
        """Put the stored turns of ``session`` on the wire, oldest first.

        Read through ``SessionManager`` rather than off the file, so the one
        reader that knows the on-disk shape stays the only one. Best-effort by
        construction: a transcript that cannot be read costs the person their
        scrollback, and failing the load over it would cost them the session.
        """
        try:
            from raven.session.manager import SessionManager

            stored = SessionManager(session.root).get_or_create(session.session_id)
            messages = list(stored.messages)
        except Exception:
            logger.exception("acp: replaying {} failed", session.session_id)
            return
        for message in messages:
            kind = _REPLAY_KINDS.get(message.get("role"))
            text = message.get("content")
            # Only the two roles a person reads. A tool call replayed as prose
            # would put a wall of JSON in the pane, and its own update kinds carry
            # ids this connection never issued.
            if kind is None or not isinstance(text, str) or not text.strip():
                continue
            # A stored turn is republished into a transcript the client keeps, so
            # it is the same publishing surface a live chunk is -- and it is worse
            # by one degree, because a credential a turn quoted months ago is
            # replayed by every reopen from here on.
            self._emit(
                protocol.session_update(
                    session.session_id,
                    {"sessionUpdate": kind, "content": {"type": "text", "text": redact(text)}},
                )
            )

    def _resumable_chat_id(self, session_id: Any) -> str | None:
        """The chat id inside ``session_id`` when this machine holds its job.

        ``None`` for anything that is not a session this agent could have minted:
        a non-string, another channel's key, or a chat id that is not one path
        segment -- ``..`` and a separator both reach outside the jobs root, and
        the id arrives from the far side of a socket.
        """
        if not isinstance(session_id, str):
            return None
        channel, sep, chat_id = session_id.partition(":")
        if not sep or channel != self._channel or not chat_id:
            return None
        if chat_id != Path(chat_id).name or chat_id in {os.curdir, os.pardir}:
            return None
        root = self._jobs_root / chat_id
        return chat_id if root.is_dir() else None

    async def _session_close(self, params: dict[str, Any]) -> dict[str, Any]:
        """Drop one session: cancel its work, release its engine.

        Per the spec, closing is a cancel plus a resource release. The job
        directory stays on disk -- closing says "I am done with this stream", not
        that the deck is gone.
        """
        session = self._session_for(params)
        engine = session.engine
        if self._questions is not None:
            # Outside the turn guard above, and before the release: the ask runs on
            # a task of its own, so it outlives the turn that started it and the
            # session it belongs to. Left alone, the client holds a form for the
            # rest of the ask's deadline and answers into a broker that went with
            # the engine. This is the same retraction ``session/cancel`` does, and
            # closing without it was the half of the lifecycle it did not cover.
            self._questions.cancel(session.session_id)
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
            if self._questions is not None:
                # Before the turn, so a tool call blocked on a question is
                # released by the same cancel that stops the work around it.
                # Cancelling the ask is also what retracts the request, so the
                # client takes its form back down instead of holding it.
                self._questions.cancel(session.session_id)
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
            # Called for the staging itself: what it returns is this turn's new
            # pairs, and the prompt block is built from the session's whole
            # accumulated list rather than from that slice.
            self._stage_for(session, text)
        except materials.StagingError as exc:
            # Fatal to the turn, not skipped: a deck built from part of its
            # material is wrong in a way nothing downstream can see. Reported as
            # message content because a prompt is never answered with an error.
            outlet.say(session.session_id, redact(f"The material could not be staged. {exc}"))
            return {"stopReason": "end_turn"}
        # No gate on an empty staging. The deck author runs with or without
        # documents -- it gathers and discloses instead of refusing -- and the
        # launcher stopped refusing in 3d50e127. A refusal that survived only on
        # this transport made the same request succeed over one entry point and
        # end_turn over the other.
        text += materials.describe(session.staged, session.materials, session.out)

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

        Returns only the newly staged pairs; ``session.staged`` accumulates. What
        counts as already held is ``materials.unstaged``, which is where the cost
        of skipping the check is written down.
        """
        declared = materials.inputs_from_prompt(text)
        wanted = materials.unique_sources(declared + materials.materials_from_prompt(text))
        staged = materials.stage(session.materials, materials.unstaged(wanted, session.staged), session.taken)
        session.staged.extend(staged)
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
    "sanitise_error_data",
]
