"""One product entry fronting several implementations: the route is picked on ``run``.

A row declaring ``routes`` is the only row the dispatching model sees for the
work its targets do (they ship ``hidden``). Every caller that dispatches to the
row -- ``spawn``, a DAG node and its retries, a WebUI direct chat -- resolves
one backend by name and calls ``run`` on it, so this is the one seat where the
task text and the implementation meet, and the gate lives here rather than once
per caller.

``run`` reads the task as the dispatching model wrote it (``authored_task``;
the rendered ``task`` only where a caller has no other text): a lane may have
inlined whole files into the rendered task, and their contents are neither
routing evidence nor something to send through the host model again. It picks
in this order:

1. a reused ``instance`` handle continues the conversation it was opened on --
   the implementation whose transport already bound the handle in this
   session wins, and nothing is classified;
2. otherwise the host's classifier (set by the manager, which holds the host
   model) picks between the targets and this row's own implementation, reading
   the targets' roster lines only: the fronting row's line is written for the
   dispatching model and argues the opposite case;
3. no classifier, no answer, or an answer outside the set keeps the task on the
   row's own implementation -- a route can redirect, never lose, a task.

Step 2's answer is read twice. It says which implementation the work is for,
and it is also the only thing on this seat that knows *what kind of work it
is*: the classifier answering a target is the entry learning that this task is
the target's specialty. ``_target_open`` then decides whether that specialty's
own lane may run here -- the tier this dispatch runs at, and whether the
deployment holds the credentials the target's pipeline spends. When it may not, the task stays on the
row's own implementation and carries the closed route's own note with it, so the
lane that ends up building it is told what the other lane would have produced,
and how to produce it here.

That note is declared per route and never written in this module. The gate is
one piece of code serving every row that declares a route, and the only row
declaring one today fronts a deck engine: a sentence about decks compiled into
the gate would be appended, the day a second row routes, to requests that are
not decks. A route declaring no note hands the task over exactly as it arrived.

The wrapper is the row's backend as far as the host is concerned: binders and
config pushes are forwarded to every implementation (a target's own row is on
the table but nothing addresses it, so it would otherwise never be bound), and
every other attribute reads off the row's own implementation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from loguru import logger

from raven.agent.subagent.backends.base import optional_keyword
from raven.agent.subagent.mode_tiers import turn_tier_in_force
from raven.config.schema import TIER_LADDER
from raven.contracts.subagent_backend import SubagentBackend

#: ``(menu, task, default) -> chosen name or None``; ``menu`` is ``[(name, description), ...]``.
Router = Callable[[list[tuple[str, str]], str, str], Awaitable[str | None]]

#: ``(target, needs) -> bool``: whether the routed target holds what the route
#: declared its pipeline spends. Injected at the table (see ``AgentRegistry``)
#: rather than read from a module-level config, so what a process routes on is
#: what that process was built with.
#:
#: Both arguments are the question: the target names *whose* credentials are
#: being asked about -- a lane configured from its own product folder, not the
#: host loop -- and ``needs`` is the route's own declaration of what to ask for.
#: A route declaring nothing is never probed.
TargetReady = Callable[[str, Sequence[str]], bool]

#: Marks a route's note as the host speaking rather than the dispatching model.
#:
#: The lane reads one block of task text, and what arrived from the gate is a
#: requirement on the answer rather than part of the request being answered.
HOST_PREFIX = "\n\n[host] "


class RouteTarget(NamedTuple):
    """One candidate target as the entry holds it.

    ``name`` and ``description`` are the roster line the classifier picks from;
    ``owes`` and ``note`` are the declaring row's own words for the case where
    the gate keeps the work here, and ``needs`` and ``min_tier`` are what put a
    route under the gate at all. A plain ``(name, description, backend)`` still
    builds one, with every declared field saying nothing -- which is what a route
    that declares nothing means, and such a route is neither probed nor tiered.
    """

    name: str
    description: str
    backend: SubagentBackend
    owes: str = ""
    note: str = ""
    needs: tuple[str, ...] = ()
    min_tier: str = ""

    def hand_off(self) -> str:
        """What to append to a task classified for this route that stays here."""
        text = self.note.strip()
        return f"{HOST_PREFIX}{text}" if text else ""


class Picked(NamedTuple):
    """Which implementation runs this task, and what the entry owes it.

    A tuple because ``pick`` has always answered ``(name, backend)`` and callers
    index it that way; ``note`` is the third thing the seat now knows and had
    nowhere to put -- see the module docstring for why it is the classifier's
    answer, not the task text, that produces it.
    """

    name: str
    backend: SubagentBackend
    note: str = ""


_BROADCAST = (
    "bind_session_dir",
    "bind_event_sink",
    "bind_caps_listener",
    "bind_unprompted_announcer",
    "set_mcp_source",
    "repoint_pooled_resident",
)


class RoutingBackend(SubagentBackend):
    def __init__(
        self,
        name: str,
        primary: SubagentBackend,
        targets: Sequence[tuple[str, str, SubagentBackend] | RouteTarget],
        *,
        instances: Any,
        target_ready: TargetReady | None = None,
    ) -> None:
        """``targets`` are ``(name, description, backend)`` in route order.

        ``target_ready`` answers whether the deployment holds what a target's own
        pipeline spends; ``None`` is a process that never told the table, and
        reads as ready -- the pre-gate behaviour.
        """
        self.name = name
        self._primary = primary
        self._targets = [RouteTarget(*target) for target in targets]
        self._instances = instances
        self._target_ready = target_ready
        self._router: Router | None = None

    @property
    def streams(self) -> bool:  # type: ignore[override]
        return bool(getattr(self._primary, "streams", False))

    def set_router(self, router: Router | None) -> None:
        self._router = router

    def set_target_ready(self, target_ready: TargetReady | None) -> None:
        self._target_ready = target_ready

    def implementations(self) -> list[SubagentBackend]:
        return [self._primary, *(target.backend for target in self._targets)]

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr in _BROADCAST:
            return self._broadcast(attr)
        return getattr(self._primary, attr)

    def _broadcast(self, attr: str) -> Callable[..., None]:
        def call(*args: Any, **kwargs: Any) -> None:
            for backend in self.implementations():
                member = getattr(backend, attr, None)
                if callable(member):
                    member(*args, **kwargs)

        return call

    def _target_open(self, target: RouteTarget, mode: str | None) -> str:
        """``""`` when ``target`` may run this dispatch, else why it may not.

        Two conditions, both about the deployment rather than the task, which is
        why neither can be asked of the classifier -- and both read off the
        *route's own declaration*, so a route that declares neither is returned
        unconditionally open. ``routes`` is a general facility: a row routing for
        reasons of its own would otherwise be closed by a credential its target
        never spends and a tier its target never asked for, having opted into
        nothing.

        The credentials come first because they are the flat answer: a target
        whose pipeline cannot buy a picture builds the same deck at every tier.
        The tier comes second, and where it is read from depends on what kind of
        dispatch this is; see :meth:`_tier_in_force`.
        """
        if target.needs and self._target_ready is not None:
            try:
                ready = bool(self._target_ready(target.name, target.needs))
            except Exception as exc:  # noqa: BLE001 - a readiness probe must not lose the task
                logger.warning(
                    "{!r}: could not read what {!r} needs; treating as ready: {}", self.name, target.name, exc
                )
                ready = True
            if not ready:
                return "a credential its lane spends is not configured here"
        if not target.min_tier:
            return ""
        where, tier = self._tier_in_force(mode)
        if not tier:
            # Nothing declared one. Three separate facts land here -- a turn that
            # began with no tier, a dispatch outside any turn that names no mode,
            # and a deployment that sets neither -- and none of them is a low
            # tier. Reading the absence as one would close the target on a
            # deployment that never asked for tiers at all.
            return ""
        if tier not in TIER_LADDER or target.min_tier not in TIER_LADDER:
            # Another vocabulary. ``resolve_tier`` declines to guess across one
            # rather than pick a nearest rung it cannot order, and so does this.
            return ""
        if TIER_LADDER.index(tier) < TIER_LADDER.index(target.min_tier):
            return f"{where} runs at {tier!r}, below {target.min_tier!r}"
        return ""

    @staticmethod
    def _tier_in_force(mode: str | None) -> tuple[str, str]:
        """Where this dispatch's tier comes from, and what it is.

        Two lanes reach this seat and they carry the same fact in two places. A
        spawn, a DAG node and their retries run inside a host turn, and the tier
        is the turn's own frozen snapshot -- frozen for the reason ``turn_tier``
        freezes it, so a switch arriving mid-turn belongs to the next turn and a
        dispatch late in this one cannot pick up a tier the turn never started
        under. A direct chat has no turn scope at all, and its tier is the
        ``mode`` the manager resolved for the dispatch (the instance's own
        override, else the session's standing tier).

        The absence of a turn is therefore not the absence of a tier, which is
        what this used to read it as: every direct chat -- the surface the user
        actually talks to a row through -- reached the target at every tier, and
        no setting on the page could exercise the other lane.
        """
        turn = turn_tier_in_force()
        if turn is None:
            return "this chat", (mode or "").strip()
        return "the turn", turn.strip()

    async def pick(
        self, task: str, *, session_key: str | None, instance: str | None, mode: str | None = None
    ) -> Picked:
        """What runs this task; see the module docstring for the order."""
        if instance and (bound := await self._bound(session_key, instance)) is not None:
            return bound
        if self._router is not None and self._targets:
            menu = [(target.name, target.description) for target in self._targets]
            try:
                answer = await self._router(menu, task, self.name)
            except Exception as exc:  # noqa: BLE001 - a routing failure must not lose the task
                logger.warning("{!r}: the route classifier failed; running here: {}", self.name, exc)
                answer = None
            for target in self._targets:
                if answer == target.name:
                    if closed := self._target_open(target, mode):
                        logger.info(
                            "{!r}: classified for {!r}, but {}; running here{}",
                            self.name,
                            target.name,
                            closed,
                            f" and owing a {target.owes}" if target.owes else "",
                        )
                        return Picked(self.name, self._primary, target.hand_off())
                    logger.info("{!r}: classified for {!r}; routing there", self.name, target.name)
                    return Picked(target.name, target.backend)
            if answer is not None and answer != self.name:
                logger.warning(
                    "{!r}: the route classifier answered {!r}, not a candidate; running here", self.name, answer
                )
        return Picked(self.name, self._primary)

    async def _bound(self, session_key: str | None, instance: str) -> Picked | None:
        """The implementation whose transport already holds this handle, if any.

        Read off the instance registry rows the transports write when a session
        is bound (``agentId`` set). The manager's own status row for the handle
        is written under this row's name before the first run and carries no
        session, so it is not evidence; a bound session under this row's name is.
        """
        try:
            records = self._instances.list_instances(session_key or "default")
        except Exception as exc:  # noqa: BLE001 - the registry is best-effort storage
            logger.warning("{!r}: could not read the instance registry: {}", self.name, exc)
            return None
        bound = {r.get("agent") for r in records if r.get("handle") == instance and r.get("agentId")}
        for target in self._targets:
            if target.name in bound:
                return Picked(target.name, target.backend)
        if self.name in bound:
            return Picked(self.name, self._primary)
        return None

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
        authored_task: str | None = None,
        **kwargs: Any,
    ) -> str:
        # Read out of ``kwargs`` rather than named as a parameter: every
        # implementation behind this entry already takes ``mode`` and gets it
        # from the same dict, and lifting it out would hand one to a backend the
        # caller never passed one to.
        picked = await self.pick(
            authored_task or task, session_key=session_key, instance=instance, mode=kwargs.get("mode")
        )
        if picked.note:
            # Both texts, because which one an implementation reads is its own
            # choice: the note is the host's requirement on the deliverable, and
            # a lane that happened to read the other copy would not have it.
            task = f"{task}{picked.note}"
            if authored_task is not None:
                authored_task = f"{authored_task}{picked.note}"
        return await picked.backend.run(
            task,
            task_id=task_id,
            workspace=workspace,
            executor=executor,
            session_key=session_key,
            instance=instance,
            # The same hand-off the lanes make: an implementation typed against
            # the paper before this keyword existed is not handed it.
            **optional_keyword(picked.backend, "authored_task", authored_task),
            **kwargs,
        )


__all__ = ["HOST_PREFIX", "Picked", "RouteTarget", "Router", "RoutingBackend", "TargetReady"]
