"""Tests for the legacy-callback adapter hooks.

Each adapter wraps a pre-existing AgentLoop constructor parameter so
the legacy invocation semantics are preserved while internal call
sites go through a single ``CompositeHook`` chain.

Two layers of coverage here:

1. **Adapter-level** unit tests — exercise each adapter in isolation
   (no AgentLoop) so we pin the translation contract.
2. **AgentLoop wire-up** tests — verify the constructor actually builds
   ``self.hooks`` with the expected composition order from the three
   legacy params, and that an explicit ``hooks`` argument is inserted
   between the on-user-inbound and response-modifier slots.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from raven.agent.hook import (
    AgentHook,
    AgentHookContext,
    CompositeHook,
    DecisionConsumerAdapter,
    OnUserInboundAdapter,
    ResponseModifierAdapter,
)
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


@dataclass
class _Reply:
    channel: str
    chat_id: str
    content: str
    media: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _msg(text: str = "hello", **extras: Any) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel="cli",
            chat_id="c1",
            sender_id="user",
            chat_type=ChatType.DM,
            extras=extras or {},
        ),
        text=text,
    )


@pytest.fixture
def ctx():
    return AgentHookContext(session_key="cli:test", turn_request=_msg())


# ===========================================================================
# OnUserInboundAdapter
# ===========================================================================


class TestOnUserInboundAdapter:
    async def test_sync_callback_invoked(self, ctx):
        recorded = []

        adapter = OnUserInboundAdapter(lambda m: recorded.append(m.text))
        decision = await adapter.before_user_inbound(ctx)

        assert recorded == ["hello"]
        # Observer — never short-circuits.
        assert decision.short_circuit_result is None
        assert decision.modified_content is None

    async def test_async_callback_awaited(self, ctx):
        recorded = []

        async def cb(m):
            recorded.append(m.text)

        adapter = OnUserInboundAdapter(cb)
        await adapter.before_user_inbound(ctx)
        assert recorded == ["hello"]

    async def test_sync_exception_swallowed(self, ctx):
        def bad(m):
            raise RuntimeError("boom")

        adapter = OnUserInboundAdapter(bad)
        # Should NOT raise — observer must not crash chain.
        decision = await adapter.before_user_inbound(ctx)
        assert decision.short_circuit_result is None

    async def test_async_exception_swallowed(self, ctx):
        async def bad(m):
            raise RuntimeError("boom")

        adapter = OnUserInboundAdapter(bad)
        decision = await adapter.before_user_inbound(ctx)
        assert decision.short_circuit_result is None

    async def test_no_inbound_message_is_noop(self):
        called = []
        ctx_no_msg = AgentHookContext(session_key="cli:test")  # turn_request=None
        adapter = OnUserInboundAdapter(lambda m: called.append(m))
        await adapter.before_user_inbound(ctx_no_msg)
        assert called == []

    async def test_other_phases_are_passthrough(self, ctx):
        adapter = OnUserInboundAdapter(lambda m: None)
        for phase in (
            "before_iteration",
            "before_execute_tools",
            "after_iteration",
            "after_send",
        ):
            decision = await getattr(adapter, phase)(ctx)
            assert decision.short_circuit_result is None
            assert decision.modified_content is None

    def test_name(self):
        adapter = OnUserInboundAdapter(lambda m: None)
        assert adapter.name == "Legacy(on_user_inbound)"


# ===========================================================================
# DecisionConsumerAdapter
# ===========================================================================


class TestDecisionConsumerAdapter:
    async def test_none_return_means_pass_through(self, ctx):
        cb = AsyncMock(return_value=None)
        adapter = DecisionConsumerAdapter(cb)

        decision = await adapter.before_user_inbound(ctx)

        assert decision.short_circuit_result is None
        cb.assert_awaited_once()

    async def test_non_none_return_short_circuits(self, ctx):
        handled = _Reply(channel="cli", chat_id="c1", content="menu picked")
        cb = AsyncMock(return_value=handled)
        adapter = DecisionConsumerAdapter(cb)

        decision = await adapter.before_user_inbound(ctx)

        # The reply is normalized to a (content, media) tuple.
        assert decision.short_circuit_result == ("menu picked", [])

    async def test_exception_treated_as_pass_through(self, ctx):
        async def boom(_):
            raise RuntimeError("boom")

        adapter = DecisionConsumerAdapter(boom)
        decision = await adapter.before_user_inbound(ctx)
        assert decision.short_circuit_result is None

    async def test_no_inbound_message_is_noop(self):
        cb = AsyncMock(return_value="should not see")
        ctx_no_msg = AgentHookContext(session_key="cli:test")
        adapter = DecisionConsumerAdapter(cb)
        decision = await adapter.before_user_inbound(ctx_no_msg)
        assert decision.short_circuit_result is None
        cb.assert_not_awaited()

    def test_name(self):
        adapter = DecisionConsumerAdapter(AsyncMock())
        assert adapter.name == "Legacy(decision_consumer)"


# ===========================================================================
# ResponseModifierAdapter
# ===========================================================================


class TestResponseModifierAdapter:
    async def test_modified_content_propagated(self):
        ctx = AgentHookContext(session_key="cli:test", outbound_content="hi")
        adapter = ResponseModifierAdapter(lambda key, content: f"{content} | session={key}")
        decision = await adapter.after_send(ctx)
        assert decision.modified_content == "hi | session=cli:test"

    async def test_unchanged_content_returns_passthrough(self):
        # If the modifier returns the same string, we must not signal a
        # change (so downstream content-chaining ignores the call).
        ctx = AgentHookContext(session_key="cli:test", outbound_content="hi")
        adapter = ResponseModifierAdapter(lambda k, c: c)
        decision = await adapter.after_send(ctx)
        assert decision.modified_content is None

    async def test_empty_content_still_invokes_callback(self):
        called = []

        def cb(key, content):
            called.append((key, content))
            return content + " appended"

        ctx = AgentHookContext(session_key="cli:test", outbound_content=None)
        adapter = ResponseModifierAdapter(cb)
        decision = await adapter.after_send(ctx)

        # Called with an empty string (legacy behavior — modifiers can
        # inject into an otherwise empty reply).
        assert called == [("cli:test", "")]
        assert decision.modified_content == " appended"

    async def test_exception_swallowed(self):
        def boom(k, c):
            raise RuntimeError("boom")

        ctx = AgentHookContext(session_key="cli:test", outbound_content="hi")
        adapter = ResponseModifierAdapter(boom)
        decision = await adapter.after_send(ctx)
        # No-op on exception — content unchanged.
        assert decision.modified_content is None

    async def test_non_string_return_treated_as_no_change(self):
        # Defensive: if the legacy modifier returns a non-string (None,
        # a future Sentinel might return an envelope), we should not
        # corrupt outbound_content.
        adapter = ResponseModifierAdapter(lambda k, c: None)  # type: ignore[arg-type]
        ctx = AgentHookContext(session_key="cli:test", outbound_content="hi")
        decision = await adapter.after_send(ctx)
        assert decision.modified_content is None

    async def test_other_phases_are_passthrough(self):
        adapter = ResponseModifierAdapter(lambda k, c: c + "!")
        ctx = AgentHookContext(session_key="cli:test")
        for phase in (
            "before_user_inbound",
            "before_iteration",
            "before_execute_tools",
            "after_iteration",
        ):
            decision = await getattr(adapter, phase)(ctx)
            assert decision.short_circuit_result is None
            assert decision.modified_content is None

    def test_name(self):
        adapter = ResponseModifierAdapter(lambda k, c: c)
        assert adapter.name == "Legacy(response_modifier)"


# ===========================================================================
# AgentLoop wire-up
# ===========================================================================


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _StubProvider:
    api_key = "test"

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def chat_with_retry(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def _make_agent(workspace, **kwargs):
    from raven.agent.loop import AgentLoop

    return AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        **kwargs,
    )


class TestAgentLoopHookWireUp:
    """Verify the constructor builds ``self.hooks`` from the three
    legacy params in the documented order."""

    def test_empty_composite_when_no_callbacks(self, workspace):
        agent = _make_agent(workspace)
        assert isinstance(agent.hooks, CompositeHook)
        assert len(agent.hooks) == 0

    def test_on_user_inbound_only(self, workspace):
        agent = _make_agent(workspace, on_user_inbound=lambda m: None)
        hooks = list(agent.hooks)
        assert len(hooks) == 1
        assert isinstance(hooks[0], OnUserInboundAdapter)

    def test_decision_consumer_only(self, workspace):
        agent = _make_agent(workspace, decision_consumer=AsyncMock())
        hooks = list(agent.hooks)
        assert len(hooks) == 1
        assert isinstance(hooks[0], DecisionConsumerAdapter)

    def test_response_modifier_only(self, workspace):
        agent = _make_agent(workspace, response_modifier=lambda k, c: c)
        hooks = list(agent.hooks)
        assert len(hooks) == 1
        assert isinstance(hooks[0], ResponseModifierAdapter)

    def test_canonical_ordering(self, workspace):
        """Order: OnUserInbound → DecisionConsumer → [explicit hooks] → ResponseModifier."""

        class Explicit(AgentHook):
            @property
            def name(self) -> str:
                return "Explicit"

        agent = _make_agent(
            workspace,
            on_user_inbound=lambda m: None,
            decision_consumer=AsyncMock(),
            response_modifier=lambda k, c: c,
            hooks=CompositeHook([Explicit()]),
        )

        hooks = list(agent.hooks)
        assert len(hooks) == 4
        assert isinstance(hooks[0], OnUserInboundAdapter)
        assert isinstance(hooks[1], DecisionConsumerAdapter)
        assert isinstance(hooks[2], Explicit)
        assert isinstance(hooks[3], ResponseModifierAdapter)

    async def test_user_inbound_observer_fires_when_no_short_circuit(self, workspace):
        received = []
        agent = _make_agent(
            workspace,
            on_user_inbound=lambda m: received.append(m.text),
            decision_consumer=AsyncMock(return_value=None),
        )

        # Drive _process_message indirectly — we only care that the
        # observer adapter was invoked through the hook chain.
        ctx = AgentHookContext(
            session_key="cli:c1",
            turn_request=_msg("hello"),
        )
        await agent.hooks.before_user_inbound(ctx)
        assert received == ["hello"]

    async def test_decision_consumer_short_circuit_halts_observer(self, workspace):
        """With both adapters wired, the canonical order has observer
        BEFORE short-circuiter, so engagement observation is preserved
        even when /pick replies short-circuit."""
        received = []
        decision_handled = _Reply(channel="cli", chat_id="c1", content="OUT")

        agent = _make_agent(
            workspace,
            on_user_inbound=lambda m: received.append(m.text),
            decision_consumer=AsyncMock(return_value=decision_handled),
        )

        ctx = AgentHookContext(
            session_key="cli:c1",
            turn_request=_msg("/pick 1"),
        )
        decision = await agent.hooks.before_user_inbound(ctx)

        # Observer ran before short-circuit halted the chain.
        assert received == ["/pick 1"]
        assert decision.short_circuit_result == ("OUT", [])

    async def test_response_modifier_invoked_in_after_send(self, workspace):
        agent = _make_agent(
            workspace,
            response_modifier=lambda k, c: c + " [appended]",
        )
        ctx = AgentHookContext(session_key="cli:c1", outbound_content="hi")
        decision = await agent.hooks.after_send(ctx)
        assert decision.modified_content == "hi [appended]"
