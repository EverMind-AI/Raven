"""Title-verified PTY prompt submission and addressed content acknowledgement matching."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from raven.contracts.terminal import Envelope, SendResult, TerminalError, WaitResult
from raven.terminal.host import TerminalHost, TerminalState

PASTE_CHUNK_BYTES = 16 * 1024
RENDER_QUIET_SECONDS = 1.5
RENDER_CAP_SECONDS = 8.0
VERIFY_SECONDS = 5.0
VERIFY_POLL_SECONDS = 0.05
IDLE_POLL_SECONDS = 2.0
UNKNOWN_QUIET_SECONDS = 3.0
DEFAULT_TIMEOUT_MS = 300000
_ACK = re.compile(r"(?<![\w-])ack_for=(a2a-[0-9a-f]{12})(?![\w-])")


@dataclass
class PendingSend:
    envelope: Envelope
    handle: str
    future: asyncio.Future
    created_at: float


class DeliveryService:
    def __init__(
        self,
        host: TerminalHost,
        *,
        emit: Callable[[str, dict], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.host = host
        self.emit = emit
        self.clock = clock
        self.sleep = sleep
        self.pending: dict[str, PendingSend] = {}

    async def _emit(self, event: str, payload: dict) -> None:
        if self.emit is not None:
            await self.emit(event, payload)

    @staticmethod
    def _envelope(text: str) -> Envelope | None:
        try:
            return Envelope.from_text(text)
        except ValueError:
            return None

    def _ack_candidates(self, text: str) -> tuple[Envelope | None, set[str]]:
        envelope = self._envelope(text)
        nonces = set(_ACK.findall(envelope.body if envelope else text))
        if envelope and envelope.ack_for:
            nonces.add(envelope.ack_for)
        return envelope, nonces

    def match_ack(self, handle: str, text: str) -> dict | None:
        reply, nonces = self._ack_candidates(text)
        for nonce in nonces:
            pending = self.pending.get(nonce)
            if pending is None or self.clock() - pending.created_at > DEFAULT_TIMEOUT_MS / 1000:
                continue
            original = pending.envelope
            target = original.reply_terminal or ("raven" if original.sender == "raven" else pending.handle)
            if target != handle:
                continue
            if reply is not None:
                if (reply.sender, reply.recipient, reply.scope) != (
                    original.recipient,
                    original.sender,
                    original.scope,
                ):
                    continue
            elif original.reply_terminal is None and original.sender != "raven":
                continue
            self.pending.pop(nonce)
            payload = {
                "handle": pending.handle,
                "nonce": reply.nonce if reply else None,
                "ack_for": nonce,
                "from": original.recipient,
                "to": original.sender,
            }
            if not pending.future.done():
                pending.future.set_result(payload)
            return payload
        return None

    async def receive_host(self, text: str) -> dict | None:
        payload = self.match_ack("raven", text)
        if payload is not None:
            await self._emit("a2a.ack.matched", payload)
        return payload

    def _assert_target(self, state: TerminalState, incarnation: str, permission_sequence: int) -> None:
        current = self.host.show(state.record.handle)
        if current.incarnation_id != incarnation:
            raise TerminalError("terminal_handle_stale", "Terminal incarnation changed during delivery")
        if state.startup_pending:
            raise TerminalError(
                "agent_prompt_blocked", "Agent startup readiness has not been verified", {"reason": "startup_pending"}
            )
        if current.status == "permission" or state.permission_sequence > permission_sequence:
            raise TerminalError("agent_prompt_blocked", "Agent permission is required", {"reason": "permission"})
        if not current.writable:
            raise TerminalError("terminal_not_writable", "Terminal is no longer writable")

    async def send(
        self,
        handle: str,
        text: str,
        enter: bool = True,
        require_ack: bool = False,
        *,
        ack_timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> SendResult:
        state = self.host.state(handle)
        envelope = self._envelope(text)
        if require_ack and envelope is None:
            raise TerminalError("invalid_params", "require_ack needs an addressed A-O envelope with a nonce")
        pending = None
        async with state.send_lock:
            baseline_permission = state.permission_sequence
            incarnation = state.record.incarnation_id
            self._assert_target(state, incarnation, baseline_permission)
            if state.composer_dirty:
                raise TerminalError("composer_not_empty", "The composer contains unsubmitted input")
            baseline_working = state.working_sequence
            baseline_cursor = state.show_cursor_sequence
            was_working = state.record.status == "working"
            if envelope:
                for nonce, old in list(self.pending.items()):
                    if self.clock() - old.created_at > DEFAULT_TIMEOUT_MS / 1000:
                        self.pending.pop(nonce)
                if envelope.nonce in self.pending:
                    raise TerminalError("nonce_in_use", "This nonce already identifies a pending send")
                if len(self.pending) >= 1000:
                    raise TerminalError("pending_send_limit", "Too many pending acknowledgements")
                pending = PendingSend(envelope, handle, asyncio.get_running_loop().create_future(), self.clock())
                self.pending[envelope.nonce] = pending
            try:
                paste = ("\x1b[200~" + text.replace("\x1b", "<ESC>") + "\x1b[201~").encode("utf-8")
                state.composer_dirty = True
                written = 0
                for offset in range(0, len(paste), PASTE_CHUNK_BYTES):
                    self._assert_target(state, incarnation, baseline_permission)
                    chunk = paste[offset : offset + PASTE_CHUNK_BYTES]
                    await self.host.write(handle, chunk)
                    written += len(chunk)
                render_start = self.clock()
                while self.clock() - render_start < RENDER_CAP_SECONDS:
                    self._assert_target(state, incarnation, baseline_permission)
                    rendered = state.show_cursor_sequence > baseline_cursor
                    if (
                        rendered
                        and self.clock() - max(render_start, state.last_output_monotonic) >= RENDER_QUIET_SECONDS
                    ):
                        break
                    await self.sleep(VERIFY_POLL_SECONDS)
                rendered = state.show_cursor_sequence > baseline_cursor
                self._assert_target(state, incarnation, baseline_permission)
                if enter:
                    await self.host.write(handle, b"\r")
                    written += 1
                    deadline = self.clock() + VERIFY_SECONDS
                    while True:
                        self._assert_target(state, incarnation, baseline_permission)
                        if state.working_sequence > baseline_working:
                            state.composer_dirty = False
                            result_state = "accepted"
                            break
                        if self.clock() >= deadline:
                            if was_working and rendered:
                                result_state = "queued"
                                break
                            raise TerminalError(
                                "agent_prompt_stalled", "No agent working transition verified submission"
                            )
                        await self.sleep(VERIFY_POLL_SECONDS)
                elif rendered:
                    result_state = "queued"
                else:
                    raise TerminalError("agent_prompt_stalled", "Composer rendering was not verified")
                result = SendResult(
                    handle=handle,
                    accepted=True,
                    bytes_written=written,
                    state=result_state,
                    nonce=envelope.nonce if envelope else None,
                )
            except BaseException as exc:
                if pending:
                    self.pending.pop(pending.envelope.nonce, None)
                if isinstance(exc, TerminalError):
                    failed = SendResult(
                        handle=handle,
                        accepted=False,
                        bytes_written=written,
                        state="blocked" if exc.code == "agent_prompt_blocked" else "stalled",
                        nonce=envelope.nonce if envelope else None,
                    )
                    exc.data = {**(exc.data if isinstance(exc.data, dict) else {}), **failed.model_dump(by_alias=True)}
                    await self._emit("a2a.send", {"handle": handle, "state": failed.state, "nonce": failed.nonce})
                raise
        await self._emit("a2a.send", {"handle": handle, "state": result.state, "nonce": result.nonce})
        if payload := self.match_ack(handle, text):
            await self._emit("a2a.ack.matched", payload)
        if require_ack and pending:
            try:
                await asyncio.wait_for(asyncio.shield(pending.future), max(0, ack_timeout_ms) / 1000)
            except TimeoutError as exc:
                result.content_ack = False
                raise TerminalError(
                    "content_ack_unverified",
                    "Terminal accepted the message; content ACK was not verified",
                    result.model_dump(by_alias=True),
                ) from exc
            finally:
                self.pending.pop(pending.envelope.nonce, None)
            result.content_ack = True
        return result

    async def wait(
        self, handle: str, for_condition: str = "tui-idle", timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> WaitResult:
        if for_condition not in {"tui-idle", "exit"}:
            raise TerminalError("invalid_params", "Unsupported terminal wait condition")
        state = self.host.state(handle)
        incarnation = state.record.incarnation_id
        deadline = self.clock() + max(0, timeout_ms) / 1000
        while True:
            record = self.host.show(handle)
            if record.incarnation_id != incarnation:
                raise TerminalError("terminal_handle_stale", "Terminal incarnation changed while waiting")
            if for_condition == "exit":
                if record.liveness == "exited":
                    return WaitResult(handle=handle, satisfied=True)
            elif record.status == "permission":
                return WaitResult(handle=handle, satisfied=False, blocked_reason=state.blocked_reason or "permission")
            elif record.liveness == "exited":
                return WaitResult(handle=handle, satisfied=False, blocked_reason="exited")
            elif state.startup_pending:
                if self.clock() - state.last_output_monotonic >= UNKNOWN_QUIET_SECONDS:
                    return WaitResult(
                        handle=handle,
                        satisfied=False,
                        blocked_reason=f"{state.provider or 'agent'}-startup-unverified",
                    )
            elif record.status == "idle" or (
                not state.status_seen and self.clock() - state.last_output_monotonic >= UNKNOWN_QUIET_SECONDS
            ):
                return WaitResult(handle=handle, satisfied=True)
            remaining = deadline - self.clock()
            if remaining <= 0:
                return WaitResult(handle=handle, satisfied=False, timed_out=True)
            state.changed.clear()
            timer = asyncio.create_task(self.sleep(min(IDLE_POLL_SECONDS, remaining)))
            changed = asyncio.create_task(state.changed.wait())
            try:
                await asyncio.wait([timer, changed], return_when=asyncio.FIRST_COMPLETED)
            finally:
                timer.cancel()
                changed.cancel()
                await asyncio.gather(timer, changed, return_exceptions=True)
