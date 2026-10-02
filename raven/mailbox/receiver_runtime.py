"""One-shot mailbox pointer notifications at verified terminal or native boundaries."""

import asyncio
import json
from uuid import UUID, uuid5

from raven.contracts.mailbox import MailboxError
from raven.contracts.terminal import TerminalError
from raven.mailbox.codec import canonical_bytes
from raven.mailbox.db import canonical_id
from raven.mailbox.notifications import MailboxNotifications
from raven.spine.events import TurnStarted
from raven.spine.message import ChatType, Source
from raven.spine.turn import BusyPolicy, Origin, TurnRequest


class MailboxReceiver:
    """Notify explicitly enrolled receivers without claiming or completing messages."""

    def __init__(self, receivers, *, host=None, delivery=None, scheduler=None, sessions=None, channel="tui"):
        self.receivers = receivers
        self.notifications = MailboxNotifications(receivers.store)
        self.host = host
        self.delivery = delivery
        self.scheduler = scheduler
        self.sessions = sessions
        self.channel = channel
        self._watcher = None

    @staticmethod
    def _pointer(binding, request_id, message_ids):
        return (
            "Mailbox work is available. Use your scoped mailbox tools to poll and inspect it.\n"
            + canonical_bytes(
                {
                    "binding_id": binding.binding_id,
                    "request_id": canonical_id(request_id),
                    "message_ids": message_ids,
                    "scope": binding.scope,
                }
            ).decode()
        )

    def _terminal_guard(self, binding, *, initial=False):
        try:
            self.receivers.current(binding.binding_id)
        except MailboxError as error:
            raise TerminalError(error.code, "Receiver binding is no longer current") from None
        state = self.host.state(binding.terminal_handle)
        if state.provider not in {"claude", "codex"}:
            raise TerminalError("receiver_capability_unavailable", "Automatic input is unavailable for this provider")
        if self.host.show(binding.terminal_handle).status != "idle":
            raise TerminalError("receiver_busy", "Receiver is not at an idle input boundary")
        if state.human_input_pending or (initial and state.composer_dirty):
            raise TerminalError("composer_not_empty", "Receiver composer contains unsubmitted input")

    def _preflight(self, binding):
        if binding.terminal_handle:
            if "terminal_notify" not in binding.capabilities or self.host is None or self.delivery is None:
                raise TerminalError("receiver_capability_unavailable", "Receiver requires explicit poll")
            state = self.host.state(binding.terminal_handle)
            self.delivery._assert_target(state, binding.terminal_incarnation, state.permission_sequence)
            self._terminal_guard(binding, initial=True)
        else:
            if "native_notify" not in binding.capabilities or self.scheduler is None or self.sessions is None:
                raise TerminalError("receiver_capability_unavailable", "Receiver requires explicit poll")
            session = self.sessions.peek(binding.session_key)
            if session is None:
                raise TerminalError("receiver_offline", "Native session is unavailable")
            if session.pending_clarification:
                raise TerminalError("receiver_human_gate", "Native session is waiting for a Human answer")
            if self.scheduler.has_work(binding.session_key):
                raise TerminalError("receiver_busy", "Native session has queued or running work")

    async def notify(self, binding_id, *, request_id, message_ids):
        binding = self.receivers.current(binding_id)
        row = self.notifications.prepare(binding, request_id=request_id, message_ids=message_ids)
        if row["stage"] not in {"prepared", "blocked"}:
            return row
        try:
            self._preflight(binding)
        except TerminalError as error:
            return self.notifications.record(request_id, "blocked", detail=error.code)
        turn_id = canonical_id(request_id) if not binding.terminal_handle else None
        if not self.notifications.begin(request_id, turn_id=turn_id):
            return self.notifications.status(request_id)
        text = self._pointer(binding, request_id, row["message_ids"])
        try:
            binding = self.receivers.current(binding_id)
            if binding.terminal_handle:
                first = True

                def guard():
                    nonlocal first
                    self._terminal_guard(binding, initial=first)
                    first = False

                sent = await self.delivery.send(
                    binding.terminal_handle, text, require_ack=False, force=False, guard=guard
                )
                if not sent.accepted:
                    return self.notifications.record(
                        request_id, "uncertain", bytes_written=sent.bytes_written, detail="submission_unverified"
                    )
                return self.notifications.record(request_id, "input_accepted", bytes_written=sent.bytes_written)
            self._preflight(binding)
            _, _, chat_id = binding.session_key.partition(":")
            self.scheduler.submit(
                TurnRequest(
                    origin=Origin.SUBAGENT,
                    source=Source(self.channel, chat_id, "mailbox", ChatType.DM),
                    text=text,
                    conversation=binding.session_key,
                    turn_id=turn_id,
                    busy=BusyPolicy.APPEND,
                )
            )
            return self.notifications.record(request_id, "input_accepted", turn_id=turn_id)
        except asyncio.CancelledError:
            self.notifications.record(request_id, "uncertain", detail="submission_cancelled")
            raise
        except (MailboxError, TerminalError) as error:
            data = error.data if isinstance(error, TerminalError) and isinstance(error.data, dict) else {}
            return self.notifications.record(
                request_id, "uncertain", bytes_written=data.get("bytesWritten", 0), detail=error.code
            )
        except Exception:
            self.notifications.record(request_id, "uncertain", detail="submission_error")
            raise

    async def on_turn_event(self, event):
        if not isinstance(event, TurnStarted) or event.origin is not Origin.SUBAGENT:
            return
        try:
            row = self.notifications.status(event.turn_id)
        except MailboxError as error:
            if error.code in {"notification_not_found", "invalid_identity"}:
                return
            raise
        binding = self.receivers.current(row["binding_id"])
        if binding.terminal_handle or row["turn_id"] != event.turn_id or binding.session_key != event.conversation_id:
            return
        self.notifications.record(row["request_id"], "turn_started", turn_id=event.turn_id)

    async def scan(self):
        """Find unnotified scoped work; a prior uncertain submission excludes its IDs."""
        with self.receivers.store.db.connection() as conn:
            bindings = [
                row[0] for row in conn.execute("SELECT binding_id FROM receiver_bindings WHERE revoked_at IS NULL")
            ]
        for binding_id in bindings:
            try:
                binding = self.receivers.current(binding_id)
                if not binding.capabilities & {"terminal_notify", "native_notify"}:
                    continue
                with self.receivers.store.db.connection() as conn:
                    submitted = set()
                    for row in conn.execute(
                        "SELECT message_ids FROM notifications WHERE binding_id=? AND stage "
                        "IN ('submitting','uncertain','input_accepted','turn_started')",
                        (binding_id,),
                    ):
                        submitted.update(json.loads(row[0]))
                messages = self.receivers.store.peek(
                    binding.ref,
                    limit=16,
                    allowed_scopes=[binding.scope],
                    exclude_message_ids=submitted,
                    now=self.notifications._now(None),
                )
                ids = [message["message_id"] for message in messages]
                if ids:
                    request_id = str(
                        uuid5(
                            UUID(binding_id),
                            canonical_bytes(
                                {
                                    "message_ids": ids,
                                    "terminal_incarnation": binding.terminal_incarnation,
                                }
                            ).decode(),
                        )
                    )
                    await self.notify(binding_id, request_id=request_id, message_ids=ids)
            except (MailboxError, TerminalError):
                continue

    def start(self):
        """Start one task only after explicit automatic-notification enrollment."""
        if self._watcher is None or self._watcher.done():
            self.notifications.recover()
            self._watcher = asyncio.create_task(self._watch())

    async def _watch(self):
        while True:
            await self.scan()
            await asyncio.sleep(2)

    async def close(self):
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
            self._watcher = None
