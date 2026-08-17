"""What the web channel has to carry before a browser can chat with an instance.

Both cases here are wiring, not mechanism: the runtime side of direct chat was
built for the TUI and is transport-agnostic, so what stood between it and the
web UI was a parameter that was never threaded and a method group that was never
registered. Neither fails loudly -- an untagged event looks like the main
agent's, and a missing method looks like an empty instance list -- which is why
they are pinned here rather than left to the round-trip test.
"""

from __future__ import annotations

from types import SimpleNamespace

from raven.rpc.dispatcher import Dispatcher
from raven.spine import ChatType, Origin, Source, Text, TurnOutcome, TurnRequest, Usage
from raven.web_rpc.methods import register_web_methods
from raven.web_rpc.spine import build_web


class _Emitter:
    """Records (session_key, event) -- stands in for SubscriptionEmitter."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, session_key: str, event: dict) -> None:
        self.emitted.append((session_key, event))


class _Loop:
    """Fake AgentLoop emitting one scripted event."""

    def __init__(self) -> None:
        self.tools: dict = {}

    async def run_turn(self, req, emit, drain, **kw) -> TurnOutcome:
        await emit(Text(content="done"))
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)


def _src() -> Source:
    return Source(channel="web", chat_id="c1", sender_id="user", chat_type=ChatType.DM)


async def test_the_web_spine_tags_a_direct_turns_events_with_its_target() -> None:
    """The browser demultiplexes on ``target``; nothing else distinguishes a
    sub-agent's reply from the main agent's on a shared subscription.

    ``build_web`` has to hand the caller's map to the outlet rather than letting
    ``build_rpc_spine`` mint its own. Dropping the passthrough is not a partial
    failure: every event arrives untagged, so a direct chat renders as the main
    agent answering.
    """
    emitter = _Emitter()
    targets = {"web:c1": {"agent": "Raven-Code", "handle": "refactor-auth"}}
    scheduler, _hub, turn_ids, teardown = build_web(_Loop(), emitter, direct_targets=targets)
    try:
        turn_ids["web:c1"] = "t1"
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="web:c1"))
        await handle.result()
    finally:
        await teardown()

    tagged = [e["payload"].get("target") for _k, e in emitter.emitted]
    assert tagged == [{"agent": "Raven-Code", "handle": "refactor-auth"}] * len(tagged)
    assert tagged, "the turn emitted nothing to tag"


async def test_a_main_agent_turn_on_the_web_channel_stays_untagged() -> None:
    """Paired with the case above so the tag cannot be unconditional."""
    emitter = _Emitter()
    scheduler, _hub, turn_ids, teardown = build_web(_Loop(), emitter, direct_targets={})
    try:
        turn_ids["web:c1"] = "t1"
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="web:c1"))
        await handle.result()
    finally:
        await teardown()

    assert all("target" not in e["payload"] for _k, e in emitter.emitted)


def test_the_web_channel_serves_the_methods_a_direct_chat_needs() -> None:
    """The browser's whole vocabulary, asserted against the real registrar.

    The gateway builds a *second* dispatcher for the browser, and a method left
    off it fails as "empty list" or "feature missing" rather than as a wiring
    mistake -- an instance it cannot list is one it cannot address, and a chat
    reopened with no history reads as a lost conversation.

    Driven through ``register_web_methods``, which is what the gateway calls, so
    dropping a group there turns this red. Asserting against a dispatcher this
    test populated itself would pass whatever the gateway does.
    """
    dispatcher = Dispatcher()
    register_web_methods(
        dispatcher,
        emitter=_Emitter(),
        scheduler=None,
        turn_ids={},
        direct_targets={},
        agent=None,
        cron=None,
        config=None,
        channel_manager=None,
        raven_config=None,
    )
    served = set(dispatcher.methods())
    assert {"turn.send", "subagents.instances", "subagents.instance.history"} <= served


async def test_the_web_channel_reads_history_through_the_live_loop(tmp_path) -> None:
    """Registering the method is not the same as being able to answer it.

    ``instances_history`` resolves a session's record directory through the
    manager, so with no ``agent_loop_factory`` it answers ``{"turns": []}`` --
    an instance whose record is on disk reads as a conversation that never
    happened, which is indistinguishable from a genuinely empty one. Measured
    against a real record before the factory was passed.
    """
    seen: list[str] = []

    class _Manager:
        def _session_dir(self, session_key: str):
            seen.append(session_key)
            return tmp_path

    dispatcher = Dispatcher()
    register_web_methods(
        dispatcher,
        emitter=_Emitter(),
        scheduler=None,
        turn_ids={},
        direct_targets={},
        agent=SimpleNamespace(subagents=_Manager()),
        cron=None,
        config=None,
        channel_manager=None,
        raven_config=None,
    )
    await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "subagents.instance.history",
            "params": {"session_key": "web:s1", "agent": "Coder", "handle": "h"},
        }
    )
    assert seen == ["web:s1"], "the handler never reached the live loop"
