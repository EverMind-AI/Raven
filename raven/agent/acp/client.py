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
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from raven.agent.acp import protocol
from raven.agent.acp.journal import FrameJournal
from raven.agent.acp.protocol import (
    AcpConnectionError,
    AcpProtocolError,
    AcpRemoteError,
    AcpTimeoutError,
)

_STDERR_LINES = 200

# How many refused requests a connection remembers. Only the ones from the
# caller's own turn are ever read, so this is a bound rather than a budget.
_REFUSAL_MEMORY = 64
_STDERR_LINE_CAP = 500

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
        # Agent-initiated requests raven had no answer for. Kept because the
        # refusal is invisible from the caller's side: the agent asks, gets
        # "method not found", and whatever it does next usually arrives as a
        # turn with no content and nothing on stderr saying why.
        self._refused: deque[tuple[str | None, str]] = deque(maxlen=_REFUSAL_MEMORY)
        self._refused_seen = 0
        self._refused_logged: set[str] = set()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._unsettled_cancels: set[str] = set()
        self._stderr: deque[str] = deque(maxlen=_STDERR_LINES)
        self._closed = False
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None

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
        from raven.agent.subagent.backends.env import login_shell_env

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
        child_env = {**base_env, **(env or {})}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=child_env,
                start_new_session=True,
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
        """Kill the whole process group and stop reading. Idempotent."""
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
        # Awaited, not just cancelled: an un-awaited cancelled task logs a
        # "Task exception was never retrieved" warning on some paths, and the
        # child must be reaped here rather than left as a zombie.
        pending_tasks = [t for t in (self._reader_task, self._stderr_task) if t is not None]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        if self._proc.returncode is None:
            await self._proc.wait()
        self._fail_pending(AcpConnectionError(f"acp agent {self.name!r}: connection closed"))
        # Last, so the frames the teardown itself produced are in the file.
        if self._journal is not None:
            self._journal.close()

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
            self._journal.note("out", frame=frame, session=session or self._frame_session(frame))
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
            # still in flight. Failing them here is what stops a caller from
            # awaiting a future nobody will resolve.
            if not self._closed:
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
        stderr = self._proc.stderr
        if stderr is None:  # pragma: no cover - PIPE is always requested
            return
        try:
            while True:
                raw = await stderr.readline()
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
            await self._answer_request(frame["id"], str(method), params)
            return
        if self._on_notification is not None:
            try:
                await self._on_notification(str(method), params)
            except Exception as exc:  # noqa: BLE001 - a bad handler must not kill the connection
                logger.opt(exception=True).warning(
                    "acp agent {!r}: notification handler for {} failed: {}", self.name, method, exc
                )

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
        """Send from inside the read loop, where a write failure is not the caller's."""
        try:
            await self._send(frame, session=session)
        except AcpConnectionError as exc:
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
