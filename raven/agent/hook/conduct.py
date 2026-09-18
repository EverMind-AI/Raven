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
from raven.agent.harness.conducts import (
    compose_addendum,
    compose_advice,
    compose_intake,
    compose_record,
    compose_review,
    compose_salvage,
    compose_tools,
)
from raven.contracts.agent_conduct import AgentConduct, ConductFactory, StepView
from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision


class _Row(dict):
    """One message as a conduct sees it: a dict that refuses to be written.

    A ``dict`` subclass rather than ``MappingProxyType`` because every reader on
    the other side of this seam asks ``isinstance(m, dict)`` -- the loop's own
    helpers do, and so do the plugins -- and a proxy fails that test. This keeps
    every read working and turns the write the paper forbids into a loud
    ``TypeError`` instead of a silent edit of the transcript the loop prompts
    with.

    Shallow, like the freeze it replaces: a nested list inside a row is still
    the loop's own object. Naming that limit is better than implying a depth
    this does not have.
    """

    __slots__ = ()

    def _refuse(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("a StepView row is read-only: a conduct answers by returning, never by writing to the step")

    __setitem__ = _refuse
    __delitem__ = _refuse
    clear = _refuse
    pop = _refuse
    popitem = _refuse
    setdefault = _refuse
    update = _refuse


def _frozen(value: Any) -> Any:
    """A mapping a conduct cannot write through, or the value unchanged."""
    return _Row(value) if isinstance(value, Mapping) else value


def _readonly(rows: Any) -> tuple[Any, ...]:
    """The loop's own rows, published so a conduct cannot edit them.

    ``tuple`` freezes the sequence; the mappings inside it are the very objects
    the loop prompts with, so a plain tuple would let a conduct rewrite the
    transcript through a field the paper calls read-only.
    """
    return tuple(_frozen(row) for row in (rows or ()))


@dataclass
class _Seat:
    """One turn's conduct and what it spliced into the system message.

    The addendum is the part exactly as it was spliced, so the next call finds
    that one and takes it back out rather than stacking a second copy.
    """

    conduct: AgentConduct
    addendum: Any = None


class ConductHook(AgentHook):
    """One plugin's conduct, seated in the hook chain."""

    # The default a seat declares to the loop, overridden per seat below. Kept
    # as a class attribute because that is where the rollback registry looks.
    rolls_back_iterations = True

    def __init__(self, name: str, factory: ConductFactory, *, rolls_back: bool = True) -> None:
        """``rolls_back`` is what the seat declares to the loop.

        The loop reads ``rolls_back_iterations`` off every hook it holds and, if
        any says yes, withholds the reply's tokens behind a draft gate so a
        verdict can still send the turn back. That is the right default for a
        conduct whose ``review`` may resample, and the wrong one for a conduct
        that never does: it would buy nothing and cost the incremental reply the
        reader sees. Declared per seat rather than inherited, because the
        plugins this replaces declared it per hook.
        """
        self.rolls_back_iterations = rolls_back
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
    def _step(ctx: Any, *, phase: str) -> StepView:
        # ``getattr`` throughout: the loop hands a full ``AgentHookContext``,
        # a phase-level test hands the two fields it cares about.
        meta = getattr(ctx, "metadata", None) or {}
        return StepView(
            session_key=getattr(ctx, "session_key", "") or "",
            iteration=getattr(ctx, "iteration", None) or 0,
            response=getattr(ctx, "response", None),
            transcript=_readonly(getattr(ctx, "messages", None)),
            history=_readonly(getattr(ctx, "session_history", None)),
            turn_base=getattr(ctx, "turn_base", 0) or 0,
            question=getattr(ctx, "turn_question", "") or "",
            rollbacks=int(meta.get("hook_rollbacks", 0) or 0),
            mode=meta.get("mode"),
            mode_overlay=_frozen(meta.get("mode_overlay")),
            phase=phase,
            tools=_readonly(getattr(ctx, "tools", None)),
            window=getattr(ctx, "context_window_tokens", None) or None,
            max_iterations=getattr(ctx, "max_iterations", None) or None,
        )

    @staticmethod
    def _with_trail(seat: "_Seat", decision: HookDecision) -> HookDecision:
        """The decision with whatever this turn's conduct noted along the way.

        ``getattr`` because the trail is a convenience the base class offers,
        not a verb the contract requires: a conduct that implements the nine
        verbs without inheriting ``AgentConduct`` is a conduct, and asking it
        for a method it never claimed would raise into the composite's catch --
        which would log the plugin as broken and drop the answer it just gave.
        """
        drain = getattr(seat.conduct, "drain_trail", None)
        trail = drain() if callable(drain) else []
        if not trail:
            return decision
        return replace(decision, notes=[*decision.notes, *trail])

    @staticmethod
    def _decide(verdict) -> HookDecision:
        # Both, de-duplicated: ``Resample`` takes its reason positionally, so an
        # author writes one there and often the same sentence again as a note.
        # Reading only the note dropped the line a conduct that wrote just the
        # reason meant to leave behind.
        notes = list(dict.fromkeys(n for n in (verdict.reason, verdict.note) if n))
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
        return await harness.memory.read_inbound(text, step, [conduct])

    async def _advise(self, step: StepView, conduct: AgentConduct) -> str | None:
        """The turn guidance, decided by the Planning role when one is bound."""
        harness = current_harness()
        if harness is None:
            return await compose_advice(step, [conduct])
        return await harness.planning.guide(step, [conduct])

    async def _review(self, step: StepView, conduct: AgentConduct):
        """The verdict on this step, decided by the Action role when one is bound."""
        harness = current_harness()
        if harness is None:
            return await compose_review(step, [conduct])
        return await harness.action.judge_step(step, [conduct])

    async def _salvage(self, step: StepView, conduct: AgentConduct):
        """What a turn with no answer sends, decided by the Action role when bound."""
        harness = current_harness()
        if harness is None:
            return await compose_salvage(step, [conduct])
        return await harness.action.rescue(step, [conduct])

    async def _addendum(self, step: StepView, conduct: AgentConduct):
        """What this call adds to the system message, composed by Memory."""
        harness = current_harness()
        if harness is None:
            return await compose_addendum(step, [conduct])
        return await harness.memory.compose_addendum(step, [conduct])

    async def _record(self, step: StepView, reply: str | None, conduct: AgentConduct):
        """What the turn's record is stamped with, merged by Memory."""
        harness = current_harness()
        if harness is None:
            return await compose_record(step, reply, [conduct])
        return await harness.memory.file_record(step, reply, [conduct])

    async def _tools(self, offered: list[dict[str, Any]], step: StepView, conduct: AgentConduct):
        """The tool array this iteration carries, composed by Capability."""
        harness = current_harness()
        if harness is None:
            return await compose_tools(offered, step, [conduct])
        return await harness.capability.offer(offered, step, [conduct])

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        seat = self._seat(ctx, fresh=True)
        text = getattr(ctx, "inbound_content", None) or ""
        intake = await self._intake(text, self._step(ctx, phase="user_inbound"), seat.conduct)
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
        step = self._step(ctx, phase="iteration")
        offered = getattr(ctx, "tools", None)
        narrowed = await self._tools(list(offered or []), step, conduct) if offered is not None else None
        note = await self._advise(step, conduct)
        addendum = await self._addendum(step, conduct)
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
        """Take this conduct's previous addendum back out of the system message.

        Found by its text rather than by where it was put: another conduct's
        seat may have spliced after this one and stripped before it, so an
        offset taken last iteration does not survive a second seat. The
        bookkeeping is cleared only once the text is actually gone -- a strip
        that cannot find its target has not removed anything, and forgetting it
        would splice a second copy next call and a third after that.
        """
        previous = seat.addendum
        if previous is None:
            return
        system = self._system_message(ctx)
        if system is None:
            return
        content = system.get("content")
        if isinstance(content, str) and isinstance(previous, str):
            at = content.rfind(previous)
            if at < 0:
                return
            system["content"] = content[:at] + content[at + len(previous) :]
        elif isinstance(content, list):
            # By equality and from the end, like the string branch: the loop
            # rebuilds the content list between iterations, so the part this
            # seat spliced comes back equal rather than identical.
            at = next((i for i in range(len(content) - 1, -1, -1) if content[i] == previous), None)
            if at is None:
                return
            system["content"] = [*content[:at], *content[at + 1 :]]
        else:
            return
        seat.addendum = None

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
        seat.addendum = part

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        seat = self._seat(ctx)
        return self._with_trail(
            seat, self._decide(await self._review(self._step(ctx, phase="execute_tools"), seat.conduct))
        )

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        seat = self._seat(ctx)
        conduct = seat.conduct
        # After the iteration, whatever the model proposed has run.
        step = self._step(ctx, phase="after_iteration")
        # Advice first and observation always: the six hooks this seat replaces
        # were separate entries in the chain, so one that counted something
        # counted it whether or not a later one ended the turn. What the loop is
        # handed still follows the chain: a decision that ends or resamples
        # carries no appended note, because the composite drops the notes it
        # accumulated the moment a hook answers with one of those.
        note = await self._advise(step, conduct)
        verdict = await self._review(step, conduct)
        decision = self._decide(verdict)
        if not verdict.accepted:
            return self._with_trail(seat, decision)
        return self._with_trail(seat, replace(decision, append_note=note or None))

    async def terminal_answerless(self, ctx: AgentHookContext) -> HookDecision:
        seat = self._seat(ctx)
        salvaged = await self._salvage(self._step(ctx, phase="answerless"), seat.conduct)
        answer = HookDecision() if salvaged is None else HookDecision(short_circuit_result=salvaged)
        return self._with_trail(seat, answer)

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        seat = self._seat(ctx)
        conduct = seat.conduct
        step = self._step(ctx, phase="sent")
        reply = getattr(ctx, "outbound_content", None) or ""
        sending = await conduct.outbound(reply, step)
        filed = await self._record(step, reply or None, conduct)
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
