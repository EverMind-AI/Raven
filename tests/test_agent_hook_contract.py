"""Contract tests for AgentHook + CompositeHook.

Pins the contract that the legacy-callback adapters rely on when porting
AgentLoop's existing scattered callback fields to hooks, and that the
eval_engine relies on when implementing its three iteration-phase hooks.

The hooks live as a standalone abstraction in ``raven/agent/hook/``.
This test file's job is to lock in the eight contract dimensions:

1. Default no-op for every phase.
2. ``HookDecision`` default state.
3. ``AgentHookContext`` field defaults.
4. ``CompositeHook`` empty.
5. ``CompositeHook`` single-member dispatch.
6. ``CompositeHook`` registration-order preservation.
7. ``CompositeHook`` short-circuit halts the chain.
8. ``CompositeHook`` ``after_send`` content chains through.

Plus exception-isolation (a hook that raises is treated as no-op,
chain continues) and late-registration via ``append``.
"""

from __future__ import annotations

import pytest

from raven.agent.hook import (
    AgentHook,
    AgentHookContext,
    CompositeHook,
    HookDecision,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Minimal context — only the required ``session_key`` is set."""
    return AgentHookContext(session_key="cli:test")


# ---------------------------------------------------------------------------
# 1. Default no-op for every phase
# ---------------------------------------------------------------------------


PHASES = [
    "before_user_inbound",
    "before_iteration",
    "before_execute_tools",
    "after_iteration",
    "after_send",
]


class TestAgentHookDefaults:
    """All phases on the bare ``AgentHook`` are pass-through no-ops."""

    @pytest.mark.parametrize("phase", PHASES)
    async def test_default_returns_passthrough(self, ctx, phase):
        class Bare(AgentHook):
            pass

        hook = Bare()
        decision = await getattr(hook, phase)(ctx)
        assert isinstance(decision, HookDecision)
        assert decision.pass_through is True
        assert decision.short_circuit_result is None
        assert decision.modified_content is None

    def test_name_defaults_to_class_name(self):
        class Foo(AgentHook):
            pass

        assert Foo().name == "Foo"

    def test_name_can_be_overridden(self):
        class Bar(AgentHook):
            @property
            def name(self) -> str:
                return "Custom"

        assert Bar().name == "Custom"


# ---------------------------------------------------------------------------
# 2. HookDecision dataclass
# ---------------------------------------------------------------------------


class TestHookDecision:
    def test_default_state(self):
        d = HookDecision()
        assert d.pass_through is True
        assert d.short_circuit_result is None
        assert d.modified_content is None
        assert d.notes == []

    def test_short_circuit_carries_arbitrary_value(self):
        d = HookDecision(short_circuit_result="STOP")
        assert d.short_circuit_result == "STOP"

        d2 = HookDecision(short_circuit_result={"answer": 42})
        assert d2.short_circuit_result == {"answer": 42}

    def test_modified_content_separate_from_short_circuit(self):
        d = HookDecision(modified_content="hello world")
        assert d.modified_content == "hello world"
        assert d.short_circuit_result is None

    def test_notes_accumulator(self):
        d = HookDecision(notes=["one", "two"])
        assert d.notes == ["one", "two"]


# ---------------------------------------------------------------------------
# 3. AgentHookContext
# ---------------------------------------------------------------------------


class TestAgentHookContext:
    def test_session_key_is_required(self):
        with pytest.raises(TypeError):
            AgentHookContext()  # type: ignore[call-arg]

    def test_default_fields(self):
        c = AgentHookContext(session_key="cli:default")
        assert c.session_key == "cli:default"
        assert c.turn_request is None
        assert c.iteration is None
        assert c.messages is None
        assert c.tools is None
        assert c.response is None
        assert c.outbound_content is None
        assert c.metadata == {}

    def test_fields_can_be_set(self):
        c = AgentHookContext(
            session_key="cli:x",
            iteration=3,
            outbound_content="hi",
            metadata={"source": "test"},
        )
        assert c.iteration == 3
        assert c.outbound_content == "hi"
        assert c.metadata == {"source": "test"}


# ---------------------------------------------------------------------------
# 4. CompositeHook empty
# ---------------------------------------------------------------------------


class TestCompositeHookEmpty:
    def test_zero_length(self):
        composite = CompositeHook()
        assert len(composite) == 0

    def test_name_shows_empty(self):
        assert CompositeHook().name == "CompositeHook(empty)"

    @pytest.mark.parametrize("phase", PHASES)
    async def test_phase_passthrough_when_empty(self, ctx, phase):
        composite = CompositeHook()
        decision = await getattr(composite, phase)(ctx)
        assert decision.short_circuit_result is None
        assert decision.modified_content is None


# ---------------------------------------------------------------------------
# 5. CompositeHook single-member dispatch
# ---------------------------------------------------------------------------


class TestCompositeHookSingleMember:
    async def test_single_hook_called(self, ctx):
        called: list[tuple[str, str]] = []

        class Recorder(AgentHook):
            async def before_iteration(self, ctx):
                called.append(("before_iteration", ctx.session_key))
                return HookDecision()

        composite = CompositeHook([Recorder()])
        assert len(composite) == 1
        await composite.before_iteration(ctx)
        assert called == [("before_iteration", "cli:test")]

    async def test_unrelated_phase_does_not_call_hook(self, ctx):
        called: list[str] = []

        class OnlyBefore(AgentHook):
            async def before_iteration(self, ctx):
                called.append("before_iteration")
                return HookDecision()

        composite = CompositeHook([OnlyBefore()])
        # Calling a phase the hook didn't override
        await composite.after_iteration(ctx)
        assert called == []  # before_iteration recorder never ran


# ---------------------------------------------------------------------------
# 6. Registration order preserved
# ---------------------------------------------------------------------------


class TestCompositeHookRegistrationOrder:
    async def test_hooks_invoked_in_order(self, ctx):
        order: list[str] = []

        class Recorder(AgentHook):
            def __init__(self, tag: str) -> None:
                self.tag = tag

            @property
            def name(self) -> str:
                return f"R({self.tag})"

            async def before_iteration(self, ctx):
                order.append(self.tag)
                return HookDecision()

        composite = CompositeHook([Recorder("first"), Recorder("second"), Recorder("third")])
        await composite.before_iteration(ctx)
        assert order == ["first", "second", "third"]

    async def test_name_reflects_order(self):
        class Tagged(AgentHook):
            def __init__(self, tag: str) -> None:
                self.tag = tag

            @property
            def name(self) -> str:
                return self.tag

        composite = CompositeHook([Tagged("A"), Tagged("B")])
        assert composite.name == "CompositeHook(A, B)"


# ---------------------------------------------------------------------------
# 7. Short-circuit halts the chain
# ---------------------------------------------------------------------------


class TestCompositeHookShortCircuit:
    async def test_first_short_circuit_wins(self, ctx):
        called: list[str] = []

        class Halter(AgentHook):
            async def before_user_inbound(self, ctx):
                called.append("halter")
                return HookDecision(short_circuit_result="STOP")

        class Trailing(AgentHook):
            async def before_user_inbound(self, ctx):
                called.append("trailing")
                return HookDecision()

        composite = CompositeHook([Halter(), Trailing()])
        decision = await composite.before_user_inbound(ctx)

        assert decision.short_circuit_result == "STOP"
        # Trailing must NOT be invoked once the chain short-circuits.
        assert called == ["halter"]

    async def test_short_circuit_only_in_named_phase(self, ctx):
        """A hook that short-circuits one phase does not affect other
        phases — the chain runs independently per phase."""
        called: list[str] = []

        class Halter(AgentHook):
            async def before_user_inbound(self, ctx):
                called.append("halter_in_user_inbound")
                return HookDecision(short_circuit_result="STOP")

            async def before_iteration(self, ctx):
                called.append("halter_in_iteration")
                return HookDecision()

        class Trailing(AgentHook):
            async def before_user_inbound(self, ctx):
                called.append("trailing_in_user_inbound")
                return HookDecision()

            async def before_iteration(self, ctx):
                called.append("trailing_in_iteration")
                return HookDecision()

        composite = CompositeHook([Halter(), Trailing()])

        await composite.before_user_inbound(ctx)
        # before_user_inbound: halter short-circuits, trailing skipped.
        # before_iteration: both run.
        await composite.before_iteration(ctx)

        assert called == [
            "halter_in_user_inbound",
            "halter_in_iteration",
            "trailing_in_iteration",
        ]


# ---------------------------------------------------------------------------
# 8. after_send content chains through
# ---------------------------------------------------------------------------


class TestCompositeHookContentChain:
    async def test_after_send_chains_modifications(self, ctx):
        ctx.outbound_content = "original"

        class Appender(AgentHook):
            def __init__(self, suffix: str) -> None:
                self.suffix = suffix

            async def after_send(self, ctx):
                base = ctx.outbound_content or ""
                return HookDecision(modified_content=base + self.suffix)

        composite = CompositeHook([Appender(" A"), Appender(" B")])
        decision = await composite.after_send(ctx)

        assert decision.modified_content == "original A B"
        # ctx is left updated so any subsequent inspection sees the
        # fully-chained content.
        assert ctx.outbound_content == "original A B"

    async def test_after_send_no_modification_means_no_change(self, ctx):
        ctx.outbound_content = "untouched"

        class Observer(AgentHook):
            async def after_send(self, ctx):
                # Observer-only, no modification
                return HookDecision()

        composite = CompositeHook([Observer(), Observer()])
        decision = await composite.after_send(ctx)

        assert decision.modified_content is None
        assert ctx.outbound_content == "untouched"

    async def test_after_send_short_circuit_skips_chain(self, ctx):
        ctx.outbound_content = "original"
        called: list[str] = []

        class Replacer(AgentHook):
            async def after_send(self, ctx):
                called.append("replacer")
                return HookDecision(short_circuit_result="replaced")

        class Trailing(AgentHook):
            async def after_send(self, ctx):
                called.append("trailing")
                return HookDecision(modified_content="should not see")

        composite = CompositeHook([Replacer(), Trailing()])
        decision = await composite.after_send(ctx)

        assert decision.short_circuit_result == "replaced"
        assert called == ["replacer"]

    async def test_after_iteration_does_not_chain_content(self, ctx):
        """Only ``after_send`` chains ``modified_content``. Other phases
        ignore it."""
        ctx.outbound_content = "starting"

        class WouldModify(AgentHook):
            async def after_iteration(self, ctx):
                return HookDecision(modified_content="ignored")

        composite = CompositeHook([WouldModify()])
        decision = await composite.after_iteration(ctx)

        # The dispatcher for non-after_send phases does not propagate
        # modified_content into ctx, and the final decision exposes the
        # raw last-modified (which is None because nothing was chained).
        assert decision.modified_content is None
        assert ctx.outbound_content == "starting"  # unchanged


# ---------------------------------------------------------------------------
# Exception isolation
# ---------------------------------------------------------------------------


class TestCompositeHookExceptionIsolation:
    async def test_one_hook_raising_does_not_break_chain(self, ctx):
        called: list[str] = []

        class Bad(AgentHook):
            async def before_iteration(self, ctx):
                called.append("bad")
                raise RuntimeError("boom")

        class Good(AgentHook):
            async def before_iteration(self, ctx):
                called.append("good")
                return HookDecision()

        composite = CompositeHook([Bad(), Good()])
        decision = await composite.before_iteration(ctx)

        # Both ran; Bad's exception was swallowed.
        assert called == ["bad", "good"]
        assert decision.short_circuit_result is None

    async def test_exception_in_short_circuit_position_does_not_halt(self, ctx):
        """A hook that would have short-circuited but raises instead
        should NOT short-circuit — subsequent hooks must still run."""
        called: list[str] = []

        class Bad(AgentHook):
            async def before_user_inbound(self, ctx):
                called.append("bad")
                raise RuntimeError("boom")

        class Trailing(AgentHook):
            async def before_user_inbound(self, ctx):
                called.append("trailing")
                return HookDecision()

        composite = CompositeHook([Bad(), Trailing()])
        decision = await composite.before_user_inbound(ctx)

        assert called == ["bad", "trailing"]
        assert decision.short_circuit_result is None


# ---------------------------------------------------------------------------
# Late registration via append/extend
# ---------------------------------------------------------------------------


class TestCompositeHookRegistration:
    async def test_append_adds_to_tail(self, ctx):
        called: list[str] = []

        class A(AgentHook):
            async def before_iteration(self, ctx):
                called.append("A")
                return HookDecision()

        class B(AgentHook):
            async def before_iteration(self, ctx):
                called.append("B")
                return HookDecision()

        composite = CompositeHook([A()])
        composite.append(B())

        assert len(composite) == 2
        await composite.before_iteration(ctx)
        assert called == ["A", "B"]

    async def test_extend_preserves_order(self, ctx):
        called: list[str] = []

        class Tagged(AgentHook):
            def __init__(self, tag: str) -> None:
                self.tag = tag

            async def before_iteration(self, ctx):
                called.append(self.tag)
                return HookDecision()

        composite = CompositeHook()
        composite.extend([Tagged("x"), Tagged("y"), Tagged("z")])

        await composite.before_iteration(ctx)
        assert called == ["x", "y", "z"]


# ---------------------------------------------------------------------------
# Smoke test: end-to-end mixed chain (mimics the adapter wire-up shape)
# ---------------------------------------------------------------------------


class TestCompositeHookEndToEndScenario:
    async def test_mixed_chain_simulates_sentinel_and_personalizer(self, ctx):
        """A realistic shape: one short-circuit candidate (Sentinel
        decision_consumer style) sitting in front of an observer
        (FeedbackTracker style) — when the short-circuit fires, the
        observer must not run.

        Then a separate phase (after_send) shows an injector-style hook
        modifying the outbound content."""
        log: list[str] = []

        from raven.spine.message import ChatType, Source
        from raven.spine.turn import Origin, TurnRequest

        def _req(text: str) -> TurnRequest:
            return TurnRequest(
                origin=Origin.USER,
                source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
                text=text,
            )

        class FakeDecisionConsumer(AgentHook):
            async def before_user_inbound(self, ctx):
                log.append("decision_consumer.check")
                if ctx.turn_request and ctx.turn_request.text.startswith("/pick"):
                    log.append("decision_consumer.short_circuit")
                    return HookDecision(short_circuit_result="picked option")
                return HookDecision()

        class FakeFeedbackTracker(AgentHook):
            async def before_user_inbound(self, ctx):
                # Observer — must not run when decision_consumer fires.
                log.append("feedback_tracker.observe")
                return HookDecision()

        class FakeNudgeInjector(AgentHook):
            async def after_send(self, ctx):
                log.append("nudge_injector.after_send")
                base = ctx.outbound_content or ""
                return HookDecision(modified_content=base + " [nudge]")

        composite = CompositeHook([FakeDecisionConsumer(), FakeFeedbackTracker(), FakeNudgeInjector()])

        # Path 1: /pick reply → short-circuit, observer skipped.
        ctx.turn_request = _req("/pick 2")
        d1 = await composite.before_user_inbound(ctx)
        assert d1.short_circuit_result == "picked option"
        assert log == ["decision_consumer.check", "decision_consumer.short_circuit"]

        # Path 2: normal reply → consumer doesn't short-circuit, observer runs.
        log.clear()
        ctx.turn_request = _req("hello")
        d2 = await composite.before_user_inbound(ctx)
        assert d2.short_circuit_result is None
        assert log == ["decision_consumer.check", "feedback_tracker.observe"]

        # after_send: nudge injector appends.
        log.clear()
        ctx.outbound_content = "hi user"
        d3 = await composite.after_send(ctx)
        assert d3.modified_content == "hi user [nudge]"
        assert log == ["nudge_injector.after_send"]
