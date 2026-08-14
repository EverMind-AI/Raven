"""Third-party ACP agent backend: one ``session/prompt`` on a pooled connection.

The counterpart of :mod:`raven.agent.subagent.backends.cli_agent`. Where that one
spawns a process per task and parses whatever the CLI prints, this one keeps a
connection (owned by :mod:`raven.agent.acp.pool`) and delivers the task as a
request on it.

Two consequences worth stating, because they are what the transport buys:

- The agent's intermediate work arrives as ``session/update`` notifications while
  the task runs, instead of being reconstructed from stdout afterwards. It is
  recorded on this backend's own tracing span rather than returned, so neither
  ``spawn`` nor ``run_subagent_dag`` has to change to carry it.
- Resuming is the agent's own ``session/load``, not a second command template, so
  whether an agent *can* resume is read from its handshake instead of inferred
  from a config field a human filled in.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.acp.capabilities import CapabilitySnapshot
from raven.agent.acp.pool import get_pool
from raven.agent.acp.protocol import AcpError, AcpRemoteError
from raven.agent.subagent.backends.observability import (
    external_agent_span,
    record_events,
    record_outcome,
    record_session,
    record_transcript,
)
from raven.agent.subagent.instances import InstanceRegistry, get_registry

# Text-bearing update kinds, and the ones whose text is the answer rather than
# commentary. Only `usage_update` was observed on a live server; the rest follow
# the ACP contract and are handled defensively -- an unrecognised shape is
# recorded raw and skipped, never guessed at.
_ANSWER_UPDATES = ("agent_message_chunk",)
_THOUGHT_UPDATES = ("agent_thought_chunk",)


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


class _TurnCollector:
    """Accumulates one session's notifications while a prompt is in flight."""

    def __init__(self) -> None:
        self.answer: list[str] = []
        self.thoughts: list[str] = []
        self.raw: list[dict[str, Any]] = []
        self.kinds: list[str] = []
        self.tool_calls: list[str] = []
        self.usage: dict[str, Any] = {}

    async def __call__(self, method: str, params: dict[str, Any]) -> None:
        self.raw.append({"method": method, "params": params})
        update = params.get("update")
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        kind = kind if isinstance(kind, str) else "unknown"
        self.kinds.append(kind)
        if kind in _ANSWER_UPDATES:
            self.answer.extend(_texts(update.get("content")))
        elif kind in _THOUGHT_UPDATES:
            self.thoughts.extend(_texts(update.get("content")))
        elif kind == "tool_call":
            title = update.get("title") or update.get("kind") or update.get("toolCallId")
            if isinstance(title, str):
                self.tool_calls.append(title)
        elif kind == "usage_update":
            self.usage = {k: v for k, v in update.items() if k != "sessionUpdate"}

    @property
    def text(self) -> str:
        return "".join(self.answer).strip()

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for kind in self.kinds:
            tally[kind] = tally.get(kind, 0) + 1
        return tally


class AcpAgentBackend:
    """Runs a task as one ``session/prompt`` against a pooled ACP connection."""

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
    ) -> str:
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
            collector = _TurnCollector()
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
                # An agent that asked raven for something -- approval, most
                # likely -- was told the method does not exist, and adapters
                # answer that by ending the turn with nothing. The stderr tail
                # says nothing about it, so name it here or it is unknowable.
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

            return text[: self.max_output_chars]

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
                    # forget the handle rather than fail the task.
                    logger.warning(
                        "acp agent {!r}: resuming {}/{!r} failed ({}); starting a fresh session",
                        self.name,
                        handle,
                        known,
                        exc.message,
                    )
                    await self._registry.forget(skey, self.name, handle)
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
        record_outcome(
            span,
            answer_chars=len(collector.text),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            stop_reason=stop_reason,
            update_counts=counts,
            tool_calls=collector.tool_calls,
            thought_chars=sum(len(t) for t in collector.thoughts),
            usage=collector.usage or None,
        )
        if collector.raw:
            record_transcript(span, {"agent": self.name, "sessionId": session_id, "frames": collector.raw})


__all__ = ["AcpAgentBackend", "AcpEmptyTurnError"]
