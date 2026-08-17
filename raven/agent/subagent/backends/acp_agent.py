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
- Resuming is the agent's own ``session/load``, not a second command template, so
  whether an agent *can* resume is read from its handshake instead of inferred
  from a config field a human filled in.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.acp.capabilities import CapabilitySnapshot
from raven.agent.acp.pool import get_pool
from raven.agent.acp.protocol import AcpError, AcpRemoteError
from raven.agent.subagent import activity
from raven.agent.subagent.backends.base import bounded_delta
from raven.agent.subagent.backends.observability import (
    external_agent_span,
    record_events,
    record_outcome,
    record_session,
    record_transcript,
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

# A tool call ends whatever message was being written: an agent that says what
# it is about to do, runs a command and then reports back has sent two messages,
# and the protocol marks the boundary only by what came between them. Thoughts
# and usage updates are deliberately not here -- neither ends a message, and
# breaking on one would split a single reply mid-sentence.
_BREAKING_UPDATES = ("tool_call", "tool_call_update")
_MESSAGE_BREAK = "\n\n"


class AcpEmptyTurnError(RuntimeError):
    """The agent finished a turn without producing anything usable.

    Its own name because the cause is almost never visible in the protocol:
    measured on ``hermes acp``, a provider ``HTTP 401`` still returned
    ``stopReason: "end_turn"`` and reported the failure only on stderr. So a turn
    with no content is treated as a failure and the stderr tail carried with it.
    """


def _texts(content: Any) -> list[str]:
    """Every text string in an ACP content value, whatever shape it arrived in.

    Accepts a block, a list of blocks, or a bare string, because the exact
    envelope for each update kind was not measured against a live server and a
    wrong assumption here would silently drop the agent's answer.
    """
    if isinstance(content, str):
        return [content]
    if isinstance(content, dict):
        text = content.get("text")
        return [text] if isinstance(text, str) else []
    if isinstance(content, list):
        return [t for item in content for t in _texts(item)]
    return []


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

    def __init__(self, on_delta: Callable[[str], Awaitable[None]] | None = None) -> None:
        self._on_delta = on_delta
        self._tool_ran = False
        # The run this collector belongs to, captured here rather than read at
        # publish time. `__call__` is awaited from the connection's read loop,
        # a task created when the *connection* was opened, so the ContextVar it
        # carries predates this run -- and a pooled connection shared by two
        # runs would put this one's steps on the other one's live record.
        self._run = activity.current()
        self.answer: list[str] = []
        self.thoughts: list[str] = []
        self.raw: list[dict[str, Any]] = []
        self.kinds: list[str] = []
        self.tool_calls: list[str] = []
        self.usage: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self.answer_at: str | None = None

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat()

    async def __call__(self, method: str, params: dict[str, Any]) -> None:
        self.raw.append({"method": method, "params": params})
        update = params.get("update")
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        kind = kind if isinstance(kind, str) else "unknown"
        self.kinds.append(kind)
        if kind in _ANSWER_UPDATES:
            if not self.answer:
                self.answer_at = self._now()
            texts = _texts(update.get("content"))
            if texts and self._tool_ran and self.answer:
                texts.insert(0, _MESSAGE_BREAK)
            if texts:
                self._tool_ran = False
            self.answer.extend(texts)
            if self._on_delta is not None:
                for text in texts:
                    await self._on_delta(text)
        elif kind in _THOUGHT_UPDATES:
            chunks = _texts(update.get("content"))
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
                title = update.get("title") or update.get("kind") or update.get("toolCallId")
                if isinstance(title, str):
                    self.tool_calls.append(title)
                    self.events.append(
                        {
                            "t": "call",
                            "id": str(update.get("toolCallId") or f"call-{len(self.tool_calls)}"),
                            "name": title,
                            "input": update.get("rawInput") if isinstance(update.get("rawInput"), dict) else None,
                            "at": self._now(),
                        }
                    )
            else:
                status = update.get("status")
                if status in ("completed", "failed"):
                    self.events.append(
                        {
                            "t": "result",
                            "id": str(update.get("toolCallId") or ""),
                            "status": status,
                            "text": "".join(_texts(update.get("content")))[:_RESULT_TEXT_CAP],
                            "at": self._now(),
                        }
                    )
        elif kind == "usage_update":
            self.usage = {k: v for k, v in update.items() if k != "sessionUpdate"}
        # Republished on every update rather than once at the end: the live
        # index is how a panel watches the run, and a transcript that only
        # exists after the answer is not a live view of anything.
        if kind in (*_ANSWER_UPDATES, *_THOUGHT_UPDATES, "tool_call", "tool_call_update"):
            activity.set_transcript(self._run, self.messages(in_flight=True))

    @property
    def text(self) -> str:
        return "".join(self.answer).strip()

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for kind in self.kinds:
            tally[kind] = tally.get(kind, 0) + 1
        return tally

    def messages(self, *, in_flight: bool = False) -> list[dict[str, Any]]:
        """The ordered events as provider-shaped messages.

        One assistant message per tool call, wearing whatever thought preceded
        it, followed by a ``role="tool"`` result matched through the call id --
        the exact shape ``session.resume`` stores, so the client renders a
        delegated run with the renderer it already has. Each message carries the
        wall clock of the event that opened it, which is what lets the renderer
        fold a finished stretch with a real duration.

        The final answer is NOT in the settled shape: the record keeps it in
        ``out.md``, and the reader appends it as the closing message.
        ``in_flight`` appends whatever answer text has streamed in so far
        instead, because a live view has no record to read it from.
        """
        msgs: list[dict[str, Any]] = []
        pending: list[str] = []
        pending_at: str | None = None
        for ev in self.events:
            if ev["t"] == "thought":
                if not pending:
                    pending_at = ev.get("at")
                pending.append(ev["text"])
            elif ev["t"] == "call":
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": ev["id"],
                            "type": "function",
                            "function": {
                                "name": ev["name"],
                                "arguments": json.dumps(ev["input"], ensure_ascii=False) if ev["input"] else "{}",
                            },
                        }
                    ],
                }
                if at := (pending_at or ev.get("at")):
                    entry["timestamp"] = at
                if pending:
                    entry["reasoning_content"] = "".join(pending)
                    pending = []
                    pending_at = None
                msgs.append(entry)
            elif ev["t"] == "result":
                result: dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": ev["id"],
                    "content": ev["text"] if ev["status"] == "completed" else f"[failed] {ev['text']}".strip(),
                }
                if at := ev.get("at"):
                    result["timestamp"] = at
                msgs.append(result)
        if pending:
            trailing: dict[str, Any] = {"role": "assistant", "content": "", "reasoning_content": "".join(pending)}
            if pending_at:
                trailing["timestamp"] = pending_at
            msgs.append(trailing)
        if in_flight and self.text:
            streaming: dict[str, Any] = {"role": "assistant", "content": self.text}
            if self.answer_at:
                streaming["timestamp"] = self.answer_at
            msgs.append(streaming)
        return msgs


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

            session_id, resumed = await self._open_session(client, cwd=cwd, skey=skey, handle=handle, budget=budget)
            record_session(span, session_id=session_id, resumed=resumed)

            # Snapshotted before the prompt so the error below names what this
            # turn ran into rather than what the connection has ever refused.
            refused_before = client.refusal_count
            sink = bounded_delta(on_delta, self.max_output_chars)
            collector = _TurnCollector(sink)
            # Held across the whole turn, not just the send: see
            # `_Connection.session_lock` for why two prompts cannot share one
            # session id.
            async with connection.session_lock(session_id):
                connection.router.attach(session_id, collector)
                try:
                    result = await client.request(
                        "session/prompt",
                        {"sessionId": session_id, "prompt": [{"type": "text", "text": task}]},
                        timeout=self.timeout,
                    )
                finally:
                    connection.router.detach(session_id, collector)

            stop_reason = (result or {}).get("stopReason") if isinstance(result, dict) else None
            text = collector.text
            self._record(span, collector, stop_reason=stop_reason, started=started, session_id=session_id)

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

    def _record(
        self,
        span: Any,
        collector: _TurnCollector,
        *,
        stop_reason: Any,
        started: float,
        session_id: str,
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
        if collector.raw:
            record_transcript(span, {"agent": self.name, "sessionId": session_id, "frames": collector.raw})


__all__ = ["AcpAgentBackend", "AcpEmptyTurnError"]
