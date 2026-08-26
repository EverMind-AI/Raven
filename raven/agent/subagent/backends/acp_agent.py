"""Third-party ACP agent backend: one ``session/prompt`` on a pooled connection.

The counterpart of :mod:`raven.agent.subagent.backends.cli_agent`. Where that one
spawns a process per task and parses whatever the CLI prints, this one keeps a
connection (owned by :mod:`raven.agent.acp.pool`) and delivers the task as a
request on it.

Two consequences worth stating, because they are what the transport buys:

- The agent's intermediate work arrives as ``session/update`` notifications while
  the task runs, instead of being reconstructed from stdout afterwards. It is
  recorded on this backend's own tracing span and in the run's own activity
  record (:mod:`raven.agent.subagent.activity`) rather than returned, so neither
  ``spawn`` nor ``run_subagent_dag`` has to change to carry it.
- Those notifications are the run as a reader wants it, not everything that
  crossed the wire. The whole exchange -- the agent's own requests and what raven
  answered, raven's outbound frames, notifications no session was listening for,
  stderr -- is written by :mod:`raven.agent.acp.journal` for the life of the
  connection, and each call records the byte range it occupied there.
- Resuming is the agent's own ``session/load``, not a second command template, so
  whether an agent *can* resume is read from its handshake instead of inferred
  from a config field a human filled in.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.acp.ask_user import AskUserResponder, clarify_responder
from raven.agent.acp.capabilities import CapabilitySnapshot, steer_offered
from raven.agent.acp.elicitor import Elicitor
from raven.agent.acp.permissions import PERMISSION_METHOD
from raven.agent.acp.pool import get_pool
from raven.agent.acp.protocol import STEER_METHOD, AcpError, AcpRemoteError
from raven.agent.subagent import activity
from raven.agent.subagent.acp_dialects import AcpDialect, ToolCall, content_texts, dialect_for
from raven.agent.subagent.backends import turn_rows
from raven.agent.subagent.backends.base import bounded_delta
from raven.agent.subagent.backends.observability import (
    external_agent_span,
    record_events,
    record_frames,
    record_outcome,
    record_session,
)
from raven.agent.subagent.instances import InstanceRegistry, get_registry

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider

# Text-bearing update kinds, and the ones whose text is the answer rather than
# commentary. Only `usage_update` was observed on a live server; the rest follow
# the ACP contract and are handled defensively -- an unrecognised shape is
# recorded raw and skipped, never guessed at.
_ANSWER_UPDATES = ("agent_message_chunk",)
_THOUGHT_UPDATES = ("agent_thought_chunk",)
# What the person said mid-turn. Only a steer produces one during a live turn:
# every other user message reached the agent as the prompt and is already on
# the caller's side, so this frame is the one record of a steer landing.
_USER_UPDATES = ("user_message_chunk",)

# A tool call ends whatever message was being written: an agent that says what
# it is about to do, runs a command and then reports back has sent two messages,
# and the protocol marks the boundary only by what came between them. Thoughts
# and usage updates are deliberately not here -- neither ends a message, and
# breaking on one would split a single reply mid-sentence.
_BREAKING_UPDATES = ("tool_call", "tool_call_update")
_MESSAGE_BREAK = "\n\n"

# One synthetic id for the turn's plan. The frame carries no call id of its
# own, and a plan frame re-sends the whole list on every change -- five times
# for one plan in codex's capture, the only adapter measured sending one -- so
# a row per frame would be five near-identical rows. The branch this feeds is
# dialect-independent: any adapter's plan frame lands here.
_PLAN_CALL_ID = "acp-plan"


class AcpEmptyTurnError(RuntimeError):
    """The agent finished a turn without producing anything usable.

    Its own name because the cause is almost never visible in the protocol:
    measured on ``hermes acp``, a provider ``HTTP 401`` still returned
    ``stopReason: "end_turn"`` and reported the failure only on stderr. So a turn
    with no content is treated as a failure and the stderr tail carried with it.
    """


_RESULT_TEXT_CAP = 2000


class _TurnCollector:
    """Accumulates one session's notifications while a prompt is in flight.

    Beside the flat tallies, an ordered event list survives the turn: thoughts,
    tool calls and their results, in the order the agent reported them. That is
    the run's own transcript, and :meth:`messages` renders it in the provider
    shape the main transcript is stored in -- so a delegated run can be read
    back with the same renderer as the conversation that spawned it.

    ``on_delta`` forwards the answer chunks as they arrive, for a caller
    rendering the turn live. It is called from the connection's read loop, which
    already treats a raising notification handler as non-fatal (see
    ``AcpClient._dispatch``) -- so a client that went away mid-turn costs this
    turn its live rendering, not the connection or the answer, which keeps
    accumulating here either way.

    One turn can hold several messages, and the chunks carry no boundary of
    their own: an agent that says what it is about to do, runs a command and
    then reports back sends two, and joined chunk-to-chunk they read as one
    paragraph whose halves do not follow from each other. ``_BREAKING_UPDATES``
    is where the boundary is recovered, and the break is forwarded to
    ``on_delta`` as well so a live view and the stored reply agree.
    """

    def __init__(
        self,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        dialect: AcpDialect | None = None,
        prompt: str | None = None,
    ) -> None:
        self._on_delta = on_delta
        # The prompt this turn was opened with. A ``user_message_chunk`` that
        # repeats it is an agent echoing the question back (the spec uses the
        # frame for replays), not a steer, and must not become a user row.
        self._prompt = (prompt or "").strip()
        # The spec by default, so a collector built without one (every test that
        # only cares about answer text) still reads tool calls correctly.
        self._dialect = dialect or AcpDialect()
        self._tool_ran = False
        # The run this collector belongs to, captured here rather than read at
        # publish time. `__call__` is awaited from the connection's read loop,
        # a task created when the *connection* was opened, so the ContextVar it
        # carries predates this run -- and a pooled connection shared by two
        # runs would put this one's steps on the other one's live record.
        self._run = activity.current()
        self.answer: list[str] = []
        self.thoughts: list[str] = []
        self.kinds: list[str] = []
        self.usage: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self.answer_at: str | None = None

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat()

    async def __call__(self, method: str, params: dict[str, Any]) -> None:
        if method == PERMISSION_METHOD:
            # Subject only, never the name: this frame reports `kind: "execute"`
            # even for a call the session update badged `read`, and reading it
            # for a name would erase the distinction codex drew.
            command = self._dialect.permission_command(params)
            tool = params.get("toolCall")
            call_id = str(tool.get("toolCallId") or "") if isinstance(tool, dict) else ""
            if command and call_id:
                self._backfill_permission_command(call_id, command)
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        kind = kind if isinstance(kind, str) else "unknown"
        self.kinds.append(kind)
        if kind in _ANSWER_UPDATES:
            if not self.answer:
                self.answer_at = self._now()
            texts = content_texts(update.get("content"))
            said = "".join(texts)
            if texts and self._tool_ran and self.answer:
                texts.insert(0, _MESSAGE_BREAK)
            if texts:
                self._tool_ran = False
            self.answer.extend(texts)
            if said:
                # Merged with the previous burst only when nothing came between:
                # a `call` event in between is exactly what makes this a new
                # burst, and the boundary is what puts each one on the step it
                # was said before instead of at the end with the rest.
                if self.events and self.events[-1].get("t") == "say":
                    self.events[-1]["text"] += said
                else:
                    self.events.append({"t": "say", "text": said, "at": self._now()})
            if self._on_delta is not None:
                for text in texts:
                    await self._on_delta(text)
        elif kind in _USER_UPDATES:
            said = "".join(content_texts(update.get("content")))
            if said and said.strip() != self._prompt:
                # A steer ends the message being written the way a tool call
                # does: what the agent says next answers the new words.
                self._tool_ran = bool(self.answer)
                self.events.append({"t": "user", "text": said, "at": self._now()})
        elif kind in _THOUGHT_UPDATES:
            chunks = content_texts(update.get("content"))
            self.thoughts.extend(chunks)
            for c in chunks:
                if self.events and self.events[-1].get("t") == "thought":
                    self.events[-1]["text"] += c
                else:
                    self.events.append({"t": "thought", "text": c, "at": self._now()})
        elif kind in _BREAKING_UPDATES:
            # Both tool kinds break the message, and each also contributes its
            # own row to the transcript: the call names what was run, the update
            # carries how it ended. One branch, because the boundary is the same
            # fact for both -- an answer chunk after either starts a new message.
            self._tool_ran = True
            if kind == "tool_call":
                call = self._dialect.call(update)
                self.events.append(
                    {
                        "t": "call",
                        "id": call.id or f"call-{len(self.calls) + 1}",
                        "call": call,
                        "at": self._now(),
                    }
                )
            else:
                self._revise_call(update)
                status = update.get("status")
                if status in ("completed", "failed"):
                    outcome = self._dialect.result(update)
                    self.events.append(
                        {
                            "t": "result",
                            "id": str(update.get("toolCallId") or ""),
                            "ok": outcome.ok,
                            "text": outcome.text[:_RESULT_TEXT_CAP],
                            "at": self._now(),
                        }
                    )
                    self._backfill_subject(str(update.get("toolCallId") or ""), update)
        elif kind == "plan":
            self._plan(update)
        elif kind == "usage_update":
            self.usage = {k: v for k, v in update.items() if k != "sessionUpdate"}
        # Republished on every update rather than once at the end: the live
        # index is how a panel watches the run, and a transcript that only
        # exists after the answer is not a live view of anything.
        if kind in (*_ANSWER_UPDATES, *_THOUGHT_UPDATES, *_USER_UPDATES, "tool_call", "tool_call_update", "plan"):
            activity.set_transcript(self._run, self.messages(in_flight=True))

    def _revise_call(self, update: dict[str, Any]) -> None:
        """Re-read a call's arguments from a later frame.

        The opening ``tool_call`` announces the call before the arguments are
        known: measured on claude-agent-acp and opencode, it carries
        ``rawInput: {}`` and the real input follows on a ``tool_call_update``.
        Until that lands, the only thing a reader has is the title -- which is
        why the fallback to it is resolved at render time (``ToolCall.subject``)
        rather than stored here.

        The newest input wins, because that is what a ``tool_call_update``
        means: the fields it carries replace the call's current ones, so an
        adapter that revises an argument has revised what the tool actually ran
        with. The whole call is re-parsed rather than only its input, since the
        same frame also carries the ``kind`` and ``_meta`` a dialect names the
        tool from -- but only when it does: ``names_call`` is what decides, and
        a frame that answers False leaves the name alone.
        """
        if not self._dialect.revises_call(update):
            return
        call_id = str(update.get("toolCallId") or "")
        for event in reversed(self.events):
            if event.get("t") == "call" and event.get("id") == call_id:
                revised = self._dialect.call(update)
                previous: ToolCall = event["call"]
                event["call"] = ToolCall(
                    id=previous.id,
                    # Guarded like the fields below it: a frame that carries no
                    # kind cannot name the call, and reading one anyway is what
                    # renamed codex's searches to the fallback.
                    name=revised.name if self._dialect.names_call(update) else previous.name,
                    argument=revised.argument or previous.argument,
                    # An update need not repeat the title, and losing it would
                    # leave a no-argument call with nothing to show at all.
                    title=revised.title or previous.title,
                    raw_input=revised.raw_input or previous.raw_input,
                )
                return

    def _backfill_subject(self, call_id: str, update: dict[str, Any]) -> None:
        """Set a call's subject from its own result.

        The frame that opened the call did not have it: codex sends
        ``apply_patch`` as the command and names the files it patched only in
        the output. Never overwrites a subject already known -- a later frame
        revising an argument is ``_revise_call``'s business, not this.

        Passes the call's own name, established when it opened, into
        ``subject_from_result`` as the tool the completed frame belongs to: that
        frame typically carries none of ``tool_name``'s own discriminators, so
        without it a dialect could not tell an ``apply_patch`` completion from
        any other tool's own -- including one whose output merely looks like an
        apply_patch envelope.
        """
        for event in reversed(self.events):
            if event.get("t") == "call" and event.get("id") == call_id:
                previous: ToolCall = event["call"]
                if previous.argument:
                    return
                subject = self._dialect.subject_from_result(update, name=previous.name)
                if not subject:
                    return
                # The new subject must win a `path` collision -- an
                # `imageGeneration` call's own `rawInput` can already carry one
                # -- so it seeds the dict and `previous.raw_input` is copied in
                # behind it, the same order `CodexDialect.call` merges with.
                raw_input = {"path": subject}
                for key, value in previous.raw_input.items():
                    if key == "path":
                        continue
                    # `apply_patch`'s own `command` is the tool's name, not an
                    # argument -- a reader that picks a subject by scanning
                    # known key names rather than by position (as
                    # `ui-tui`'s `callSubject` does) would otherwise take it
                    # over the subject just recovered here. A real recovered
                    # command never equals the call's own name, so this never
                    # drops one.
                    if key == "command" and value == previous.name:
                        continue
                    raw_input[key] = value
                event["call"] = ToolCall(
                    id=previous.id,
                    name=previous.name,
                    argument=subject,
                    title=previous.title,
                    raw_input=raw_input,
                )
                return

    def _backfill_permission_command(self, call_id: str, command: str) -> None:
        """Set a call's subject to the command a permission request recovered.

        Guarded on ``raw_input`` already carrying a ``command`` key rather than
        on ``argument`` being non-empty: a ``commandExecution.read`` opens with
        its ``locations`` path already filling ``argument``, and that
        placeholder is exactly what this must replace. A real
        ``commandExecution`` -- and ``apply_patch``, whose ``rawInput.command``
        is its own name -- already carry their command there, and overwriting
        either would either restate the same value or, for ``apply_patch``,
        block the file-derived subject its own result still owes it.
        """
        for event in reversed(self.events):
            if event.get("t") == "call" and event.get("id") == call_id:
                previous: ToolCall = event["call"]
                if previous.raw_input.get("command"):
                    return
                event["call"] = ToolCall(
                    id=previous.id,
                    name=previous.name,
                    argument=command,
                    title=previous.title,
                    raw_input={"command": command, **previous.raw_input},
                )
                return

    def _plan(self, update: dict[str, Any]) -> None:
        """Open the turn's plan row, or move the one already open.

        Only the opening frame breaks the message. A revision that broke it too
        would split the narration around a plan that changes four times.
        """
        subject, checklist = self._dialect.plan_rows(update)
        if not checklist:
            return
        call = ToolCall(
            id=_PLAN_CALL_ID,
            name=self._dialect.plan_tool_name,
            argument=subject,
            title="",
            raw_input={"argument": subject} if subject else {},
        )
        for event in self.events:
            if event.get("t") == "call" and event.get("id") == _PLAN_CALL_ID:
                event["call"] = call
                break
        else:
            self._tool_ran = True
            self.events.append({"t": "call", "id": _PLAN_CALL_ID, "call": call, "at": self._now()})
        for event in self.events:
            if event.get("t") == "result" and event.get("id") == _PLAN_CALL_ID:
                event["text"] = checklist
                return
        self.events.append({"t": "result", "id": _PLAN_CALL_ID, "ok": True, "text": checklist, "at": self._now()})

    @property
    def calls(self) -> list[ToolCall]:
        return [ev["call"] for ev in self.events if ev["t"] == "call"]

    @property
    def tool_calls(self) -> list[str]:
        """One label per call, read at the end rather than when each was announced.

        The opening ``tool_call`` frame does not yet carry the arguments, so a
        label built there names the call by its title and can never be corrected
        -- which put "read_file read_file src/a.py" on the span, the title
        pasted under a verb derived from it.
        """
        return [call.label for call in self.calls]

    @property
    def text(self) -> str:
        return "".join(self.answer).strip()

    @property
    def closing_text(self) -> str:
        """What the agent said after its last tool call -- the reply proper.

        Measured on codex-acp, a turn narrates as it goes: a plan, then a
        progress note before each of three calls, then the report. ``text``
        joins all of it, which is right for the caller that receives the run's
        answer, and wrong for a transcript -- the three progress notes belong on
        the steps they preceded, and repeating them inside the final message is
        what made a direct chat read as one blob of restated plan.

        Empty when the turn ended on a tool call and said nothing after it, which
        is a real outcome and not the same as "not reported".

        A steer is a boundary too: what was said before the person's words is
        already on its own row above them, and repeating it here printed the
        first half of the reply twice.
        """
        said: list[str] = []
        for ev in reversed(self.events):
            if ev["t"] in ("call", "user"):
                break
            if ev["t"] == "say":
                said.append(ev["text"])
        return "".join(reversed(said)).strip()

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for kind in self.kinds:
            tally[kind] = tally.get(kind, 0) + 1
        return tally

    def messages(self, *, in_flight: bool = False) -> list[dict[str, Any]]:
        """The ordered events as provider-shaped messages.

        The shape itself lives in
        :mod:`raven.agent.subagent.backends.turn_rows`, shared with the OpenAI
        Step Dialect: one implementation is what keeps the two transports'
        conversations readable by one renderer. This maps ACP's events onto that
        vocabulary and nothing else.
        """
        events: list[dict[str, Any]] = []
        for ev in self.events:
            kind = ev["t"]
            if kind == "say":
                events.append(turn_rows.say(ev["text"]))
            elif kind == "thought":
                events.append(turn_rows.thought(ev["text"], at=ev.get("at")))
            elif kind == "user":
                events.append(turn_rows.user(ev["text"], at=ev.get("at")))
            elif kind == "call":
                acp_call: ToolCall = ev["call"]
                events.append(
                    turn_rows.call(
                        id=ev["id"],
                        name=acp_call.name,
                        arguments_json=acp_call.arguments_json(),
                        at=ev.get("at"),
                    )
                )
            elif kind == "result":
                events.append(turn_rows.result(id=ev["id"], text=ev["text"], ok=ev["ok"], at=ev.get("at")))
        return turn_rows.rows(
            events,
            in_flight_answer=self.closing_text if in_flight else None,
            in_flight_answer_at=self.answer_at if in_flight else None,
        )


class AcpAgentBackend:
    """Runs a task as one ``session/prompt`` against a pooled ACP connection."""

    streams = True

    def __init__(
        self,
        *,
        name: str,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        ready_timeout_ms: int = 30000,
        timeout: int | None = None,
        max_output_chars: int = 30000,
        snapshot: CapabilitySnapshot | None = None,
        registry: InstanceRegistry | None = None,
    ) -> None:
        self.name = name
        self.command = command
        self.cwd = cwd
        self.env = env or {}
        self.ready_timeout_ms = ready_timeout_ms
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self._snapshot = snapshot
        self._registry = registry or get_registry()

    @property
    def is_stateful(self) -> bool:
        """Whether reusing a handle continues this agent's session.

        Read from the handshake snapshot, never declared: a cli entry derives this
        from ``resume_command`` because that is the mechanism that would have to
        deliver it, and for acp the equivalent mechanism is the agent's own
        ``sessionCapabilities.resume``.
        """
        return bool(self._snapshot and self._snapshot.can_resume)

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
        provider: LLMProvider | None = None,
        model: str | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        # The parent's provider/model are accepted and ignored, as the cli
        # transport does it: this backend prompts an agent that holds its own
        # credential and picks its own model. Accepted rather than omitted
        # because the manager passes the same keyword set to whichever backend
        # it resolved, and a missing parameter fails the dispatch with a
        # TypeError before the agent is ever contacted.
        skey = session_key or "default"
        handle = instance or task_id
        cwd = self.cwd or str(workspace)
        started = time.monotonic()

        with external_agent_span(agent=self.name, transport="acp", task_id=task_id, instance=handle) as span:
            # The entry's own budget, not the pool's default: `readyTimeoutMs` is
            # documented as how long the `initialize` handshake may take, and
            # after the handshake moved into the connection it was the one thing
            # that stopped honouring it -- so an operator who raised it for a
            # slow adapter had `verify` pass at 150s and every dispatch fail at
            # the module constant.
            budget = max(1.0, self.ready_timeout_ms / 1000)
            connection = await get_pool().acquire(
                name=self.name,
                command=self.command,
                cwd=cwd,
                env=dict(self.env),
                ready_timeout_s=budget,
            )
            client = connection.client
            # Marked after acquire, so a call's range covers its own traffic and
            # not the handshake of a connection it merely inherited. The pool
            # records where that handshake ends, and it is carried alongside so a
            # reader of one call can still find it.
            journal = client.journal
            frames_start = journal.offset if journal is not None else None

            session_id, resumed = await self._open_session(client, cwd=cwd, skey=skey, handle=handle, budget=budget)
            if journal is not None:
                # Inside the marked range on purpose, so the per-call copy of the
                # frames carries the line that says whose call they are.
                journal.bind(
                    {
                        "session": session_id,
                        "agent": self.name,
                        "instance": handle,
                        "task_id": task_id,
                        "session_key": skey,
                        "resumed": resumed,
                    }
                )
            record_session(span, session_id=session_id, resumed=resumed)

            # Snapshotted before the prompt so the error below names what this
            # turn ran into rather than what the connection has ever refused.
            refused_before = client.refusal_count
            sink = bounded_delta(on_delta, self.max_output_chars)
            # From the live handshake, not the stored snapshot: this is the
            # process actually answering, and a snapshot can be stale.
            collector = _TurnCollector(sink, dialect_for(connection.initialize), prompt=task)
            # Built here, in the turn's context, for the reason `_TurnCollector`
            # documents: the read loop's ContextVars predate this run, and the
            # asker is bound per turn.
            elicitor = Elicitor(self.name, handle, dialect_for(connection.initialize))
            # Built here for the same reason and bound to this connection's
            # client, because the answer goes back as a request rather than as
            # this frame's return value.
            responder = AskUserResponder(self.name, handle, clarify_responder(client))
            # Held across the whole turn, not just the send: see
            # `_Connection.session_lock` for why two prompts cannot share one
            # session id.
            async with connection.session_lock(session_id):
                connection.router.attach(session_id, collector)
                connection.elicitors.attach(session_id, elicitor)
                connection.responders.attach(session_id, responder)
                if self.can_steer or steer_offered(connection.initialize):
                    activity.offer_steer(collector._run, self._steerer(client, session_id))
                try:
                    result = await client.request(
                        "session/prompt",
                        {"sessionId": session_id, "prompt": [{"type": "text", "text": task}]},
                        timeout=self.timeout,
                        cancel_session=session_id,
                    )
                except asyncio.CancelledError:
                    # The turn outlived its cancel budget, so it is still running
                    # on the agent while this lock is about to be released --
                    # prompting the same session again would collide with it.
                    # Dropping the binding is the same recovery `_open_session`
                    # makes when a resume fails: the instance keeps its handle
                    # and the next dispatch opens a fresh session under it.
                    # Evaluated in this order so take_unsettled_cancel -- a
                    # consuming read -- always clears the flag; skipping it for a
                    # stateless agent would leak it.
                    if client.take_unsettled_cancel(session_id) and self.is_stateful:
                        await self._registry.unbind(skey, self.name, handle)
                    # The frames of a turn that was cut short are the ones worth
                    # having, and this path returns no result to hang them off:
                    # published here or the record of a timed-out call points at
                    # nothing, while the journal holds the whole exchange.
                    cancelled = self._frames(journal, connection, session_id, frames_start)
                    activity.note_frames(cancelled)
                    record_frames(span, cancelled)
                    raise
                finally:
                    activity.offer_steer(collector._run, None)
                    connection.router.detach(session_id, collector)
                    connection.elicitors.detach(session_id, elicitor)
                    connection.responders.detach(session_id, responder)
                    # Detaching stops only the *next* request from routing here.
                    # One already put to the user waits on a task of the
                    # connection's, which no part of this teardown reaches --
                    # the pooled connection stays open, so `AcpClient.close`
                    # never cancels it either. Left standing it holds this
                    # conversation's form lock and keeps a sheet up that answers
                    # into a run that is gone.
                    elicitor.cancel()
                    # Same reason, and the same window: a question already put to
                    # the user answers into a run that is gone, and holds this
                    # conversation's lock against the next one while it waits.
                    responder.cancel()

            stop_reason = (result or {}).get("stopReason") if isinstance(result, dict) else None
            text = collector.text
            frames = self._frames(journal, connection, session_id, frames_start)
            self._record(span, collector, stop_reason=stop_reason, started=started, frames=frames)

            if not text:
                tail = client.stderr_tail(1500)
                span.error(f"empty turn (stopReason={stop_reason})")
                # An agent that asked raven for something it does not serve --
                # `fs/read_text_file` and its siblings, permission requests
                # being answered now -- was told the method does not exist, and
                # adapters answer that by ending the turn with nothing. The
                # stderr tail says nothing about it, so name it here or it is
                # unknowable.
                refused = client.refusals_since(refused_before, session_id=session_id)
                # Collapsed: one turn with three tool calls refuses the same
                # method three times, and naming it three times says no more.
                asked = f"; refused agent requests: {', '.join(sorted(set(refused)))}" if refused else ""
                raise AcpEmptyTurnError(
                    f"acp agent {self.name!r} ended its turn with no content "
                    f"(stopReason={stop_reason!r}){asked}; stderr tail: {tail or '<empty>'}"
                )

            if not resumed and self.is_stateful:
                # Deferred commit, as the cli transport does it: binding a handle
                # before the first turn succeeded would resume a session that
                # never produced anything.
                await self._registry.commit(skey, self.name, handle, session_id, kind="acp")

            return await self._finished(text, stop_reason=stop_reason, span=span, sink=on_delta)

    @property
    def can_steer(self) -> bool:
        """Whether this agent takes text mid-turn (``_raven/session/steer``).

        Read from the handshake snapshot like ``is_stateful``: the agent declares
        the extension in ``agentCapabilities._meta``, and calling an undeclared
        extension gets a method-not-found in the middle of somebody's turn. A
        snapshot measured before the agent learned the extension says no; the
        turn itself also asks the live handshake (``steer_offered``), so a stale
        snapshot delays nothing.
        """
        return bool(self._snapshot and self._snapshot.can_steer)

    def _steerer(self, client: Any, session_id: str) -> Callable[[str], Awaitable[str]]:
        """The steer hook for one prompt: merge text into that session's turn.

        Answers the agent's own status -- ``injected`` or ``no_turn`` -- and
        ``unsupported`` if the agent refuses the method after all. Never raises
        for a refusal: the caller is a person typing, and the honest answer is
        a status they can act on.
        """

        async def steer(text: str) -> str:
            try:
                answer = await client.request(STEER_METHOD, {"sessionId": session_id, "text": text}, timeout=30)
            except AcpRemoteError as exc:
                logger.warning("acp agent {!r} refused a steer: {}", self.name, exc)
                return "unsupported"
            status = answer.get("status") if isinstance(answer, dict) else None
            return status if status in ("injected", "no_turn") else "no_turn"

        return steer

    async def _finished(
        self,
        text: str,
        *,
        stop_reason: Any,
        span: Any,
        sink: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """The reply, marked when the agent stopped before it had said everything.

        Only ``end_turn`` means the agent finished. Every other stop reason
        leaves a reply that reads complete and is not, and the reader -- the main
        agent as much as a person in a direct chat -- has no other way to tell:
        the text simply ends, mid-thought, and the run is reported as a success.
        Measured, and the reason this exists: a cancelled codex turn returned its
        opening sentence and nothing else, with the span still green.

        Appended rather than raised. A partial answer is worth having, and the
        turn's record already holds it -- discarding it to signal the truncation
        would trade one kind of silence for another.

        The notice is budgeted *before* the reply is clamped, so the one line
        saying the answer is incomplete cannot be the part that gets cut.

        Pushed through ``sink`` as well as returned, because for a caller that
        streamed there is no other way for it to arrive: the return value is
        deliberately not delivered a second time (``AgentLoop.run_turn``), so a
        notice that only rode on it reached the record and never the screen --
        which is the one reader it exists for.

        The *unbounded* callback, not the reply's ``bounded_delta``: that budget
        exists to cap what the agent says, and a reply which saturates it would
        otherwise swallow the one line explaining that it was cut off -- exactly
        the case the notice is for. This line is raven's own and fixed-length.
        """
        if stop_reason in ("end_turn", None):
            return text[: self.max_output_chars]
        span.error(f"turn ended early (stopReason={stop_reason})")
        logger.warning("acp agent {!r}: turn ended with stopReason={!r}; reply is partial", self.name, stop_reason)
        notice = f"\n\n[raven] {self.name} stopped before finishing (stopReason={stop_reason}); reply is partial."
        activity.append_closing(notice)
        if sink is not None:
            await sink(notice)
        return text[: max(0, self.max_output_chars - len(notice))] + notice

    async def _open_session(self, client: Any, *, cwd: str, skey: str, handle: str, budget: float) -> tuple[str, bool]:
        """The session to prompt, and whether it continues an earlier one."""
        if self.is_stateful:
            known = await self._registry.lookup(skey, self.name, handle, kind="acp")
            if known is not None and (self._snapshot and self._snapshot.can_load):
                try:
                    await client.request(
                        "session/load", {"sessionId": known, "cwd": cwd, "mcpServers": []}, timeout=budget
                    )
                    return known, True
                except AcpRemoteError as exc:
                    # The agent's own store may have pruned this id, and the
                    # `session/load` param shape is not measured against a live
                    # server. Either way a fresh session is a working outcome, so
                    # drop the stale binding rather than fail the task -- the
                    # instance itself stays on the strip, see `unbind`.
                    logger.warning(
                        "acp agent {!r}: resuming {}/{!r} failed ({}); starting a fresh session",
                        self.name,
                        handle,
                        known,
                        exc.message,
                    )
                    await self._registry.unbind(skey, self.name, handle)
                except AcpError:
                    # A transport-level failure is not evidence the session was
                    # pruned, so the binding is kept and the caller is told.
                    raise

        result = await client.request("session/new", {"cwd": cwd, "mcpServers": []}, timeout=budget)
        session_id = (result or {}).get("sessionId") if isinstance(result, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise AcpEmptyTurnError(f"acp agent {self.name!r}: session/new returned no sessionId")
        return session_id, False

    @staticmethod
    def _frames(journal: Any, connection: Any, session_id: str, start: int | None) -> dict[str, Any]:
        """Where this call's frames sit in the connection's journal.

        Empty when no journal is being kept, so a disabled journal leaves the
        record saying nothing about frames rather than pointing at a file that
        does not exist.
        """
        if journal is None or start is None:
            return {}
        return {
            "path": str(journal.path),
            "session_id": session_id,
            "start": start,
            "end": journal.offset,
            "handshake_end": getattr(connection, "handshake_bytes", 0),
        }

    def _record(
        self,
        span: Any,
        collector: _TurnCollector,
        *,
        stop_reason: Any,
        started: float,
        frames: dict[str, Any],
    ) -> None:
        counts = collector.counts()
        record_events(span, transport="acp", kinds=counts)
        thought_chars = sum(len(t) for t in collector.thoughts)
        record_outcome(
            span,
            answer_chars=len(collector.text),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            stop_reason=stop_reason,
            update_counts=counts,
            tool_calls=collector.tool_calls,
            thought_chars=thought_chars,
            usage=collector.usage or None,
        )
        # The same facts to the run's own record, so a reader sees what the agent
        # did without opening a trace viewer. Once, not per notification: an ACP
        # agent's `usage_update` is cumulative for the turn.
        for title in collector.tool_calls:
            activity.note_tool_call(title)
        activity.note_usage(collector.usage)
        activity.note_steps(counts)
        activity.note_thoughts(thought_chars)
        activity.note_transcript(collector.messages())
        # After the transcript, and only here: the live republish inside the
        # collector carries the streaming tail as a message, and this is the
        # settled split -- narration on the steps, the reply on its own.
        activity.note_closing(collector.closing_text)
        activity.note_frames(frames)
        record_frames(span, frames)


__all__ = ["AcpAgentBackend", "AcpEmptyTurnError"]
