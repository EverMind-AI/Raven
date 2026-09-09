"""One ACP connection: a child process, a read loop, and request correlation.

A connection is not a task. The agent's server is launched once and many tasks
run as ``session/prompt`` requests on it, which is the whole difference from the
cli transport (one process per task). Everything about keeping that process alive
lives here; deciding when to have one lives in the pool.

Process discipline is copied deliberately from
:class:`raven.agent.subagent.backends.cli_agent.CliAgentBackend`: own session, pgid
captured at spawn, ``killpg`` on teardown. An ACP server that reparents a worker
would otherwise survive ``proc.kill()`` exactly as a ``codex``-style CLI does.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import time
from collections import deque
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from loguru import logger

from raven.acp_client import protocol
from raven.acp_client.journal import FrameJournal, redact_acp_frame
from raven.acp_client.protocol import (
    CANCEL_REQUEST_METHOD,
    AcpConnectionError,
    AcpError,
    AcpProtocolError,
    AcpRemoteError,
    AcpTimeoutError,
)

_STDERR_LINES = 200

# How many refused requests a connection remembers. Only the ones from the
# caller's own turn are ever read, so this is a bound rather than a budget.
_REFUSAL_MEMORY = 64
_STDERR_LINE_CAP = 500

# Line budget for both child pipes. asyncio's default is 64 KiB, which is 128x
# smaller than the 8 MiB frame the ACP stdio protocol lets an agent
# send: a single frame past 64 KiB made `readline` raise and took the read loop
# down with it, and on stderr the same raise stopped the drain and deadlocked the
# child. Matched to that cap so the transport can carry what the protocol allows.
_READER_LIMIT = 8 * 1024 * 1024

_TEARDOWN_DRAIN_S = 1.0
"""How long ``close`` waits out the tasks it just cancelled.

The SIGKILL has already gone out by the time this budget starts, so what is
being waited on is only the unwind: a handler that ignores its cancellation
(one parked on the elicitation broker) would otherwise hold the close, and the
process exit behind it, for as long as its own request runs.
"""

_TEARDOWN_REAP_S = 1.0
"""How long ``close`` waits to reap a child it has already SIGKILLed.

Short because the reap is the one part of the teardown with nothing riding on
it here: the signal is delivered synchronously above, and a caller that is
exiting hands any child it did not collect to init regardless. Measured: an
``openclaw acp`` bridge takes longer than this to be collected and is gone
moments later anyway.
"""

_CANCEL_SETTLE_S = 5.0
"""How long a cancelled turn is given to settle with ``stopReason: cancelled``.

A ceiling, not an expectation. The agent's obligation on ``session/cancel`` is
to stop model requests and abort tool calls "as soon as possible" -- the same
class of work raven's own process-group kill finishes in milliseconds.
Exceeding it is a handled state (the caller unbinds the session instead of
prompting it again), which is what lets the bound stay short.

Measured mid-tool-call, three runs each, all settling with ``cancelled``:
claude-agent-acp@0.66.0 at 0.007s / 0.017s / 0.018s, codex-acp@1.1.14 at
0.089s / 0.144s / 17.060s. The budget deliberately does not stretch to that
last one: it is a lone spike beside two sub-200ms runs on the same adapter,
and widening the bound to cover it would put every interactive stop behind a
half-minute wait to spare one session a quarantine that costs it only a fresh
session id.
"""

_DRAINING = False


def begin_drain() -> None:
    """Stop waiting for cancelled turns to settle: the process is going away.

    The pool teardown that follows kills every server, so the wait buys nothing
    there. The notification is still sent, so an adapter that persists session
    state can record the turn as cancelled rather than have it truncated.

    Process-global because asyncio delivers cancellation as a bare
    ``CancelledError`` into the target task: the canceller cannot hand an
    argument or a contextvar to the code that handles it.
    """
    global _DRAINING
    _DRAINING = True


def end_drain() -> None:
    """Leave drain mode. Called by ``close_pool``, which ends the teardown."""
    global _DRAINING
    _DRAINING = False


def is_draining() -> bool:
    """Whether cancelled turns are currently abandoned rather than awaited."""
    return _DRAINING


UNHANDLED: Any = object()
"""A request handler's way of saying "not mine", answered as ``method not found``.

A sentinel rather than a raise, so a handler that covers one method does not have
to reach the blanket ``except`` below -- which logs a traceback and would report
every unimplemented method as a handler crash."""

RequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]
"""Answers an agent-initiated request. Returns the JSON-RPC result, ``UNHANDLED``,
or raises to turn into an error response."""

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


def _indexable(request_id: Any) -> bool:
    """Whether a peer's request id can be used as a dict key.

    The schema's `RequestId` is null, an integer or a string, all of which a dict
    can hold, so no conforming peer fails this. It is checked because the peer is
    a third-party binary and the rest of this loop is deliberately built to
    survive whatever one emits: an unhashable id raises `TypeError` from the
    index, `_read_stdout` answers that by leaving its loop, and its `finally`
    then fails every request in flight -- ending the connection mid-turn over a
    notification that should have cost nothing. `_resolve` guards its own id for
    the same reason.
    """
    return request_id is None or isinstance(request_id, (str, int))


class AcpClient:
    """A live JSON-RPC connection to one ACP agent process."""

    def __init__(
        self,
        *,
        name: str,
        proc: asyncio.subprocess.Process,
        pgid: int,
        on_request: RequestHandler | None = None,
        on_notification: NotificationHandler | None = None,
        journal: FrameJournal | None = None,
    ) -> None:
        self.name = name
        self._proc = proc
        # Captured at spawn by the caller, not re-derived here: with
        # start_new_session the launcher's pid doubles as the group's pgid, and
        # once the launcher exits its pid can be recycled onto an unrelated
        # process.
        self._pgid = pgid
        self._on_request = on_request
        self._on_notification = on_notification
        # Every frame both ways, on disk, for the life of this connection. Held
        # here rather than in the pool because the frames are only visible from
        # inside the send path and the read loop.
        self._journal = journal
        # Which session each outbound request belongs to, so the response -- which
        # carries an id and nothing else -- can be attributed in the journal.
        # Read by the read loop before the awaiting task clears the entry.
        self._request_sessions: dict[int, str | None] = {}
        # Which method each outbound request carries, so "is a turn in flight"
        # is answerable while one is: a pending session/prompt IS the running
        # turn, and a session open that times out behind it needs to say
        # "busy", not "broken" (see AcpBusyError).
        self._request_methods: dict[int, str] = {}
        # Agent-initiated requests raven had no answer for. Kept because the
        # refusal is invisible from the caller's side: the agent asks, gets
        # "method not found", and whatever it does next usually arrives as a
        # turn with no content and nothing on stderr saying why.
        self._refused: deque[tuple[str | None, str]] = deque(maxlen=_REFUSAL_MEMORY)
        self._refused_seen = 0
        self._refused_logged: set[str] = set()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        # Launch counts as activity: a fresh connection has said nothing yet
        # and must not read as stale before its first frame.
        self._last_frame_at = time.monotonic()
        self._unsettled_cancels: set[str] = set()
        self._stderr: deque[str] = deque(maxlen=_STDERR_LINES)
        self._closed = False
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        # Answering runs off the read loop: a handler may block on a human
        # (elicitation), and awaiting it here stops every other session on this
        # connection. Requests carry their own id and are independent, so
        # answering out of arrival order is sound.
        self._answer_tasks: set[asyncio.Task] = set()
        # The same tasks, by the agent's request id, so a ``$/cancel_request``
        # can find the one answer it retracts. A set cannot answer that.
        self._answering: dict[Any, asyncio.Task] = {}

    # ---- lifecycle -------------------------------------------------------

    @classmethod
    async def launch(
        cls,
        *,
        name: str,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        on_request: RequestHandler | None = None,
        on_notification: NotificationHandler | None = None,
        journal: FrameJournal | None = None,
    ) -> "AcpClient":
        """Start the agent's ACP server and begin reading it.

        Raises :class:`AcpConnectionError` if the process cannot be started; a
        server that starts but never speaks is a per-request timeout, not a
        launch failure, because the two need different operator action.
        """
        # Imported here rather than at module level: backends/__init__ pulls in
        # the acp backend, which pulls in this module, so a module-level import
        # back into that package would close the cycle at init time.
        from raven.agent.subagent.backends.env import host_identity_env, login_shell_env
        from raven.agent.subagent.role import subagent_role_env

        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise AcpConnectionError(f"acp agent {name!r}: command cannot be parsed: {exc}") from exc
        if not argv:
            raise AcpConnectionError(f"acp agent {name!r}: command is empty")

        # The capture shells out and can block for real seconds on a slow
        # profile (nvm/conda init); to_thread keeps that off the event loop.
        # These agents need the login shell's PATH, not raven's -- the same
        # reason the cli transport does it.
        base_env = await asyncio.to_thread(login_shell_env)
        # The role goes on before the caller's own map, so a config ``env`` entry
        # is the way to hand one agent back its full registry.
        child_env = {**base_env, **host_identity_env(), **subagent_role_env(), **(env or {})}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=child_env,
                start_new_session=True,
                limit=_READER_LIMIT,
            )
        except (OSError, ValueError) as exc:
            raise AcpConnectionError(f"acp agent {name!r}: cannot start {argv[0]!r}: {exc}") from exc

        client = cls(
            name=name,
            proc=proc,
            pgid=proc.pid,
            on_request=on_request,
            on_notification=on_notification,
            journal=journal,
        )
        client._start_loops()
        logger.info("acp agent {!r}: started {} (pid {})", name, argv[:1], proc.pid)
        return client

    def _start_loops(self) -> None:
        self._reader_task = asyncio.create_task(self._read_stdout(), name=f"acp-read-{self.name}")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name=f"acp-err-{self.name}")

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def alive(self) -> bool:
        """Whether this connection can still answer a request.

        The read loop is part of the check, not just the process: an agent whose
        stdout has closed can linger as a live pid (an ``npx`` wrapper waiting on
        a dead child does), and a connection nobody is reading answers nothing.
        Without this the pool would hand that connection out forever, failing
        every task in milliseconds while the process table says all is well.
        """
        if self._closed or self._proc.returncode is not None:
            return False
        return self._reader_task is None or not self._reader_task.done()

    async def close(self) -> None:
        """Kill the whole process group and stop reading. Idempotent.

        Bounded from the inside, not by its caller: a ``close`` cancelled from
        outside stops before releasing the subprocess transport, whose
        ``__del__`` then runs after the loop is closed and prints
        ``RuntimeError: Event loop is closed`` over whatever the user is looking
        at. So both waits below give up on their own and the teardown always
        runs to its end.
        """
        if self._closed:
            return
        self._closed = True
        try:
            os.killpg(self._pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        answer_tasks = list(self._answer_tasks)
        for task in answer_tasks:
            task.cancel()
        # Awaited, not just cancelled: an un-awaited cancelled task logs a
        # "Task exception was never retrieved" warning on some paths, and the
        # child must be reaped here rather than left as a zombie.
        pending_tasks = [t for t in (self._reader_task, self._stderr_task) if t is not None]
        pending_tasks.extend(answer_tasks)
        if pending_tasks:
            done, _ = await asyncio.wait(pending_tasks, timeout=_TEARDOWN_DRAIN_S)
            for task in done:
                if not task.cancelled():
                    task.exception()
        if self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=_TEARDOWN_REAP_S)
            except (asyncio.TimeoutError, ProcessLookupError):
                logger.debug(
                    "acp agent {!r}: not reaped within {}s of SIGKILL; abandoning the wait",
                    self.name,
                    _TEARDOWN_REAP_S,
                )
        self._release_transport()
        self._fail_pending(AcpConnectionError(f"acp agent {self.name!r}: connection closed"))
        # Last, so the frames the teardown itself produced are in the file.
        if self._journal is not None:
            self._journal.close()

    def _release_transport(self) -> None:
        """Close the subprocess transport while there is still a loop to close it on.

        ``asyncio.subprocess.Process`` holds its transport until the child is
        reaped, and an unreaped one is left to ``__del__`` -- which runs at
        interpreter shutdown, calls ``call_soon`` on a closed loop, and prints a
        ``RuntimeError`` traceback the user can do nothing about. Closing it
        here is the only point at which that is still a no-op.
        """
        transport = getattr(self._proc, "_transport", None)
        if transport is None:
            return
        try:
            transport.close()
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            logger.debug("acp agent {!r}: releasing the transport failed: {}", self.name, exc)

    async def __aenter__(self) -> "AcpClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    # ---- diagnostics -----------------------------------------------------

    @property
    def refusal_count(self) -> int:
        """How many agent-initiated requests this connection has refused, ever.

        A count rather than a set, because the caller wants the ones that
        happened during *its* turn: a connection is process-wide and an adapter
        that asks for approval asks on every tool-using turn, so a set-difference
        would name the cause once and never again.
        """
        return self._refused_seen

    def refusals_since(self, count: int, *, session_id: str | None = None) -> list[str]:
        """The methods refused after the caller's ``refusal_count`` reading.

        Filtered to one session when asked, because a connection is shared: two
        turns run concurrently on different session ids, and a refusal in one
        would otherwise be reported to both -- pointing the operator at
        approvals for a turn that never asked for anything. A refusal that
        carries no session is connection-level and reaches every turn.
        """
        fresh = max(0, self._refused_seen - max(0, count))
        recent = list(self._refused)[-fresh:] if fresh else []
        return [m for sid, m in recent if session_id is None or sid in (None, session_id)]

    @property
    def journal(self) -> FrameJournal | None:
        """This connection's wire log, if one is being kept."""
        return self._journal

    def _frame_session(self, frame: dict[str, Any]) -> str | None:
        """Which session a frame belongs to, where the wire says.

        A request or notification names it in ``params``; a response carries an
        id and nothing else, so it is attributed through the request it answers.
        Connection-level frames -- ``initialize`` and its answer -- belong to no
        session and are recorded without one.
        """
        params = frame.get("params")
        if isinstance(params, dict) and isinstance(params.get("sessionId"), str):
            return params["sessionId"]
        if "method" not in frame and isinstance(frame.get("id"), int):
            return self._request_sessions.get(frame["id"])
        return None

    def stderr_tail(self, max_chars: int = 2000) -> str:
        """The most recent stderr, newest last, clamped.

        Kept because an ACP agent can report a fatal condition *only* here while
        the protocol still reports success: measured on ``hermes acp``, a
        provider ``HTTP 401`` produced ``stopReason: "end_turn"`` with the 401
        visible nowhere else. Bounded because a failing run can be verbose --
        one ``openclaw`` failure wrote 35 KB.
        """
        text = "\n".join(self._stderr)
        return text[-max_chars:] if len(text) > max_chars else text

    def take_unsettled_cancel(self, session_id: str) -> bool:
        """Whether this session's cancel went unanswered. Consumes the flag.

        Consumed rather than sticky: the caller acts on it by dropping the
        session binding, and a second reader acting on the same fact would
        unbind a session that has already been replaced.
        """
        if session_id in self._unsettled_cancels:
            self._unsettled_cancels.remove(session_id)
            return True
        return False

    # ---- messaging -------------------------------------------------------

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        cancel_session: str | None = None,
    ) -> Any:
        """Send a request and await its result.

        Raises :class:`AcpRemoteError` if the agent answers with an error,
        :class:`AcpTimeoutError` on budget expiry, :class:`AcpConnectionError` if
        the connection is gone.

        ``cancel_session`` names the session to stop if *this* request is
        cancelled. Without it a cancelled prompt is only abandoned locally and
        the agent runs the turn to completion, answering an id nobody is
        waiting on.

        Awaited through :func:`asyncio.shield`: cancelling this call cancels
        only the wait, not ``future`` itself. Without the shield, cancelling
        the caller's task cancels whatever bare future it is suspended on as
        the very mechanism that delivers the ``CancelledError`` -- so by the
        time the handler below ran, ``future`` would already read as done, and
        ``_cancel_turn`` could never tell a real settlement from that.
        """
        if not self.alive:
            raise AcpConnectionError(f"acp agent {self.name!r}: connection is not open")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._request_sessions[request_id] = (params or {}).get("sessionId") if isinstance(params, dict) else None
        self._request_methods[request_id] = method
        try:
            await self._send(protocol.request(request_id, method, params))
            if timeout is None:
                return await asyncio.shield(future)
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError:
            raise AcpTimeoutError(f"acp agent {self.name!r}: {method} timed out after {timeout}s") from None
        except asyncio.CancelledError:
            if cancel_session is not None:
                await self._cancel_turn(cancel_session, future)
            raise
        finally:
            self._pending.pop(request_id, None)
            self._request_sessions.pop(request_id, None)
            self._request_methods.pop(request_id, None)

    @property
    def prompting(self) -> bool:
        """Whether a turn is in flight on this connection right now.

        True while any ``session/prompt`` awaits its answer. One connection
        carries every session of this agent, so a pending prompt is exactly
        the condition under which another session's open can only queue.
        """
        return any(method == "session/prompt" for method in self._request_methods.values())

    @property
    def idle_seconds(self) -> float:
        """Seconds since the agent last put any frame on the wire.

        ``alive`` cannot see the failure this measures: a process can hold its
        pipes open with its frame loop wedged, so the pid is live, the reader
        task is parked on ``readline``, and nothing ever answers. Measured
        2026-09-02 on the fork: an agent answered at 18:39 and then logged
        nothing all night, while three session opens each waited out their full
        budget against it. Only silence has a duration; this is it.
        """
        return time.monotonic() - self._last_frame_at

    async def probe(self, timeout: float = 5.0) -> bool:
        """Whether the agent still answers at all, proven by one round trip.

        A repeat ``initialize`` handshake: the agent's dispatcher answers it
        before any business logic and re-initialising is explicitly allowed
        (it only refreshes the capability record), so a healthy agent answers
        in milliseconds and a short timeout convicts nothing that works. Any
        reply counts -- an error frame proves the loop reads and answers just
        as well as a result does.
        """
        if not self.alive:
            return False
        try:
            await self.request("initialize", protocol.initialize_params(), timeout=timeout)
        except AcpRemoteError:
            return True
        except AcpError:
            return False
        return True

    async def _cancel_turn(self, session_id: str, future: asyncio.Future) -> None:
        """Tell the agent to stop this turn, and give it a bounded chance to.

        Killing the process is not available here the way it is for the cli
        transport: one connection carries every session of this agent, so a kill
        would abort unrelated in-flight work.

        Awaiting inside a cancel handler is sound because every canceller in
        this codebase cancels once and then gathers -- the same property
        ``CliAgentBackend._kill_process_group`` relies on to await the child.
        """
        try:
            await self.notify("session/cancel", {"sessionId": session_id})
        except Exception:  # noqa: BLE001 - a connection already gone has nothing to settle
            return
        if is_draining():
            return
        done, _ = await asyncio.wait({future}, timeout=_CANCEL_SETTLE_S)
        if done:
            # Retrieve the exception so a connection that died inside the settle window does
            # not log "Future exception was never retrieved" at GC time.
            if not future.cancelled():
                future.exception()
            return
        self._unsettled_cancels.add(session_id)
        logger.warning(
            "acp agent {!r}: session {!r} did not settle within {}s of session/cancel",
            self.name,
            session_id,
            _CANCEL_SETTLE_S,
        )

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send(protocol.notification(method, params))

    async def _send(self, frame: dict[str, Any], *, session: str | None = None) -> None:
        """Write one frame, recording it first.

        Recorded before the write rather than after: a frame that fails to reach
        a dead process is still what raven tried to say, and the failure is the
        thing an operator reading the journal is looking for.

        ``session`` names the session for a frame whose own shape cannot -- the
        response to an agent-initiated request carries only the id it answers.
        """
        if self._journal is not None:
            self._journal.note("out", frame=redact_acp_frame(frame), session=session or self._frame_session(frame))
        stdin = self._proc.stdin
        if stdin is None or stdin.is_closing():
            raise AcpConnectionError(f"acp agent {self.name!r}: stdin is closed")
        try:
            stdin.write(protocol.encode(frame))
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            raise AcpConnectionError(f"acp agent {self.name!r}: write failed: {exc}") from exc

    # ---- read loops ------------------------------------------------------

    async def _read_stdout(self) -> None:
        stdout = self._proc.stdout
        if stdout is None:  # pragma: no cover - PIPE is always requested
            return
        try:
            while True:
                raw = await stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    frame = protocol.decode(line)
                except AcpProtocolError as exc:
                    # A stray non-JSON line is diagnostics leaking onto stdout,
                    # not a fatal condition: openclaw is documented to interleave
                    # plugin chatter, and killing the connection over it would
                    # turn a cosmetic problem into an outage. Journalled all the
                    # same -- it is what the agent said, and a reader asking why
                    # a turn went wrong should not have to guess that something
                    # unparseable came through.
                    if self._journal is not None:
                        self._journal.note("in", text=line)
                    logger.debug("acp agent {!r}: ignoring unparseable stdout line ({})", self.name, exc)
                    continue
                self._last_frame_at = time.monotonic()
                # Before dispatch, so a notification the router has no sink for
                # is recorded rather than dropped with only a debug line.
                if self._journal is not None:
                    self._journal.note("in", frame=frame, session=self._frame_session(frame))
                await self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must not die silently
            logger.opt(exception=True).warning("acp agent {!r}: read loop failed: {}", self.name, exc)
        finally:
            # EOF or a dead loop means no answer is ever coming for anything
            # still in flight -- for the callers awaiting a response, and for
            # the tasks composing one. Failing the first is what stops a caller
            # from awaiting a future nobody will resolve; cancelling the second
            # stops a handler (an elicitation parked on the broker) from
            # waiting out its whole budget on a connection that is gone.
            # Cancelled rather than gathered: `close` gathers, but this path
            # must not wait on a handler that ignores its cancellation.
            if not self._closed:
                for task in list(self._answer_tasks):
                    task.cancel()
                # A dying child closes stdout a beat before it is reaped and
                # before its last stderr lines are read, so composing the
                # message immediately reports "exit None; stderr: <empty>" for
                # a process that has both -- a diagnostic that points nowhere.
                # Bounded waits: a wrapper that keeps stderr open forever must
                # not park every in-flight caller behind it.
                try:
                    await asyncio.wait_for(asyncio.shield(self._proc.wait()), timeout=1.5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass
                if self._stderr_task is not None and not self._stderr_task.done():
                    await asyncio.wait({self._stderr_task}, timeout=0.5)
            self._fail_pending(
                AcpConnectionError(
                    f"acp agent {self.name!r}: connection ended (exit {self._proc.returncode}); "
                    f"stderr tail: {self.stderr_tail(400) or '<empty>'}"
                )
            )

    async def _read_stderr(self) -> None:
        """Drain the child's stderr for as long as the child is alive.

        Draining is not optional and not merely diagnostic: this is a pipe, and a
        reader that stops emptying it blocks the child at its next write once the
        kernel buffer fills. That write is usually inside
        ``logging.StreamHandler.emit``, which holds the handler lock, so every
        other thread that logs blocks behind it and the child deadlocks with no
        error anywhere. Measured on a stuck sub-agent: litellm's DEBUG request
        dumps reached 146 KiB on a single line, ``readline`` raised past its
        limit, this loop exited, and the agent loop died holding a lock it never
        saw. So the only exits are EOF and cancellation.
        """
        stderr = self._proc.stderr
        if stderr is None:  # pragma: no cover - PIPE is always requested
            return
        try:
            while True:
                try:
                    raw = await stderr.readline()
                except ValueError as exc:
                    # A line past the reader's limit. It has already cleared its
                    # buffer and resumed the transport, so the oversized line is
                    # all that is lost and draining continues.
                    logger.debug("acp agent {!r}: dropped an oversized stderr line: {}", self.name, exc)
                    continue
                if not raw:
                    break
                text = raw.decode("utf-8", "replace").rstrip()[:_STDERR_LINE_CAP]
                self._stderr.append(text)
                if self._journal is not None:
                    self._journal.note("err", text=text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - diagnostics must never break the connection
            logger.debug("acp agent {!r}: stderr reader stopped: {}", self.name, exc)

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        method = frame.get("method")
        if method is None:
            self._resolve(frame)
            return
        params = frame.get("params")
        params = params if isinstance(params, dict) else {}
        if "id" in frame:
            request_id = frame["id"]
            task = asyncio.create_task(self._answer_request(request_id, str(method), params))
            self._answer_tasks.add(task)
            if _indexable(request_id):
                self._answering[request_id] = task
                task.add_done_callback(partial(self._forget_answer, request_id))
            else:
                # Answered but not indexed. Still answered because an unanswered
                # request stalls the agent's turn for the life of the session,
                # and nothing is lost by leaving it out: a retraction naming the
                # same id could not be indexed either.
                logger.debug("acp agent {!r}: request id {!r} cannot be indexed", self.name, request_id)
                task.add_done_callback(self._answer_tasks.discard)
            return
        if method == CANCEL_REQUEST_METHOD:
            # Handled here rather than passed on: the notification carries no
            # sessionId, so a router that fans out on one can only log it as
            # unroutable.
            self._retract(params.get("requestId"))
            return
        if self._on_notification is not None:
            try:
                await self._on_notification(str(method), params)
            except Exception as exc:  # noqa: BLE001 - a bad handler must not kill the connection
                logger.opt(exception=True).warning(
                    "acp agent {!r}: notification handler for {} failed: {}", self.name, method, exc
                )

    def _retract(self, request_id: Any) -> None:
        """Stop answering a request the agent has taken back.

        Cancelling the task is the whole of it. What that reaches is whatever the
        handler was waiting on -- for an elicitation, a question standing in front
        of a person -- which is told the question is dead on its way out, the same
        as any other cancelled round trip. Nothing suppresses the response that
        may still follow: answering a retracted request is allowed, and the agent
        drops a response it no longer holds a promise for.
        """
        if not _indexable(request_id):
            logger.debug("acp agent {!r}: retraction names an unusable id {!r}", self.name, request_id)
            return
        task = self._answering.pop(request_id, None)
        if task is None:
            # Already answered, or never ours. Both are ordinary: the retraction
            # races the answer it is trying to beat.
            return
        logger.debug("acp agent {!r}: request {} was retracted, cancelling its answer", self.name, request_id)
        task.cancel()

    def _forget_answer(self, request_id: Any, task: asyncio.Task) -> None:
        """Drop both index entries, unless a later request already replaced one."""
        self._answer_tasks.discard(task)
        if self._answering.get(request_id) is task:
            del self._answering[request_id]

    async def _answer_request(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        """Answer an agent-initiated request, always with something.

        An unanswered request stalls the agent's turn, so the no-handler case is
        an explicit ``method not found`` rather than silence.
        """
        session_id = params.get("sessionId")
        asked_by = session_id if isinstance(session_id, str) else None
        if self._on_request is None:
            self._note_refusal(method, asked_by)
            await self._send_quietly(
                protocol.error_response(request_id, protocol.METHOD_NOT_FOUND, method), session=asked_by
            )
            return
        try:
            result = await self._on_request(method, params)
        except Exception as exc:  # noqa: BLE001 - the agent gets an error, raven keeps the connection
            logger.opt(exception=True).warning("acp agent {!r}: handler for {} failed: {}", self.name, method, exc)
            self._note_refusal(method, asked_by)
            await self._send_quietly(
                protocol.error_response(request_id, protocol.METHOD_NOT_FOUND, str(exc)), session=asked_by
            )
            return
        if result is UNHANDLED:
            self._note_refusal(method, asked_by)
            await self._send_quietly(
                protocol.error_response(request_id, protocol.METHOD_NOT_FOUND, method), session=asked_by
            )
            return
        await self._send_quietly(protocol.result_response(request_id, result), session=asked_by)

    def _note_refusal(self, method: str, session_id: str | None = None) -> None:
        """Record one refusal. Deduped in the log only, never in the record."""
        self._refused.append((session_id, method))
        self._refused_seen += 1
        if method not in self._refused_logged:
            self._refused_logged.add(method)
            logger.info("acp agent {!r}: no answer for {}, told it method not found", self.name, method)

    async def _send_quietly(self, frame: dict[str, Any], *, session: str | None = None) -> None:
        """Send from the request's own answer task, where nothing awaits the result.

        Broader than a connection failure on purpose: this task has no caller to
        raise to, so anything that stops the frame reaching the wire -- a dead
        connection, or a handler result `protocol.encode` cannot serialise --
        must be logged and swallowed here, or it escapes as an exception the
        task is never asked for, reported only as "Task exception was never
        retrieved" at GC time, with the request left unanswered either way.
        """
        try:
            await self._send(frame, session=session)
        except Exception as exc:  # noqa: BLE001 - the answer task has no caller to raise to
            logger.debug("acp agent {!r}: could not answer request: {}", self.name, exc)

    def _resolve(self, frame: dict[str, Any]) -> None:
        raw_id = frame.get("id")
        if not isinstance(raw_id, int):
            logger.debug("acp agent {!r}: response with non-integer id {!r}", self.name, raw_id)
            return
        future = self._pending.get(raw_id)
        if future is None or future.done():
            return
        error = frame.get("error")
        if isinstance(error, dict):
            future.set_exception(
                AcpRemoteError(
                    method="request",
                    code=int(error.get("code") or 0),
                    message=str(error.get("message") or ""),
                    data=error.get("data"),
                )
            )
            return
        future.set_result(frame.get("result"))

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


__all__ = [
    "begin_drain",
    "end_drain",
    "is_draining",
    "UNHANDLED",
    "AcpClient",
    "NotificationHandler",
    "RequestHandler",
]
