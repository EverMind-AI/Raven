"""Decide what a replan carries into its successor run.

A replan's node list is the successor run's plan. The plan includes every node it
lists and, transitively, every unfinished node of the replanned run that an
included node depends on. Those unfinished nodes are carried into the successor
-- a running one keeps running -- and every other unfinished node stops with the
old run. Finished nodes are never carried: the successor reads them as finished
context.

Pure planning over the run's state as the caller read it: nothing here touches a
store, a desk or a task. The graph tool plans a replan before asking anyone, and
the runner plans it again at commit, over the state as it stands then -- which is
how a change that happened in between is either tolerated or refused.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from raven.agent.subagent.dag_graph import DagNodeSpec

NodeClass = Literal["finished", "in_progress", "queued", "not_started", "answered"]

FINISHED_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "skipped", "cancelled", "not_executed", "carried"}
)

_UNFINISHED: frozenset[str] = frozenset({"in_progress", "queued", "not_started"})
_DEAD: dict[str, str] = {"failed": "failed", "skipped": "was skipped", "cancelled": "was cancelled"}


def classify_nodes(
    status: Mapping[str, str],
    *,
    begun: Collection[str],
    in_flight: Collection[str],
    queued: Collection[str],
    answered: str,
) -> dict[str, NodeClass]:
    """Each node's class, the first matching rule deciding it.

    ``begun`` holds the nodes some attempt has started on, ``in_flight`` the nodes
    whose current attempt has not settled, and ``queued`` the nodes a dispatched
    task holds. A node in flight is in progress whatever its status reads: between
    the backend's return and the verdict it reads ``completed``, and carrying or
    stopping it there must not count an unjudged attempt as finished. A ``pending``
    node that has begun an attempt is in progress too -- it is waiting to run
    again, with its history on record.
    """
    classes: dict[str, NodeClass] = {}
    for node_id, state in status.items():
        if node_id == answered:
            classes[node_id] = "answered"
        elif node_id in in_flight:
            classes[node_id] = "in_progress"
        elif state in FINISHED_STATUSES:
            classes[node_id] = "finished"
        elif state in ("running", "exception"):
            classes[node_id] = "in_progress"
        elif state == "pending" and node_id in begun:
            classes[node_id] = "in_progress"
        elif state == "pending" and node_id in queued:
            classes[node_id] = "queued"
        else:
            classes[node_id] = "not_started"
    return classes


def _comparable(node: DagNodeSpec) -> dict[str, object]:
    """A node's fields as compared for sameness; a blank guard reads as no guard."""
    dumped = node.model_dump()
    when = dumped.get("when")
    dumped["when"] = when.strip() or None if isinstance(when, str) else when
    return dumped


def _definition(node_id: str, authored: Mapping[str, DagNodeSpec], current: Mapping[str, DagNodeSpec]) -> DagNodeSpec:
    """The definition a node of the run is carried or read with: as authored, else as it dispatches."""
    return authored.get(node_id) or current[node_id]


def differing_fields(
    entry: DagNodeSpec,
    *,
    authored: DagNodeSpec,
    current: DagNodeSpec | None = None,
    ignore_summary: bool = False,
) -> list[str]:
    """The sorted names of the fields in which ``entry`` differs from ``authored``.

    ``current`` is the node as it dispatches now. A reassign may have moved it to
    another agent and handle, so ``subagent`` and ``instance`` also match the
    values it runs under, not only the submitted ones.
    """
    listed, submitted = _comparable(entry), _comparable(authored)
    running = _comparable(current) if current is not None else None
    differ: list[str] = []
    for name, value in listed.items():
        if ignore_summary and name == "node_summary":
            continue
        if value == submitted.get(name):
            continue
        if running is not None and name in ("subagent", "instance") and value == running.get(name):
            continue
        differ.append(name)
    return sorted(differ)


@dataclass(frozen=True)
class CarryPlan:
    """What one replan does to the run it answers.

    Attributes:
        new (`tuple[str, ...]`):
            Ids the run has never had, in the order they were listed.
        redefined (`tuple[str, ...]`):
            Not-started nodes listed with a different definition, in the run's order.
        carried (`tuple[str, ...]`):
            Every unfinished node the plan includes, in the run's order; the
            redefined ones are among them.
        dropped (`tuple[str, ...]`):
            The unfinished nodes the plan does not include, in the run's order.
            They stop with the old run.
        context (`tuple[str, ...]`):
            The finished nodes the plan reaches. They stay in the old run, and the
            successor reads them as that run's plan read them.
        nodes (`tuple[DagNodeSpec, ...]`):
            The successor's own nodes: the carried ones in the run's order -- the
            listed definition for a redefined node, the authored one otherwise --
            then the new ones in the order they were listed.
    """

    new: tuple[str, ...]
    redefined: tuple[str, ...]
    carried: tuple[str, ...]
    dropped: tuple[str, ...]
    context: tuple[str, ...]
    nodes: tuple[DagNodeSpec, ...]


def plan_carry(
    entries: Sequence[DagNodeSpec],
    *,
    authored: Mapping[str, DagNodeSpec],
    current: Mapping[str, DagNodeSpec],
    classes: Mapping[str, NodeClass],
    status: Mapping[str, str],
    carried_before: Collection[str] = (),
) -> CarryPlan | list[str]:
    """Plan a replan over the run's state, or say every reason it cannot be applied.

    ``entries`` is the replan's node list as parsed. ``authored`` holds the run's
    nodes as submitted (or as an earlier replan redefined them), ``current`` holds
    them as they dispatch now, ``classes`` comes from :func:`classify_nodes`, and
    ``status`` is the scheduler's own status of each node. The run's order is the
    order of ``classes``. An entry whose id is not in ``classes`` is a new node;
    whether its id is free in the conversation is the caller's to check.

    ``carried_before`` holds the nodes an earlier planning of the same replan
    carried as they were. One of them listed here that has finished since, and did
    not die, is not refused as a finished node listed: the plan still says the same
    thing, and the node reads as finished like any other.
    """
    listing_errors: list[str] = []
    dead_errors: list[str] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    new: list[str] = []
    new_entries: dict[str, DagNodeSpec] = {}
    redefined: set[str] = set()
    definitions: dict[str, DagNodeSpec] = {}
    included: set[str] = set()

    for entry in entries:
        node_id = entry.id
        if node_id in seen:
            if node_id not in duplicates:
                duplicates.append(node_id)
            continue
        seen.add(node_id)
        node_class = classes.get(node_id)
        if node_class is None:
            new.append(node_id)
            new_entries[node_id] = entry
            definitions[node_id] = entry
            continue
        if node_class == "answered":
            listing_errors.append(
                f"node '{node_id}' is the node this replan answers, so it is given up; redo its work under a new id"
            )
            continue
        if node_class == "finished":
            if node_id in carried_before and status.get(node_id) not in _DEAD:
                continue
            listing_errors.append(
                f"node '{node_id}' has finished ({status.get(node_id, 'finished')}); depend on it to read its "
                "output instead of listing it"
            )
            continue
        submitted = authored.get(node_id) or current.get(node_id) or entry
        if node_class == "not_started":
            if differing_fields(entry, authored=submitted):
                redefined.add(node_id)
                definitions[node_id] = entry
            else:
                definitions[node_id] = submitted
            included.add(node_id)
            continue
        changed = differing_fields(entry, authored=submitted, current=current.get(node_id), ignore_summary=True)
        if changed:
            state = "in progress" if node_class == "in_progress" else "queued"
            listing_errors.append(
                f"node '{node_id}' is {state} and can only be carried as it is; it differs in {', '.join(changed)}. "
                "List it unchanged or depend on it to carry it, or add a node under a new id and leave this one out"
            )
            continue
        definitions[node_id] = submitted
        included.add(node_id)

    context: set[str] = set()
    # Each item is (reader, dependency, reached through a finished node). Past a
    # finished node the walk only collects finished context: the successor renders
    # a node's upstream record lines from its own node table, so the finished nodes
    # two hops up must be there too. It never carries anything from there.
    stack: list[tuple[str, str, bool]] = [
        (reader, dep, False) for reader in list(definitions) for dep in definitions[reader].depends_on
    ]
    walked: set[tuple[str, str]] = set()
    while stack:
        reader, dep, via_finished = stack.pop()
        if (reader, dep) in walked:
            continue
        walked.add((reader, dep))
        dep_class = classes.get(dep)
        if dep_class is None:
            continue
        if via_finished:
            if dep_class == "finished" and status.get(dep) not in _DEAD and dep not in context:
                context.add(dep)
                stack.extend((dep, upstream, True) for upstream in _definition(dep, authored, current).depends_on)
            continue
        if dep_class == "answered":
            dead_errors.append(
                f"node '{reader}' depends on '{dep}', which is the node this replan answers and is given up; "
                f"redefine '{reader}' to read the node that redoes its work, or leave it out"
            )
            continue
        if dep_class == "finished":
            dead = _DEAD.get(status.get(dep, ""))
            reader_class = classes.get(reader)
            if dead is not None and reader_class in (None, "not_started"):
                dead_errors.append(
                    f"node '{reader}' depends on '{dep}', which {dead} in this run; redefine '{reader}' to read the "
                    "node that does that work now, or leave it out"
                )
            elif dead is None and dep not in context:
                context.add(dep)
                stack.extend((dep, upstream, True) for upstream in _definition(dep, authored, current).depends_on)
            continue
        if dep not in included:
            included.add(dep)
            definitions[dep] = _definition(dep, authored, current)
            stack.extend((dep, upstream, False) for upstream in definitions[dep].depends_on)

    head_errors: list[str] = [f"node '{node_id}' is listed twice" for node_id in duplicates]
    if not new and not redefined:
        head_errors.append(
            "a replan has to change the plan: add a node or redefine one that has not started. "
            "To give up on this node alone, abandon it"
        )
    errors = head_errors + listing_errors + dead_errors
    if errors:
        return errors

    order = list(classes)
    carried = tuple(node_id for node_id in order if node_id in included and classes[node_id] in _UNFINISHED)
    return CarryPlan(
        new=tuple(new),
        redefined=tuple(node_id for node_id in carried if node_id in redefined),
        carried=carried,
        dropped=tuple(node_id for node_id in order if classes[node_id] in _UNFINISHED and node_id not in included),
        context=tuple(node_id for node_id in order if node_id in context),
        nodes=tuple(definitions[node_id] for node_id in carried) + tuple(new_entries[node_id] for node_id in new),
    )


__all__ = ["FINISHED_STATUSES", "CarryPlan", "NodeClass", "classify_nodes", "differing_fields", "plan_carry"]
