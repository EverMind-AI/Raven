"""Owner-controlled Telegram DM binding; the page still owns the serial lane."""

import json
import os
import re
import stat
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path

from raven.spine import ChatType, MediaOut, Notice, NoticeKind, Origin, Text, TurnEnded, TurnFailed
from raven.spine.scheduler import SchedulerDrainingError
from raven.spine.turn import BusyPolicy


@dataclass(frozen=True)
class Binding:
    chat_id: str
    sender_id: str
    session_key: str


def load_binding(path: Path) -> Binding | None:
    """An absent file disables linking; an unsafe or malformed file refuses startup."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "getuid"):
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        raise ValueError("Telegram project linking requires POSIX file ownership checks")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Telegram project binding must be an owner-only regular file")
        try:
            data = json.loads(f.read(4097))
        except (ValueError, UnicodeError):
            raise ValueError("Invalid Telegram project binding JSON") from None
    if not isinstance(data, dict) or set(data) != {"chat_id", "sender_id", "session_key"}:
        raise ValueError("Invalid Telegram project binding fields")
    if (
        any(
            not isinstance(data[k], str) or not re.fullmatch(r"[1-9][0-9]{0,15}", data[k])
            for k in ("chat_id", "sender_id")
        )
        or data["chat_id"] != data["sender_id"]
    ):
        raise ValueError("Telegram binding requires matching positive numeric private-chat and sender IDs")
    if not isinstance(data["session_key"], str) or not re.fullmatch(r"tui:[A-Za-z0-9_-]{1,128}", data["session_key"]):
        raise ValueError("Telegram binding requires an exact existing page session key")
    return Binding(**data)


class LinkedChat:
    def __init__(self, binding, agent, hub, sources):
        self.binding = binding
        self.agent = agent
        self.hub = hub
        self.sources = sources
        self.active = {}
        self.stopping = False
        self._question_turn: ContextVar[tuple[str, str] | None] = ContextVar("linked_chat_question_turn", default=None)

    def _authorized(self, req):
        s = req.source
        return (
            req.origin is Origin.USER
            and s.channel == "telegram"
            and s.chat_type is ChatType.DM
            and s.chat_id == self.binding.chat_id
            and isinstance(s.sender_id, str)
            and re.fullmatch(re.escape(self.binding.sender_id) + r"(?:\|[A-Za-z0-9_]{1,32})?", s.sender_id) is not None
            and s.extras.get("is_group") is False
            and s.extras.get("is_forum") is False
            and type(s.extras.get("user_id")) is int
            and str(s.extras["user_id"]) == self.binding.sender_id
            and s.extras.get("message_thread_id") is None
            and req.direct_target is None
            and req.parent_session is None
            and req.deliver_text is None
            and req.delegated is None
            and req.sentinel is None
        )

    def _target_exists(self):
        key = self.binding.session_key
        return self.agent.sessions.exists(key) and self.agent.sessions.peek(key) is not None

    def channel_active(self, cid):
        return cid in self.active

    def question_route(self, cid):
        turn = self._question_turn.get()
        if turn is None:
            return False
        active = self.active.get(cid)
        return True if active is not None and turn == (cid, active[0]) else None

    async def dispatch(self, req, scheduler, broker, subagents):
        """True means handled, including a refused Telegram request; never fall back."""
        if req.source.channel != "telegram":
            return False
        if not self._authorized(req) or req.conversation is not None:
            return True

        async def reply(text):
            await self.hub.dispatch(Text(content=text, source=req.source))

        if scheduler is None or not self._target_exists():
            await reply("Linked project unavailable; no new conversation was created.")
            return True
        cid = self.binding.session_key
        command = req.text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower() if req.text.strip() else ""
        if command == "/stop":
            self.stopping = True
            try:
                stopped = await scheduler.cancel_conversation_reported(
                    cid, cancel_children=lambda: subagents.cancel_by_session(cid, reason="the user sent /stop")
                )
            finally:
                self.stopping = False
            await reply(f"Stopped {stopped} task(s)." if stopped else "No active task to stop.")
        elif command.startswith("/"):
            # Slash commands can clear history or alter runtime policy; use the page for those.
            await reply("Commands other than /stop are disabled in this linked DM; use the project page.")
        elif self.channel_active(cid) and broker.pending_req(cid) is not None:
            broker.reply(cid, req.text)
        else:
            # Never merge across surfaces: an injected DM would inherit the UI reply address.
            try:
                scheduler.submit(replace(req, conversation=cid, busy=BusyPolicy.APPEND))
            except (RuntimeError, SchedulerDrainingError):
                await reply("Linked project is not accepting messages; please retry after maintenance.")
        return True

    def wrap_runner(self, page_runner, outlet):
        from raven.gateway.spine import GatewayTurnRunner

        channel_runner = GatewayTurnRunner(self.agent, {}, self.sources)
        owner = self

        class Runner:
            async def run(self, req, emit, drain):
                if req.source.channel != "telegram" or req.conversation != owner.binding.session_key:
                    return await page_runner.run(req, emit, drain)
                if req.origin is not Origin.USER:
                    # Native result relays keep the project, not the DM's delivery address.
                    source = replace(req.source, channel="tui", chat_id=req.conversation.removeprefix("tui:"))

                    async def page_emit(event):
                        await emit(replace(event, source=source))

                    return await page_runner.run(replace(req, source=source), page_emit, drain)
                if not owner._authorized(req):
                    raise ValueError("Unauthorized linked project request")
                owner.active[req.conversation] = (req.turn_id, req.source)
                if not owner._target_exists():
                    raise ValueError("Linked project no longer exists")
                token = owner._question_turn.set((req.conversation, req.turn_id))
                try:
                    await outlet.emit_start(req.conversation, req.turn_id, req.text)
                    # UI injects remain in the mailbox and fall back to their own native turn.
                    return await channel_runner.run(req, emit, lambda: [])
                finally:
                    owner._question_turn.reset(token)

        return Runner()

    def wrap_sink(self, page_sink, outlet):
        async def sink(event):
            if isinstance(event, (TurnEnded, TurnFailed)):
                active = self.active.get(event.conversation_id)
                if active is not None and active[0] == event.turn_id:
                    self.active.pop(event.conversation_id, None)
                    self.sources.pop(event.conversation_id, None)
                    if isinstance(event, TurnFailed) and not event.cancelled:
                        await self.hub.dispatch(Text(content="Sorry, I encountered an error.", source=active[1]))
                    self.agent.notify_turn_complete()
                await page_sink(event)
                if (
                    isinstance(event, TurnFailed)
                    and event.cancelled
                    and event.conversation_id == self.binding.session_key
                    and (self.stopping or active is not None)
                ):
                    await outlet.emit_error(
                        event.conversation_id, -32099, "turn_cancelled", "cancelled_by_client", turn_id=event.turn_id
                    )
            elif getattr(event, "source", None) is not None and event.source.channel == "telegram":
                # No tokens, reasoning, tools or progress mirrored into Telegram.
                if (
                    isinstance(event, (Text, MediaOut))
                    or isinstance(event, Notice)
                    and event.kind is NoticeKind.ACTION_BLOCKED
                ):
                    await self.hub.dispatch(event)
                    source = replace(event.source, channel="tui", chat_id=event.conversation_id.removeprefix("tui:"))
                    await page_sink(replace(event, source=source))
            else:
                await page_sink(event)

        return sink
