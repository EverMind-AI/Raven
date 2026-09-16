"""The adapter that lets a conduct sit in the hook chain: one turn, one instance.

``ConductHook`` is an ``AgentHook`` the composite runs like any other. Each
phase builds a ``StepView`` from the hook context, asks the conduct the verbs
that belong to that phase, and renders the answers as a ``HookDecision`` --
so the loop's timing, the composite's merge rules and the rollback budget are
untouched, and the conduct never sees the context.

A new conduct is made from the factory the first time a turn's context is
seen: the loop builds one ``AgentHookContext`` per turn and mutates it across
iterations, so the context's identity is the turn's. ``before_user_inbound``
also starts a fresh one, since it is the turn's first phase whenever it runs
(sub-agent and sentinel turns skip it, which is why identity is the rule and
this is only the tidy case).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from raven.agent.harness import current_harness
from raven.agent.harness.conducts import compose_advice, compose_intake, compose_review, compose_salvage
from raven.contracts.agent_conduct import AgentConduct, ConductFactory, StepView
from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision


@dataclass
class _Seat:
    """One turn's conduct and what it spliced into the system message.

    The addendum is (the part as it was spliced, where it went), so the next
    call takes that one back out rather than stacking a second copy.
    """

    conduct: AgentConduct
    addendum: tuple[Any, int] | None = None


class ConductHook(AgentHook):
    """One plugin's conduct, seated in the hook chain."""

    # A conduct's ``review`` may resample, so the loop holds this turn's
    # deltas the way it does for any hook that can send a response back.
    rolls_back_iterations = True

    def __init__(self, name: str, factory: ConductFactory) -> None:
        self._name = name
        self._factory = factory
        self._seat_key = f"raven.conduct.{name}"
        # Only for a caller with no metadata dict to seat the turn in.
        self._turn: Any = None
        self._seat_fallback: _Seat | None = None
        self._last_seat: _Seat | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def factory(self) -> ConductFactory:
        """The factory, for a host that wants to seat the conduct elsewhere."""
        return self._factory

    @property
    def conduct(self) -> AgentConduct | None:
        """The conduct of the turn whose phase ran last, for a host or a test
        that wants to look at it. Never read inside a phase: with two turns in
        flight the last one to start is not this one."""
        return self._last_seat.conduct if self._last_seat is not None else None

    def _seat(self, ctx: Any, *, fresh: bool = False) -> "_Seat":
        """This turn's conduct and the addendum it spliced.

        Parked in the turn's own ``metadata`` dict, which is where the hook
        context has always carried a plugin's turn state: one hook instance
        serves every turn a process runs, and a system turn can overlap a user
        one on the same chain, so a seat kept on the hook would have the two
        turns discarding each other's counters. A caller with no dict to share
        (a phase-level test) falls back to the hook, keyed on the context.
        """
        meta = getattr(ctx, "metadata", None)
        if isinstance(meta, dict):
            seat = meta.get(self._seat_key)
            if fresh or not isinstance(seat, _Seat):
                seat = _Seat(self._factory())
                meta[self._seat_key] = seat
        else:
            seat = self._seat_fallback
            if fresh or seat is None or self._turn is not ctx:
                seat = _Seat(self._factory())
                self._seat_fallback = seat
                self._turn = ctx
        self._last_seat = seat
        return seat

    @staticmethod
    def _step(ctx: Any, *, tools_ran: bool = False) -> StepView:
        # ``getattr`` throughout: the loop hands a full ``AgentHookContext``,
        # a phase-level test hands the two fields it cares about.
        meta = getattr(ctx, "metadata", None) or {}
        return StepView(
            session_key=getattr(ctx, "session_key", "") or "",
            iteration=getattr(ctx, "iteration", None) or 0,
            response=getattr(ctx, "response", None),
            transcript=tuple(getattr(ctx, "messages", None) or ()),
            history=tuple(getattr(ctx, "session_history", None) or ()),
            turn_base=getattr(ctx, "turn_base", 0) or 0,
            question=getattr(ctx, "turn_question", "") or "",
            rollbacks=int(meta.get("hook_rollbacks", 0) or 0),
            mode=meta.get("mode"),
            mode_overlay=meta.get("mode_overlay"),
            tools_ran=tools_ran,
            tools=tuple(getattr(ctx, "tools", None) or ()),
            window=getattr(ctx, "context_window_tokens", None) or None,
            max_iterations=getattr(ctx, "max_iterations", None) or None,
        )

    @staticmethod
    def _with_trail(seat: "_Seat", decision: HookDecision) -> HookDecision:
        """The decision with whatever this turn's conduct noted along the way."""
        trail = seat.conduct.drain_trail()
        if not trail:
            return decision
        return replace(decision, notes=[*decision.notes, *trail])

    @staticmethod
    def _decide(verdict) -> HookDecision:
        notes = [verdict.note] if verdict.note else []
        if verdict.kind == "resample":
            return HookDecision(
                rollback=True,
                rollback_inject=list(verdict.inject) if verdict.inject else None,
                rollback_overrides=dict(verdict.overrides) if verdict.overrides else None,
                notes=notes,
            )
        if verdict.kind == "end":
            return HookDecision(short_circuit_result=verdict.reply, notes=notes)
        return HookDecision(notes=notes)

    async def _intake(self, text: str, step: StepView, conduct: AgentConduct):
        """What this turn reads in, decided by the Memory role when one is bound."""
        harness = current_harness()
        if harness is None:
            return await compose_intake(text, step, [conduct])
        return await harness.memory.intake(text, step, [conduct])

    async def _advise(self, step: StepView, conduct: AgentConduct) -> str | None:
        """The turn guidance, decided by the Planning role when one is bound."""
        harness = current_harness()
        if harness is None:
            return await compose_advice(step, [conduct])
        return await harness.planning.advise(step, [conduct])

    async def _review(self, step: StepView, conduct: AgentConduct):
        """The verdict on this step, decided by the Action role when one is bound."""
        harness = current_harness()
        if harness is None:
            return await compose_review(step, [conduct])
        return await harness.action.review(step, [conduct])

    async def _salvage(self, step: StepView, conduct: AgentConduct):
        """What a turn with no answer sends, decided by the Action role when bound."""
        harness = current_harness()
        if harness is None:
            return await compose_salvage(step, [conduct])
        return await harness.action.salvage(step, [conduct])

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        seat = self._seat(ctx, fresh=True)
        text = getattr(ctx, "inbound_content", None) or ""
        intake = await self._intake(text, self._step(ctx), seat.conduct)
        if intake is None:
            return self._with_trail(seat, HookDecision())
        notes = [intake.note] if intake.note else []
        if intake.reply is not None:
            return self._with_trail(seat, HookDecision(short_circuit_result=intake.reply, notes=notes))
        if intake.text != text:
            return self._with_trail(seat, HookDecision(modified_content=intake.text, notes=notes))
        return self._with_trail(seat, HookDecision(notes=notes))

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        seat = self._seat(ctx)
        conduct = seat.conduct
        # The system message is shown without this conduct's earlier addendum,
        # so what the conduct sizes its text against is the prefix it will be
        # spliced into, and a turn that adds nothing this call leaves nothing.
        self._strip_addendum(ctx, seat)
        step = self._step(ctx)
        offered = getattr(ctx, "tools", None)
        narrowed = await conduct.select_tools(list(offered or []), step) if offered is not None else None
        note = await self._advise(step, conduct)
        addendum = await conduct.system_addendum(step)
        if addendum is not None and addendum.reply is not None:
            return self._with_trail(
                seat,
                HookDecision(short_circuit_result=addendum.reply, notes=[addendum.note] if addendum.note else []),
            )
        if addendum is not None and addendum.text:
            self._splice_addendum(ctx, addendum.text, seat)
        return self._with_trail(
            seat,
            HookDecision(
                modified_tools=narrowed if narrowed is not None and narrowed != offered else None,
                append_note=note or None,
                notes=[addendum.note] if addendum is not None and addendum.note else [],
            ),
        )

    @staticmethod
    def _system_message(ctx: Any) -> dict[str, Any] | None:
        messages = getattr(ctx, "messages", None) or []
        return next((m for m in messages if isinstance(m, dict) and m.get("role") == "system"), None)

    def _strip_addendum(self, ctx: Any, seat: "_Seat") -> None:
        """Take this conduct's previous addendum back out of the system message."""
        if seat.addendum is None:
            return
        previous, offset = seat.addendum
        seat.addendum = None
        system = self._system_message(ctx)
        if system is None:
            return
        content = system.get("content") or ""
        if isinstance(content, str) and isinstance(previous, str):
            if content[offset : offset + len(previous)] == previous:
                system["content"] = content[:offset] + content[offset + len(previous) :]
        elif isinstance(content, list) and offset < len(content) and content[offset] == previous:
            content = list(content)
            content.pop(offset)
            system["content"] = content

    def _splice_addendum(self, ctx: Any, text: str, seat: "_Seat") -> None:
        """Append ``text`` to the system message, in the shape its content has."""
        system = self._system_message(ctx)
        if system is None:
            return
        content = system.get("content") or ""
        if isinstance(content, list):
            part: Any = {"type": "text", "text": ("\n\n" if content else "") + text}
            system["content"] = [*content, part]
        else:
            part = ("\n\n" if content else "") + text
            system["content"] = content + part
        seat.addendum = (part, len(content))

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        seat = self._seat(ctx)
        return self._with_trail(seat, self._decide(await self._review(self._step(ctx), seat.conduct)))

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        seat = self._seat(ctx)
        conduct = seat.conduct
        # After the iteration, whatever the model proposed has run.
        step = self._step(ctx, tools_ran=True)
        # Advice first and observation always: the six hooks this seat replaces
        # were separate entries in the chain, so one that counted something
        # counted it whether or not a later one ended the turn. What the loop is
        # handed still follows the chain: a decision that ends or resamples
        # carries no appended note, because the composite drops the notes it
        # accumulated the moment a hook answers with one of those.
        note = await self._advise(step, conduct)
        verdict = await self._review(step, conduct)
        await conduct.observe(step)
        decision = self._decide(verdict)
        if not verdict.accepted:
            return self._with_trail(seat, decision)
        return self._with_trail(seat, replace(decision, append_note=note or None))

    async def terminal_answerless(self, ctx: AgentHookContext) -> HookDecision:
        seat = self._seat(ctx)
        salvaged = await self._salvage(self._step(ctx), seat.conduct)
        answer = HookDecision() if salvaged is None else HookDecision(short_circuit_result=salvaged)
        return self._with_trail(seat, answer)

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        seat = self._seat(ctx)
        conduct = seat.conduct
        step = self._step(ctx)
        reply = getattr(ctx, "outbound_content", None) or ""
        sending = await conduct.outbound(reply, step)
        filed = await conduct.archive(step, reply or None)
        if filed:
            # Stamped where the loop files a turn's observers: one entry per
            # observer name, merged so another conduct's counters stand.
            meta = getattr(ctx, "metadata", None)
            if isinstance(meta, dict):
                observers = meta.setdefault("observers", {})
                for name, counters in filed.items():
                    if isinstance(counters, Mapping) and isinstance(observers.get(name), dict):
                        observers[name].update(dict(counters))
                    else:
                        observers[name] = dict(counters) if isinstance(counters, Mapping) else counters
        if sending is None or sending == reply:
            return self._with_trail(seat, HookDecision())
        return self._with_trail(seat, HookDecision(modified_content=sending))


__all__ = ["ConductHook"]
