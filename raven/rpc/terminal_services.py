"""Serve-owned assembly of terminal hosting, identity, delivery and event streams."""

from __future__ import annotations

import asyncio

from raven.agent.registry.identity import IdentityRegistry
from raven.contracts.terminal import TerminalError
from raven.rpc.methods.agents import register_agents_methods
from raven.rpc.methods.terminal import register_terminal_methods
from raven.rpc.terminal_stream import TerminalStream
from raven.security.trust import wrap_untrusted
from raven.terminal.deliver import DeliveryService
from raven.terminal.host import TerminalHost
from raven.tracing import trace


class TerminalServices:
    def __init__(self, emitter, broadcast, *, host=None, delivery=None, identities=None):
        self.emitter = emitter
        self.identities = identities if identities is not None else IdentityRegistry()
        self.host = host if host is not None else TerminalHost(emit=self.emit)
        self.identities.terminal_show = self.host.show
        self.identities.reconcile()
        self.identities.ensure_host()
        self.delivery = delivery if delivery is not None else DeliveryService(self.host, emit=self.emit)
        self.stream = TerminalStream(self.host, broadcast, emitter)
        self.worktrees: set[str] = set()
        self.conversations: dict[str, str] = {}
        self.sessions = None

    def register(self, dispatcher):
        register_terminal_methods(
            dispatcher,
            host=self.host,
            delivery=self.delivery,
            stream=self.stream,
            receive_host=self.receive_host,
            bind_session=self.bind_session,
        )
        register_agents_methods(dispatcher, registry=self.identities, terminal_show=self.host.show)

    def bind_session(self, handle, session):
        self.host.show(handle)
        self.conversations.setdefault(handle, session)

    async def receive_host(self, text, source_handle=None):
        if self.delivery is None:
            raise TerminalError("terminal_unavailable", "Terminal delivery service is unavailable")
        matched = await self.delivery.receive_host(text)
        handle = source_handle or (matched.get("handle") if matched else None)
        conversation = self.conversations.get(handle)
        if conversation is None and handle:
            record = self.host.show(handle)
            candidates = {
                getattr(identity, "session_key", None)
                for identity in self.identities.list()
                if identity.binding is not None
                and identity.binding.handle == handle
                and identity.binding.incarnation_id == record.incarnation_id
                and getattr(identity, "session_key", None)
            }
            if len(candidates) == 1:
                conversation = candidates.pop()
        if conversation is None or self.sessions is None:
            raise TerminalError("host_conversation_not_found", "The reply has no originating Raven conversation")
        notice = {"kind": "terminal_reply", "detail": text}
        session = self.sessions.peek(conversation)
        if session is None:
            raise TerminalError("host_conversation_not_found", "The originating Raven session no longer exists")
        session.add_message("assistant", wrap_untrusted(text, source="peer terminal reply"), notice=notice)
        self.sessions.save(session)
        await self.emitter.emit(conversation, {"type": "notice", "payload": notice})
        return {
            "handle": None,
            "to": "raven",
            "accepted": True,
            "bytesWritten": 0,
            "state": "delivered_to_host",
            "contentAck": matched is not None,
        }

    async def emit(self, event, payload):
        handle = payload.get("handle")
        worktree = payload.get("worktreeId") or payload.get("worktree_id")
        if worktree is None and handle:
            try:
                worktree = self.host.show(handle).worktree_id
            except TerminalError:
                pass
        if worktree:
            self.worktrees.add(worktree)
            await self.emitter.emit(worktree, {"type": event, "payload": payload})
        if event == "terminal.closed" and handle:
            await asyncio.to_thread(self.identities.mark_exited, handle)
        with trace.span(event, attributes={f"terminal.{key}": value for key, value in payload.items()}):
            pass

    def attach(self, app, port):
        self.host.hook_url = f"http://127.0.0.1:{port}/terminal/hook"
        self.host.install_hook_route(app)

    async def shutdown(self):
        await self.stream.shutdown()
        await self.host.shutdown()
        for worktree in self.worktrees:
            await self.emitter.close_session(worktree)
